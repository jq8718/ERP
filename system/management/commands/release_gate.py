from __future__ import annotations

import subprocess
import sys
import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


@dataclass
class GateStep:
    name: str
    command: list[str]


class Command(BaseCommand):
    help = "执行发布前门禁检查，默认覆盖系统检查、迁移一致性、依赖检查和业务冒烟"

    def add_arguments(self, parser):
        parser.add_argument("--operator", default="", help="业务冒烟测试执行人用户名")
        parser.add_argument("--include-deploy-check", action="store_true", help="同时执行 Django check --deploy --fail-level WARNING")
        parser.add_argument("--include-tests", action="store_true", help="同时执行完整自动测试")
        parser.add_argument("--include-production-preflight", action="store_true", help="同时执行 production_preflight --strict")
        parser.add_argument("--fail-fast", action="store_true", help="遇到第一个失败步骤时立即停止")
        parser.add_argument("--timeout-seconds", type=int, default=900, help="单个步骤最长执行秒数，默认 900")
        parser.add_argument("--report-file", default="", help="输出 Markdown 门禁报告文件路径")

    def handle(self, *args, **options):
        steps = _build_gate_steps(
            operator=options["operator"].strip(),
            include_deploy_check=options["include_deploy_check"],
            include_tests=options["include_tests"],
            include_production_preflight=options["include_production_preflight"],
        )

        failures = []
        results = []
        for step in steps:
            self.stdout.write(f"[RUN] {step.name}")
            result = _run_step(step, timeout_seconds=options["timeout_seconds"])
            results.append(result)
            if result["ok"]:
                self.stdout.write(self.style.SUCCESS(f"[OK] {step.name}: {result['message']}"))
            else:
                failures.append(result)
                self.stdout.write(self.style.ERROR(f"[FAIL] {step.name}: {result['message']}"))
                if options["fail_fast"]:
                    break

        if failures:
            summary = {
                "total": len(results),
                "failed": len(failures),
                "failed_steps": [failure["name"] for failure in failures],
            }
            if options["report_file"]:
                report_path = _write_report(options["report_file"], results, passed=False)
                self.stdout.write(f"门禁报告已生成：{report_path}")
            raise CommandError(f"发布前门禁检查未通过：{summary}")

        if options["report_file"]:
            report_path = _write_report(options["report_file"], results, passed=True)
            self.stdout.write(f"门禁报告已生成：{report_path}")
        self.stdout.write(self.style.SUCCESS(f"发布前门禁检查通过：{len(results)} 个步骤全部通过"))


def _build_gate_steps(
    operator: str = "",
    include_deploy_check: bool = False,
    include_tests: bool = False,
    include_production_preflight: bool = False,
) -> list[GateStep]:
    manage_py = str(Path(settings.BASE_DIR) / "manage.py")
    steps = [
        GateStep("Django 系统检查", [sys.executable, manage_py, "check"]),
    ]
    if include_deploy_check:
        steps.append(GateStep("Django 生产安全检查", [sys.executable, manage_py, "check", "--deploy", "--fail-level", "WARNING"]))
    steps.extend(
        [
            GateStep("URL 引用完整性检查", [sys.executable, manage_py, "check_url_references"]),
            GateStep("模板语法检查", [sys.executable, manage_py, "check_templates"]),
            GateStep("权限配置检查", [sys.executable, manage_py, "check_permissions"]),
            GateStep("权限引用完整性检查", [sys.executable, manage_py, "check_permission_references"]),
            GateStep("路由保护检查", [sys.executable, manage_py, "check_route_protection"]),
            GateStep("CSRF 表单检查", [sys.executable, manage_py, "check_csrf_tokens"]),
            GateStep("导航页面烟测", [sys.executable, manage_py, "check_navigation_pages"]),
            GateStep("低频入口烟测", [sys.executable, manage_py, "check_low_frequency_entrypoints"]),
            GateStep("迁移一致性检查", [sys.executable, manage_py, "makemigrations", "--check", "--dry-run"]),
            GateStep("Python 依赖检查", [sys.executable, "-m", "pip", "check"]),
            GateStep("业务冒烟测试", _business_smoke_command(manage_py, operator)),
        ]
    )
    if include_tests:
        steps.append(GateStep("完整自动测试", [sys.executable, manage_py, "test", "--noinput", "--verbosity", "1"]))
    if include_production_preflight:
        steps.append(
            GateStep(
                "生产严格预检",
                [sys.executable, manage_py, "production_preflight", "--strict", "--skip-release-gate-report"],
            )
        )
    return steps


