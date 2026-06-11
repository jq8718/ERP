from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from system.management.commands.production_preflight import PreflightCheck, run_preflight_checks
from system.management.commands.simulate_production_settings import run_simulated_production_check
from system.models import BackgroundJob, Backup, PendingEvent, ReleaseRecord
from system.release_gate_status import ReleaseGateReportStatus, get_release_gate_report_status

REQUIRED_FULL_GATE_STEPS = (
    "Django 系统检查",
    "Django 生产安全检查",
    "URL 引用完整性检查",
    "模板语法检查",
    "权限配置检查",
    "权限引用完整性检查",
    "路由保护检查",
    "CSRF 表单检查",
    "导航页面烟测",
    "低频入口烟测",
    "迁移一致性检查",
    "Python 依赖检查",
    "业务冒烟测试",
    "完整自动测试",
    "生产严格预检",
)


@dataclass(frozen=True)
class PrelaunchReportResult:
    path: Path
    ready: bool
    hard_failure_count: int
    warning_count: int


class Command(BaseCommand):
    help = "生成预上线验收报告，汇总发布门禁、生产预检和运维状态"

    def add_arguments(self, parser):
        parser.add_argument(
            "--report-file",
            default="docs/prelaunch-acceptance-report.md",
            help="输出 Markdown 验收报告路径，默认 docs/prelaunch-acceptance-report.md",
        )
        parser.add_argument("--bootstrap-username", default="admin", help="初始化管理员用户名，默认 admin")
        parser.add_argument("--bootstrap-role-code", default="permission-admin", help="权限管理员角色编码")
        parser.add_argument("--host", default="erp.example.com", help="生产配置模拟使用的域名")
        parser.add_argument("--release-version", default="", help="本次发布版本号；填写后会校验发布记录是否存在")
        parser.add_argument("--deployment-runbook-file", default="", help="部署命令清单归档文件；填写后会校验文件内容")
        parser.add_argument("--strict", action="store_true", help="存在失败或预警时返回非 0，适合上线脚本")

    def handle(self, *args, **options):
        result = write_prelaunch_report(
            options["report_file"],
            bootstrap_username=options["bootstrap_username"].strip(),
            bootstrap_role_code=options["bootstrap_role_code"].strip(),
            host=options["host"].strip(),
            release_version=options["release_version"].strip(),
            deployment_runbook_file=options["deployment_runbook_file"].strip(),
        )
        self.stdout.write(f"预上线验收报告已生成：{result.path}")
        if result.ready:
            self.stdout.write(self.style.SUCCESS("预上线验收状态：通过"))
            return

        self.stdout.write(
            self.style.WARNING(
                f"预上线验收状态：需处理，失败 {result.hard_failure_count} 项，预警 {result.warning_count} 项"
            )
        )
        if options["strict"]:
            raise CommandError("预上线验收未通过")


def write_prelaunch_report(
    report_file: str | Path,
    bootstrap_username: str = "admin",
    bootstrap_role_code: str = "permission-admin",
    host: str = "erp.example.com",
    release_version: str = "",
    deployment_runbook_file: str | Path = "",
) -> PrelaunchReportResult:
    report_path = _resolve_report_path(report_file)
    release_gate_status = get_release_gate_report_status()
    simulated_production_check = _run_simulated_production_check(host)
    bootstrap_check = _run_bootstrap_check(bootstrap_username, bootstrap_role_code)
    preflight_checks = run_preflight_checks(check_release_gate_report=False)
    operational_checks = _run_operational_checks(
        release_version=release_version,
        deployment_runbook_file=deployment_runbook_file,
    )
    hard_failures, warnings = _count_findings(
        release_gate_status,
        simulated_production_check,
        bootstrap_check,
        preflight_checks,
        operational_checks,
    )
    ready = hard_failures == 0 and warnings == 0

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_report(
            release_gate_status=release_gate_status,
            simulated_production_check=simulated_production_check,
            bootstrap_check=bootstrap_check,
            preflight_checks=preflight_checks,
            operational_checks=operational_checks,
            release_version=release_version,
            deployment_runbook_file=str(deployment_runbook_file),
            ready=ready,
            hard_failure_count=hard_failures,
            warning_count=warnings,
        ),
        encoding="utf-8",
    )
    return PrelaunchReportResult(
        path=report_path,
        ready=ready,
        hard_failure_count=hard_failures,
        warning_count=warnings,
    )


