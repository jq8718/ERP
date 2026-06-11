from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from accounts.models import Role, User
from accounts.permissions import PermissionCode
from system.models import BackgroundJob, PendingEvent
from system.release_gate_status import get_release_gate_report_status


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    message: str
    warning: bool = False


class Command(BaseCommand):
    help = "执行生产上线前自动预检，发现硬性失败时返回非 0"

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="将预警也视为失败")
        parser.add_argument(
            "--skip-release-gate-report",
            action="store_true",
            help="跳过最近发布门禁报告检查；供 release_gate 内部调用，避免检查当前尚未生成的报告",
        )

    def handle(self, *args, **options):
        checks = run_preflight_checks(check_release_gate_report=not options["skip_release_gate_report"])
        has_failure = any(not check.ok and not check.warning for check in checks)
        has_warning = any(not check.ok and check.warning for check in checks)

        for check in checks:
            if check.ok:
                label = self.style.SUCCESS("OK")
            elif check.warning:
                label = self.style.WARNING("WARN")
            else:
                label = self.style.ERROR("FAIL")
            self.stdout.write(f"[{label}] {check.name}: {check.message}")

        if has_failure or (options["strict"] and has_warning):
            raise CommandError("生产预检未通过")

        self.stdout.write(self.style.SUCCESS("生产预检通过"))


def run_preflight_checks(check_release_gate_report: bool = True) -> list[PreflightCheck]:
    checks = [
        _check_production_flags(),
        _check_database_connection(),
        _check_migrations_applied(),
        _check_directory_writable("附件目录", Path(settings.MEDIA_ROOT)),
        _check_directory_writable("备份目录", Path(getattr(settings, "ERP_BACKUP_DIR", settings.BASE_DIR / "backups"))),
        _check_directory_writable("日志目录", Path(getattr(settings, "LOG_DIR", settings.BASE_DIR / "logs"))),
        _check_storage_path_isolation(),
        _check_https_security_settings(),
        _check_static_root(),
        _check_initial_superuser(),
        _check_permission_admin_role(),
        _check_attachment_scan_configuration(),
        _check_recent_failed_jobs(),
        _check_stale_running_jobs(),
        _check_failed_pending_events(),
        _check_stale_running_pending_events(),
    ]
    if check_release_gate_report:
        checks.append(_check_release_gate_report())
    return checks


def _check_production_flags() -> PreflightCheck:
    if not getattr(settings, "IS_PRODUCTION", False):
        return PreflightCheck("生产环境标记", False, "DJANGO_ENV 不是 production/prod", warning=True)
    if settings.DEBUG:
        return PreflightCheck("生产环境标记", False, "DEBUG 仍为 True")
    if not settings.ALLOWED_HOSTS:
        return PreflightCheck("生产环境标记", False, "ALLOWED_HOSTS 为空")
    blocked_hosts = {"*", "localhost", "127.0.0.1", "testserver"}
    if any(host in blocked_hosts for host in settings.ALLOWED_HOSTS):
        return PreflightCheck("生产环境标记", False, "ALLOWED_HOSTS 包含开发或通配地址")
    return PreflightCheck("生产环境标记", True, "生产安全开关通过")


def _check_database_connection() -> PreflightCheck:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return PreflightCheck("数据库连接", False, str(exc))
    engine = connection.settings_dict.get("ENGINE", "")
    if getattr(settings, "IS_PRODUCTION", False) and "postgresql" not in engine:
        return PreflightCheck("数据库连接", False, f"生产环境数据库不是 PostgreSQL：{engine}")
    return PreflightCheck("数据库连接", True, engine)


def _check_migrations_applied() -> PreflightCheck:
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:
        return PreflightCheck("数据库迁移", False, str(exc))
    if plan:
        pending = ", ".join(f"{migration.app_label}.{migration.name}" for migration, _backward in plan[:5])
        suffix = " ..." if len(plan) > 5 else ""
        return PreflightCheck("数据库迁移", False, f"存在未应用迁移：{pending}{suffix}")
    return PreflightCheck("数据库迁移", True, "全部迁移已应用")