def _business_smoke_command(manage_py: str, operator: str) -> list[str]:
    command = [sys.executable, manage_py, "business_smoke_test"]
    if operator:
        command.extend(["--operator", operator])
    return command


def _run_step(step: GateStep, timeout_seconds: int) -> dict:
    try:
        completed = subprocess.run(
            step.command,
            cwd=Path(settings.BASE_DIR),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": step.name,
            "command": _command_display(step.command),
            "ok": False,
            "message": f"执行超过 {timeout_seconds} 秒",
        }

    if completed.returncode == 0:
        return {
            "name": step.name,
            "command": _command_display(step.command),
            "ok": True,
            "message": _summarize_output(step.name, completed.stdout, completed.stderr) or "通过",
        }
    return {
        "name": step.name,
        "command": _command_display(step.command),
        "ok": False,
        "message": _summarize_output(step.name, completed.stdout, completed.stderr) or f"退出码 {completed.returncode}",
    }


def _summarize_output(step_name: str, stdout: str, stderr: str) -> str:
    combined = "\n".join(part for part in [stdout, stderr] if part)
    if step_name == "完整自动测试":
        test_summary = _test_summary(combined)
        if test_summary:
            return test_summary

    patterns = [
        r"System check identified .+",
        r"URL 引用检查通过：.+",
        r"模板语法检查通过：.+",
        r"权限配置检查通过：.+",
        r"权限引用检查通过：.+",
        r"路由保护检查通过：.+",
        r"CSRF 表单检查通过：.+",
        r"导航页面烟测通过：.+",
        r"低频入口烟测通过：.+",
        r"No changes detected",
        r"No broken requirements found\.",
        r"业务冒烟测试通过：.+",
        r"生产预检通过",
        r"CommandError: .+",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined)
        if match:
            return match.group(0).strip()
    return _last_output_line(stderr) or _last_output_line(stdout)


def _test_summary(output: str) -> str:
    ran_match = re.search(r"Ran\s+\d+\s+tests?\s+in\s+[^\r\n]+", output)
    if not ran_match:
        return ""
    status = ""
    if re.search(r"^OK$", output, flags=re.MULTILINE):
        status = "OK"
    else:
        failed_match = re.search(r"^(FAILED \(.+\))$", output, flags=re.MULTILINE)
        if failed_match:
            status = failed_match.group(1)
    return f"{ran_match.group(0)}; {status}".strip("; ")


def _last_output_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _command_display(command: list[str]) -> str:
    return " ".join(command)


def _write_report(report_file: str, results: list[dict], passed: bool) -> Path:
    report_path = Path(report_file)
    if not report_path.is_absolute():
        report_path = Path(settings.BASE_DIR) / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    status = "通过" if passed else "未通过"
    generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# ERP 发布前门禁报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 总体结果：{status}",
        f"- 检查步骤数：{len(results)}",
        "",
        "| 步骤 | 结果 | 摘要 | 命令 |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        result_label = "OK" if result["ok"] else "FAIL"
        lines.append(
            "| {name} | {result_label} | {message} | `{command}` |".format(
                name=_escape_markdown_cell(result["name"]),
                result_label=result_label,
                message=_escape_markdown_cell(result["message"]),
                command=_escape_markdown_cell(result.get("command", "")),
            )
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _escape_markdown_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
