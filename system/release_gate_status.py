from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone


@dataclass(frozen=True)
class ReleaseGateReportStatus:
    path: Path
    exists: bool
    ok: bool
    fresh: bool
    message: str
    generated_at: datetime | None = None
    overall_result: str = ""
    step_count: int | None = None
    step_names: tuple[str, ...] = ()
    age_hours: float | None = None
    max_age_hours: int = 24


def get_release_gate_report_status(report_file: str | Path | None = None) -> ReleaseGateReportStatus:
    path = _resolve_report_path(report_file)
    max_age_hours = int(getattr(settings, "ERP_RELEASE_GATE_MAX_AGE_HOURS", 24))
    if not path.exists():
        return ReleaseGateReportStatus(
            path=path,
            exists=False,
            ok=False,
            fresh=False,
            message=f"门禁报告不存在：{path}",
            max_age_hours=max_age_hours,
        )

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return ReleaseGateReportStatus(
            path=path,
            exists=True,
            ok=False,
            fresh=False,
            message=f"门禁报告无法读取：{exc}",
            max_age_hours=max_age_hours,
        )

    generated_at = _parse_generated_at(content)
    overall_result = _parse_field(content, "总体结果")
    step_count_text = _parse_field(content, "检查步骤数")
    step_count = int(step_count_text) if step_count_text.isdigit() else None
    step_names = tuple(_parse_step_names(content))

    if generated_at is None:
        return ReleaseGateReportStatus(
            path=path,
            exists=True,
            ok=False,
            fresh=False,
            message="门禁报告缺少生成时间",
            overall_result=overall_result,
            step_count=step_count,
            step_names=step_names,
            max_age_hours=max_age_hours,
        )

    now = timezone.localtime(timezone.now())
    age_hours = max((now - timezone.localtime(generated_at)).total_seconds() / 3600, 0)
    passed = overall_result == "通过"
    fresh = age_hours <= max_age_hours
    if passed and fresh:
        message = f"最近门禁通过，生成于 {timezone.localtime(generated_at):%Y-%m-%d %H:%M:%S}"
    elif not passed:
        message = f"最近门禁未通过或状态未知：{overall_result or '未找到总体结果'}"
    else:
        message = f"最近门禁报告已超过 {max_age_hours} 小时"

    return ReleaseGateReportStatus(
        path=path,
        exists=True,
        ok=passed,
        fresh=fresh,
        message=message,
        generated_at=generated_at,
        overall_result=overall_result,
        step_count=step_count,
        step_names=step_names,
        age_hours=age_hours,
        max_age_hours=max_age_hours,
    )


def _resolve_report_path(report_file: str | Path | None) -> Path:
    configured = report_file or getattr(settings, "ERP_RELEASE_GATE_REPORT_FILE", "docs/latest-release-gate-report.md")
    path = Path(configured)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return path


def _parse_field(content: str, field_name: str) -> str:
    match = re.search(rf"^- {re.escape(field_name)}：(.+)$", content, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_generated_at(content: str) -> datetime | None:
    value = _parse_field(content, "生成时间")
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return timezone.make_aware(parsed, timezone.get_current_timezone())


def _parse_step_names(content: str) -> list[str]:
    names = []
    for line in content.splitlines():
        if not line.startswith("| ") or line.startswith("| 步骤 ") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0]:
            names.append(cells[0])
    return names