def _resolve_report_path(report_file: str | Path) -> Path:
    path = Path(report_file)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return path


def _count_findings(
    release_gate_status: ReleaseGateReportStatus,
    simulated_production_check: PreflightCheck,
    bootstrap_check: PreflightCheck,
    preflight_checks: list[PreflightCheck],
    operational_checks: list[PreflightCheck],
) -> tuple[int, int]:
    hard_failures = 0
    warnings = 0

    if not release_gate_status.exists:
        warnings += 1
    elif not release_gate_status.ok:
        hard_failures += 1
        if not release_gate_status.fresh:
            warnings += 1
    elif not release_gate_status.fresh:
        warnings += 1
    elif not _release_gate_is_full(release_gate_status):
        warnings += 1

    if not simulated_production_check.ok:
        if simulated_production_check.warning:
            warnings += 1
        else:
            hard_failures += 1

    if not bootstrap_check.ok:
        if bootstrap_check.warning:
            warnings += 1
        else:
            hard_failures += 1

    for check in preflight_checks:
        if check.ok:
            continue
        if check.warning:
            warnings += 1
        else:
            hard_failures += 1
    for check in operational_checks:
        if check.ok:
            continue
        if check.warning:
            warnings += 1
        else:
            hard_failures += 1
    return hard_failures, warnings