def _check_directory_writable(name: str, directory: Path) -> PreflightCheck:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".erp_preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        return PreflightCheck(name, False, str(exc))
    return PreflightCheck(name, True, str(directory))


def _check_storage_path_isolation() -> PreflightCheck:
    paths = {
        "附件目录": Path(settings.MEDIA_ROOT).resolve(),
        "静态文件目录": Path(settings.STATIC_ROOT).resolve(),
        "备份目录": Path(getattr(settings, "ERP_BACKUP_DIR", settings.BASE_DIR / "backups")).resolve(),
        "日志目录": Path(getattr(settings, "LOG_DIR", settings.BASE_DIR / "logs")).resolve(),
    }
    base_dir = Path(settings.BASE_DIR).resolve()

    labels = list(paths)
    for child_label in labels:
        child_path = paths[child_label]
        for parent_label in labels:
            if child_label == parent_label:
                continue
            parent_path = paths[parent_label]
            if _is_relative_to(child_path, parent_path) or child_path == parent_path:
                return PreflightCheck("目录隔离", False, f"{child_label}不能位于{parent_label}内：{child_path}")
    for label in ("备份目录", "日志目录"):
        path = paths[label]
        if getattr(settings, "IS_PRODUCTION", False) and (_is_relative_to(path, base_dir) or path == base_dir):
            return PreflightCheck("目录隔离", False, f"生产{label}不能位于应用代码目录内：{path}")
    return PreflightCheck("目录隔离", True, "附件、备份、日志和静态文件目录已隔离")


def _check_https_security_settings() -> PreflightCheck:
    if not getattr(settings, "IS_PRODUCTION", False):
        return PreflightCheck("HTTPS 安全配置", True, "非生产环境不强制检查")

    missing = []
    if not settings.SECURE_SSL_REDIRECT:
        missing.append("DJANGO_SECURE_SSL_REDIRECT")
    if not settings.SESSION_COOKIE_SECURE:
        missing.append("DJANGO_SESSION_COOKIE_SECURE")
    if not settings.CSRF_COOKIE_SECURE:
        missing.append("DJANGO_CSRF_COOKIE_SECURE")
    if not settings.CSRF_TRUSTED_ORIGINS:
        missing.append("DJANGO_CSRF_TRUSTED_ORIGINS")
    if settings.SECURE_HSTS_SECONDS < 3600:
        missing.append("DJANGO_SECURE_HSTS_SECONDS")

    if missing:
        return PreflightCheck(
            "HTTPS 安全配置",
            False,
            "生产 HTTPS 安全项未完整启用：" + ", ".join(missing),
            warning=True,
        )
    return PreflightCheck("HTTPS 安全配置", True, "HTTPS、Secure Cookie、CSRF 和 HSTS 配置通过")


def _check_static_root() -> PreflightCheck:
    static_root = Path(settings.STATIC_ROOT)
    if not static_root.exists():
        return PreflightCheck("静态文件目录", False, f"{static_root} 不存在，请先执行 collectstatic", warning=True)
    if not any(static_root.iterdir()):
        return PreflightCheck("静态文件目录", False, f"{static_root} 为空，请先执行 collectstatic", warning=True)
    return PreflightCheck("静态文件目录", True, str(static_root))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _check_initial_superuser() -> PreflightCheck:
    exists = User.objects.filter(
        is_superuser=True,
        is_active=True,
        is_deleted=False,
        status=User.AccountStatus.ACTIVE,
    ).exists()
    if not exists:
        return PreflightCheck("初始超级管理员", False, "未找到启用状态的超级管理员")
    return PreflightCheck("初始超级管理员", True, "已存在启用状态超级管理员")


