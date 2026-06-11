from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "生成生产部署命令清单，供上线执行和归档"

    def add_arguments(self, parser):
        parser.add_argument("--host", required=True, help="正式访问域名，例如 erp.example.com")
        parser.add_argument("--operator", required=True, help="上线执行人或冒烟测试操作人用户名")
        parser.add_argument("--release-version", required=True, help="本次发布版本号，例如 2026.06.11.1")
        parser.add_argument("--summary", default="生产发布", help="发布摘要")
        parser.add_argument("--bootstrap-username", default="", help="初始化管理员用户名，默认使用 --operator")
        parser.add_argument("--output-file", default="", help="输出命令清单文件路径；为空时只打印到控制台")
        parser.add_argument("--windows", action="store_true", help="输出 Windows PowerShell 风格命令")

    def handle(self, *args, **options):
        host = options["host"].strip()
        operator = options["operator"].strip()
        version = options["release_version"].strip()
        summary = options["summary"].strip() or "生产发布"
        bootstrap_username = options["bootstrap_username"].strip() or operator

        if not host:
            raise CommandError("正式访问域名不能为空")
        if not operator:
            raise CommandError("上线执行人不能为空")
        if not version:
            raise CommandError("发布版本号不能为空")

        commands = build_deployment_runbook(
            host=host,
            operator=operator,
            version=version,
            summary=summary,
            bootstrap_username=bootstrap_username,
            windows=options["windows"],
            deployment_runbook_file=options["output_file"].strip(),
        )
        if options["output_file"].strip():
            output_path = _resolve_output_path(options["output_file"].strip())
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("\n".join(commands) + "\n", encoding="utf-8")
            self.stdout.write(f"部署命令清单已生成：{output_path}")
            return
        for line in commands:
            self.stdout.write(line)


def build_deployment_runbook(
    host: str,
    operator: str,
    version: str,
    summary: str,
    bootstrap_username: str,
    windows: bool = False,
    deployment_runbook_file: str = "",
) -> list[str]:
    python = ".\\.venv\\Scripts\\python" if windows else "python"
    report_gate = "docs\\latest-release-gate-report.md" if windows else "docs/latest-release-gate-report.md"
    report_prelaunch = "docs\\prelaunch-acceptance-report.md" if windows else "docs/prelaunch-acceptance-report.md"
    prelaunch_parts = [
        python,
        "manage.py",
        "prelaunch_report",
        "--strict",
        "--bootstrap-username",
        bootstrap_username,
        "--release-version",
        version,
        "--report-file",
        report_prelaunch,
    ]
    if deployment_runbook_file:
        prelaunch_parts.extend(["--deployment-runbook-file", deployment_runbook_file])

    return [
        "# ERP 生产部署命令清单",
        f"# host={host}",
        f"# operator={operator}",
        f"# version={version}",
        "",
        _cmd([python, "manage.py", "migrate"], windows),
        _cmd([python, "manage.py", "collectstatic", "--noinput"], windows),
        _cmd([python, "manage.py", "bootstrap_admin", "--username", bootstrap_username, "--password-env", "ERP_BOOTSTRAP_ADMIN_PASSWORD", "--noinput"], windows),
        _cmd([python, "manage.py", "bootstrap_admin", "--username", bootstrap_username, "--check-only"], windows),
        _cmd([python, "manage.py", "simulate_production_settings", "--host", host], windows),
        _cmd([python, "manage.py", "release_gate", "--operator", operator, "--include-deploy-check", "--include-tests", "--include-production-preflight", "--report-file", report_gate], windows),
        _cmd([python, "manage.py", "backup_daily"], windows),
        _cmd([python, "manage.py", "verify_backups"], windows),
        _cmd([python, "manage.py", "restore_drill"], windows),
        _cmd([python, "manage.py", "process_pending_events"], windows),
        _cmd([python, "manage.py", "business_smoke_test", "--operator", operator], windows),
        _cmd([python, "manage.py", "record_release", version, "--summary", summary, "--released-by", operator], windows),
        _cmd(prelaunch_parts, windows),
    ]


def _cmd(parts: list[str], windows: bool) -> str:
    if windows:
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _resolve_output_path(output_file: str) -> Path:
    path = Path(output_file)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return path