def _run_operational_checks(release_version: str = "", deployment_runbook_file: str | Path = "") -> list[PreflightCheck]:
    latest_successful_backup = Backup.objects.filter(status=Backup.BackupStatus.SUCCESS).order_by("-created_at").first()
    latest_successful_restore_drill = (
        BackgroundJob.objects.filter(job_type="restore_drill", status=BackgroundJob.JobStatus.SUCCESS)
        .order_by("-created_at")
        .first()
    )
    latest_successful_backup_verify = (
        BackgroundJob.objects.filter(job_type="backup_verify", status=BackgroundJob.JobStatus.SUCCESS)
        .order_by("-created_at")
        .first()
    )
    latest_successful_event_process = (
        BackgroundJob.objects.filter(job_type="process_pending_events", status=BackgroundJob.JobStatus.SUCCESS)
        .order_by("-created_at")
        .first()
    )
    latest_release = ReleaseRecord.objects.order_by("-released_at").first()
    required_release = ReleaseRecord.objects.filter(version_no=release_version).first() if release_version else None
    now = timezone.now()
    backup_max_age_hours = int(getattr(settings, "ERP_PRELAUNCH_BACKUP_MAX_AGE_HOURS", 24))
    backup_verify_max_age_hours = int(getattr(settings, "ERP_PRELAUNCH_BACKUP_VERIFY_MAX_AGE_HOURS", 24))
    restore_max_age_hours = int(getattr(settings, "ERP_PRELAUNCH_RESTORE_DRILL_MAX_AGE_HOURS", 168))
    event_process_max_age_minutes = int(getattr(settings, "ERP_PRELAUNCH_EVENT_PROCESS_MAX_AGE_MINUTES", 30))

    checks = []
    checks.append(_check_deployment_runbook_file(deployment_runbook_file, release_version))
    if latest_successful_backup:
        age_hours = (now - latest_successful_backup.created_at).total_seconds() / 3600
        if age_hours > backup_max_age_hours:
            checks.append(
                PreflightCheck(
                    "最近成功备份",
                    False,
                    f"最近成功备份已超过 {backup_max_age_hours} 小时：{_backup_summary(latest_successful_backup)}",
                    warning=True,
                )
            )
        else:
            checks.append(PreflightCheck("最近成功备份", True, _backup_summary(latest_successful_backup)))
    else:
        checks.append(PreflightCheck("最近成功备份", False, "未找到成功备份记录，上线前至少执行一次 backup_daily", warning=True))

    if latest_successful_backup_verify:
        evidence_time = latest_successful_backup_verify.finished_at or latest_successful_backup_verify.created_at
        age_hours = (now - evidence_time).total_seconds() / 3600
        if age_hours > backup_verify_max_age_hours:
            checks.append(
                PreflightCheck(
                    "最近备份校验",
                    False,
                    f"最近成功备份校验已超过 {backup_verify_max_age_hours} 小时：{_job_summary(latest_successful_backup_verify)}",
                    warning=True,
                )
            )
        elif latest_successful_backup and evidence_time < latest_successful_backup.created_at:
            checks.append(
                PreflightCheck(
                    "最近备份校验",
                    False,
                    "最近成功备份校验早于最近成功备份，上线前需要重新执行 verify_backups",
                    warning=True,
                )
            )
        else:
            checks.append(PreflightCheck("最近备份校验", True, _job_summary(latest_successful_backup_verify)))
    else:
        checks.append(PreflightCheck("最近备份校验", False, "未找到成功备份校验记录，上线前至少执行一次 verify_backups", warning=True))

    if latest_successful_restore_drill:
        evidence_time = latest_successful_restore_drill.finished_at or latest_successful_restore_drill.created_at
        age_hours = (now - evidence_time).total_seconds() / 3600
        if age_hours > restore_max_age_hours:
            checks.append(
                PreflightCheck(
                    "最近恢复演练",
                    False,
                    f"最近成功恢复演练已超过 {restore_max_age_hours} 小时：{_job_summary(latest_successful_restore_drill)}",
                    warning=True,
                )
            )
        elif latest_successful_backup and evidence_time < latest_successful_backup.created_at:
            checks.append(
                PreflightCheck(
                    "最近恢复演练",
                    False,
                    "最近成功恢复演练早于最近成功备份，上线前需要重新执行 restore_drill",
                    warning=True,
                )
            )
        else:
            checks.append(PreflightCheck("最近恢复演练", True, _job_summary(latest_successful_restore_drill)))
    else:
        checks.append(PreflightCheck("最近恢复演练", False, "未找到成功恢复演练记录，上线前至少执行一次 restore_drill", warning=True))

    event_backlog_count = PendingEvent.objects.filter(
        status__in=[
            PendingEvent.EventStatus.PENDING,
            PendingEvent.EventStatus.RUNNING,
            PendingEvent.EventStatus.FAILED,
        ]
    ).count()
    if event_backlog_count:
        checks.append(
            PreflightCheck(
                "事务后事件处理",
                False,
                f"存在 {event_backlog_count} 条待处理、处理中或失败事务后事件，上线前需要处理清空",
                warning=True,
            )
        )
    elif latest_successful_event_process:
        evidence_time = latest_successful_event_process.finished_at or latest_successful_event_process.created_at
        age_minutes = (now - evidence_time).total_seconds() / 60
        if age_minutes > event_process_max_age_minutes:
            checks.append(
                PreflightCheck(
                    "事务后事件处理",
                    False,
                    f"最近事务后事件处理已超过 {event_process_max_age_minutes} 分钟：{_job_summary(latest_successful_event_process)}",
                    warning=True,
                )
            )
        else:
            checks.append(PreflightCheck("事务后事件处理", True, _job_summary(latest_successful_event_process)))
    else:
        checks.append(
            PreflightCheck(
                "事务后事件处理",
                False,
                "未找到成功事务后事件处理记录，上线前至少执行一次 process_pending_events",
                warning=True,
            )
        )

    if release_version and required_release is None:
        checks.append(PreflightCheck("发布记录", False, f"未找到本次发布版本记录：{release_version}", warning=True))
    elif release_version:
        checks.append(PreflightCheck("发布记录", True, _release_summary(required_release)))
    elif latest_release:
        checks.append(PreflightCheck("发布记录", True, _release_summary(latest_release)))
    else:
        checks.append(PreflightCheck("发布记录", False, "未找到发布记录，上线前执行 record_release 登记版本", warning=True))
    return checks


def _check_deployment_runbook_file(deployment_runbook_file: str | Path, release_version: str = "") -> PreflightCheck:
    if not deployment_runbook_file:
        return PreflightCheck("部署命令清单", False, "未指定部署命令清单归档文件，上线前建议执行 deployment_runbook --output-file", warning=True)

    path = Path(deployment_runbook_file)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    if not path.exists():
        return PreflightCheck("部署命令清单", False, f"部署命令清单不存在：{path}", warning=True)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return PreflightCheck("部署命令清单", False, f"部署命令清单无法读取：{exc}", warning=True)

    missing = []
    if "# ERP 生产部署命令清单" not in content:
        missing.append("标题")
    required_commands = [
        ("release_gate 命令", "release_gate"),
        ("backup_daily 命令", "backup_daily"),
        ("verify_backups 命令", "verify_backups"),
        ("restore_drill 命令", "restore_drill"),
        ("process_pending_events 命令", "process_pending_events"),
        ("business_smoke_test 命令", "business_smoke_test"),
        ("record_release 命令", "record_release"),
        ("prelaunch_report 命令", "prelaunch_report"),
    ]
    for label, marker in required_commands:
        if marker not in content:
            missing.append(label)
    if release_version and release_version not in content:
        missing.append("本次发布版本号")
    if missing:
        return PreflightCheck("部署命令清单", False, "部署命令清单内容不完整：" + "、".join(missing), warning=True)
    return PreflightCheck("部署命令清单", True, str(path))