def _check_permission_admin_role() -> PreflightCheck:
    exists = Role.objects.filter(
        status=Role.RoleStatus.ACTIVE,
        permissions__permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE,
        users__is_active=True,
        users__is_deleted=False,
        users__status=User.AccountStatus.ACTIVE,
    ).exists()
    if not exists:
        return PreflightCheck("权限管理员角色", False, "未找到分配给启用用户的权限管理员角色")
    return PreflightCheck("权限管理员角色", True, "权限管理员角色已分配")


def _check_attachment_scan_configuration() -> PreflightCheck:
    scan_command = getattr(settings, "ERP_ATTACHMENT_SCAN_COMMAND", "").strip()
    if scan_command:
        return PreflightCheck("附件安全扫描", True, "已配置扫描命令")
    risk_acceptor = getattr(settings, "ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY", "").strip()
    if risk_acceptor:
        return PreflightCheck("附件安全扫描", True, f"未配置扫描命令，已由 {risk_acceptor} 接受风险")
    return PreflightCheck("附件安全扫描", False, "未配置扫描命令，需记录风险接受人", warning=True)


def _check_recent_failed_jobs() -> PreflightCheck:
    failed_count = BackgroundJob.objects.filter(status=BackgroundJob.JobStatus.FAILED).count()
    if failed_count:
        return PreflightCheck("失败后台任务", False, f"存在 {failed_count} 条失败后台任务", warning=True)
    return PreflightCheck("失败后台任务", True, "无失败后台任务")


def _check_stale_running_jobs() -> PreflightCheck:
    timeout_minutes = max(1, int(getattr(settings, "ERP_BACKGROUND_JOB_RUNNING_TIMEOUT_MINUTES", 120)))
    stale_before = timezone.now() - timedelta(minutes=timeout_minutes)
    stale_count = BackgroundJob.objects.filter(
        status=BackgroundJob.JobStatus.RUNNING,
        started_at__lte=stale_before,
    ).count()
    if stale_count:
        return PreflightCheck(
            "卡住后台任务",
            False,
            f"存在 {stale_count} 条运行超过 {timeout_minutes} 分钟的后台任务，请检查后重新执行对应命令",
            warning=True,
        )
    return PreflightCheck("卡住后台任务", True, f"无超过 {timeout_minutes} 分钟的运行中任务")


def _check_failed_pending_events() -> PreflightCheck:
    failed_count = PendingEvent.objects.filter(status=PendingEvent.EventStatus.FAILED).count()
    if failed_count:
        return PreflightCheck("失败事务后事件", False, f"存在 {failed_count} 条失败事务后事件", warning=True)
    return PreflightCheck("失败事务后事件", True, "无失败事务后事件")


def _check_stale_running_pending_events() -> PreflightCheck:
    timeout_minutes = max(1, int(getattr(settings, "ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES", 30)))
    stale_before = timezone.now() - timedelta(minutes=timeout_minutes)
    stale_count = PendingEvent.objects.filter(
        status=PendingEvent.EventStatus.RUNNING,
        updated_at__lte=stale_before,
    ).count()
    if stale_count:
        return PreflightCheck(
            "卡住事务后事件",
            False,
            f"存在 {stale_count} 条处理中超过 {timeout_minutes} 分钟的事务后事件，请执行 process_pending_events 回收处理",
            warning=True,
        )
    return PreflightCheck("卡住事务后事件", True, f"无超过 {timeout_minutes} 分钟的处理中事件")


def _check_release_gate_report() -> PreflightCheck:
    status = get_release_gate_report_status()
    if not status.exists:
        return PreflightCheck("发布门禁报告", False, status.message, warning=True)
    if not status.ok:
        return PreflightCheck("发布门禁报告", False, status.message)
    if not status.fresh:
        return PreflightCheck("发布门禁报告", False, status.message, warning=True)
    return PreflightCheck("发布门禁报告", True, status.message)