def _run_simulated_production_check(host: str) -> PreflightCheck:
    if not host:
        return PreflightCheck("生产配置模拟", False, "未指定生产域名", warning=True)
    result = run_simulated_production_check(host=host)
    if result["ok"]:
        return PreflightCheck("生产配置模拟", True, result["message"])
    return PreflightCheck("生产配置模拟", False, result["message"])


def _run_bootstrap_check(username: str, role_code: str) -> PreflightCheck:
    if not username:
        return PreflightCheck("初始化管理员", False, "未指定初始化管理员用户名", warning=True)
    try:
        call_command(
            "bootstrap_admin",
            username=username,
            role_code=role_code or "permission-admin",
            check_only=True,
            stdout=StringIO(),
        )
    except CommandError as exc:
        return PreflightCheck("初始化管理员", False, str(exc))
    return PreflightCheck("初始化管理员", True, f"{username} / {role_code or 'permission-admin'} 已通过 check-only")


def _render_report(
    release_gate_status: ReleaseGateReportStatus,
    simulated_production_check: PreflightCheck,
    bootstrap_check: PreflightCheck,
    preflight_checks: list[PreflightCheck],
    operational_checks: list[PreflightCheck],
    release_version: str,
    deployment_runbook_file: str,
    ready: bool,
    hard_failure_count: int,
    warning_count: int,
) -> str:
    generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    latest_backup = Backup.objects.order_by("-created_at").first()
    latest_job = BackgroundJob.objects.order_by("-created_at").first()
    latest_release = ReleaseRecord.objects.order_by("-released_at").first()
    failed_jobs = BackgroundJob.objects.filter(status=BackgroundJob.JobStatus.FAILED).count()
    failed_events = PendingEvent.objects.filter(status=PendingEvent.EventStatus.FAILED).count()
    pending_events = PendingEvent.objects.filter(status=PendingEvent.EventStatus.PENDING).count()

    lines = [
        "# ERP 预上线验收报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 总体结果：{'通过' if ready else '需处理'}",
        f"- 硬性失败：{hard_failure_count}",
        f"- 预警：{warning_count}",
        f"- 当前运行环境：{getattr(settings, 'DJANGO_ENV', 'unknown')}",
        f"- 生产模式：{'是' if getattr(settings, 'IS_PRODUCTION', False) else '否'}",
        f"- 本次发布版本：{release_version or '-'}",
        f"- 部署命令清单：{deployment_runbook_file or '-'}",
        "",
        "## 结论说明",
        "",
        _environment_note(
            ready,
            release_gate_status,
            bootstrap_check,
            preflight_checks,
            operational_checks,
        ),
        "",
        "## 发布门禁",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 报告路径 | `{_escape_markdown_cell(str(release_gate_status.path))}` |",
        f"| 报告存在 | {'是' if release_gate_status.exists else '否'} |",
        f"| 最近结果 | {_escape_markdown_cell(release_gate_status.overall_result or '-')} |",
        f"| 是否未过期 | {'是' if release_gate_status.fresh else '否'} |",
        f"| 检查步骤数 | {release_gate_status.step_count or '-'} |",
        f"| 是否完整上线门禁 | {'是' if _release_gate_is_full(release_gate_status) else '否'} |",
        f"| 缺失门禁步骤 | {_escape_markdown_cell('、'.join(_missing_full_gate_steps(release_gate_status)) or '-')} |",
        f"| 说明 | {_escape_markdown_cell(release_gate_status.message)} |",
        "",
        "## 生产配置模拟",
        "",
        "| 检查项 | 级别 | 结果 | 说明 |",
        "| --- | --- | --- | --- |",
        _check_row(simulated_production_check),
        "",
        "## 初始化验收",
        "",
        "| 检查项 | 级别 | 结果 | 说明 |",
        "| --- | --- | --- | --- |",
        _check_row(bootstrap_check),
        "",
        "## 生产预检",
        "",
        "| 检查项 | 级别 | 结果 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for check in preflight_checks:
        lines.append(_check_row(check))

    lines.extend(
        [
            "",
            "## 运维状态",
            "",
            "| 项目 | 状态 |",
            "| --- | --- |",
            f"| 最近备份 | {_backup_summary(latest_backup)} |",
            f"| 最近后台任务 | {_job_summary(latest_job)} |",
            f"| 失败后台任务 | {failed_jobs} |",
            f"| 待处理事务后事件 | {pending_events} |",
            f"| 失败事务后事件 | {failed_events} |",
            f"| 最近发布记录 | {_release_summary(latest_release)} |",
            "",
            "## 运维证据验收",
            "",
            "| 检查项 | 级别 | 结果 | 说明 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for check in operational_checks:
        lines.append(_check_row(check))

    lines.extend(
        [
            "",
            "## 后续动作",
            "",
        ]
    )
    if ready:
        lines.append("- 可以进入正式上线签字和部署窗口。")
    else:
        lines.extend(
            _next_actions(
                release_gate_status,
                simulated_production_check,
                bootstrap_check,
                preflight_checks,
                operational_checks,
            )
        )
        lines.append("- 处理后重新执行 `python manage.py release_gate --include-deploy-check --include-tests --include-production-preflight --report-file docs/latest-release-gate-report.md`。")
        lines.append("- 处理后重新执行 `python manage.py prelaunch_report --strict --bootstrap-username <用户名> --release-version <版本号> --deployment-runbook-file docs/deployment-runbook-<版本号>.md`。")
    return "\n".join(lines) + "\n"


def _environment_note(
    ready: bool,
    release_gate_status: ReleaseGateReportStatus,
    bootstrap_check: PreflightCheck,
    preflight_checks: list[PreflightCheck],
    operational_checks: list[PreflightCheck],
) -> str:
    if ready:
        return "- 当前报告未发现失败或预警，可作为上线签字证据。"
    if not getattr(settings, "IS_PRODUCTION", False):
        pending_items = _pending_summary_items(release_gate_status, bootstrap_check, preflight_checks, operational_checks)
        pending_text = "、".join(pending_items) if pending_items else "生产环境复核"
        return f"- 当前报告运行在非生产环境，剩余待处理项：{pending_text}；正式上线前必须在生产环境完成这些项目并重新执行 `prelaunch_report --strict`。"
    return "- 当前报告运行在生产模式，所有失败和预警都必须处理或形成风险接受记录后才能上线。"


def _pending_summary_items(
    release_gate_status: ReleaseGateReportStatus,
    bootstrap_check: PreflightCheck,
    preflight_checks: list[PreflightCheck],
    operational_checks: list[PreflightCheck],
) -> list[str]:
    items: list[str] = []
    if not release_gate_status.exists or not release_gate_status.ok or not release_gate_status.fresh or not _release_gate_is_full(release_gate_status):
        items.append("完整上线门禁")
    if not bootstrap_check.ok:
        items.append("初始化管理员")
    items.extend(check.name for check in preflight_checks if not check.ok)
    items.extend(check.name for check in operational_checks if not check.ok)
    return list(dict.fromkeys(items))


def _next_actions(
    release_gate_status: ReleaseGateReportStatus,
    simulated_production_check: PreflightCheck,
    bootstrap_check: PreflightCheck,
    preflight_checks: list[PreflightCheck],
    operational_checks: list[PreflightCheck],
) -> list[str]:
    actions = []
    if not release_gate_status.exists or not release_gate_status.ok or not release_gate_status.fresh or not _release_gate_is_full(release_gate_status):
        actions.append(
            "- 重新执行 `python manage.py release_gate --include-deploy-check --include-tests --include-production-preflight --report-file docs/latest-release-gate-report.md`，确保发布门禁报告通过且未过期。"
        )
    if not simulated_production_check.ok:
        actions.append("- 执行 `python manage.py simulate_production_settings --host <正式域名>`，确保生产环境变量组合和 `check --deploy` 通过。")
    if not bootstrap_check.ok:
        actions.append(
            "- 执行 `python manage.py bootstrap_admin --username <用户名> --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput` 初始化管理员，然后执行 `python manage.py bootstrap_admin --username <用户名> --check-only` 验证。"
        )

    failed_names = {check.name for check in preflight_checks if not check.ok}
    if "生产环境标记" in failed_names:
        actions.append("- 设置生产环境变量：`DJANGO_ENV=production`、`DJANGO_DEBUG=false`、正式域名和 PostgreSQL 配置。")
    if "静态文件目录" in failed_names:
        actions.append("- 执行 `python manage.py collectstatic --noinput`，并确认 `STATIC_ROOT` 可由反向代理访问。")
    if "权限管理员角色" in failed_names and bootstrap_check.ok:
        actions.append("- 检查至少一个启用用户拥有权限管理员角色，并确认角色包含 `admin.permission_manage`。")
    if "附件安全扫描" in failed_names:
        actions.append("- 配置 `ERP_ATTACHMENT_SCAN_COMMAND`，或在验收记录中写明附件扫描风险接受人。")
    if "失败后台任务" in failed_names:
        actions.append("- 处理 `/background-jobs/` 中的失败后台任务，确认失败原因已解决。")
    if "失败事务后事件" in failed_names:
        actions.append("- 处理失败事务后事件，并重新执行 `python manage.py process_pending_events`。")

    failed_operational_names = {check.name for check in operational_checks if not check.ok}
    if "部署命令清单" in failed_operational_names:
        actions.append("- 执行 `python manage.py deployment_runbook --host <正式域名> --operator <用户名> --release-version <版本号> --output-file docs/deployment-runbook-<版本号>.md` 生成部署命令清单归档。")
    if "最近成功备份" in failed_operational_names:
        actions.append("- 执行 `python manage.py backup_daily`，确认生成成功备份记录。")
    if "最近备份校验" in failed_operational_names:
        actions.append("- 执行 `python manage.py verify_backups`，确认最近备份文件存在、校验值和压缩包可读。")
    if "最近恢复演练" in failed_operational_names:
        actions.append("- 执行 `python manage.py restore_drill`，确认最近一次恢复演练成功。")
    if "事务后事件处理" in failed_operational_names:
        actions.append("- 执行 `python manage.py process_pending_events`，确认没有待处理、处理中或失败事务后事件积压。")
    if "发布记录" in failed_operational_names:
        actions.append("- 执行 `python manage.py record_release <版本号> --summary \"<发布摘要>\" --released-by <用户名>` 登记本次发布记录；若使用 `prelaunch_report --release-version`，版本号必须一致。")
    if not actions:
        actions.append("- 先处理报告中的硬性失败和预警。")
    return actions


def _check_row(check: PreflightCheck) -> str:
    if check.ok:
        level = "OK"
        result = "通过"
    elif check.warning:
        level = "WARN"
        result = "预警"
    else:
        level = "FAIL"
        result = "失败"
    return "| {name} | {level} | {result} | {message} |".format(
        name=_escape_markdown_cell(check.name),
        level=level,
        result=result,
        message=_escape_markdown_cell(check.message),
    )


def _backup_summary(backup: Backup | None) -> str:
    if backup is None:
        return "暂无"
    return _escape_markdown_cell(f"{backup.backup_no} / {backup.get_status_display()} / {timezone.localtime(backup.created_at):%Y-%m-%d %H:%M:%S}")


def _release_gate_is_full(release_gate_status: ReleaseGateReportStatus) -> bool:
    return bool(
        release_gate_status.exists
        and release_gate_status.ok
        and not _missing_full_gate_steps(release_gate_status)
    )


def _missing_full_gate_steps(release_gate_status: ReleaseGateReportStatus) -> list[str]:
    existing = set(release_gate_status.step_names)
    return [step for step in REQUIRED_FULL_GATE_STEPS if step not in existing]


def _job_summary(job: BackgroundJob | None) -> str:
    if job is None:
        return "暂无"
    finished_at = timezone.localtime(job.finished_at).strftime("%Y-%m-%d %H:%M:%S") if job.finished_at else "-"
    return _escape_markdown_cell(f"{job.job_no} / {job.job_type} / {job.get_status_display()} / {finished_at}")


def _release_summary(release: ReleaseRecord | None) -> str:
    if release is None:
        return "暂无"
    released_at = timezone.localtime(release.released_at).strftime("%Y-%m-%d %H:%M:%S")
    return _escape_markdown_cell(f"{release.version_no} / {released_at}")


def _escape_markdown_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
