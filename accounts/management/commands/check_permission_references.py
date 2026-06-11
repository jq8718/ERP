from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.permissions import DEFAULT_PERMISSIONS


DEFAULT_SCAN_DIRS = [
    "accounts",
    "approvals",
    "bom",
    "files",
    "finance",
    "inventory",
    "masterdata",
    "notifications",
    "production",
    "purchase",
    "sales",
    "system",
    "templates",
]

PERMISSION_CONTEXT_RE = re.compile(
    r"(has_erp_perm|user_has_permission|require_erp_permission|permission_required|"
    r"create_permission_required|page_action_permissions|PermissionCode|permission_code|"
    r"permissions__permission_code|PERMISSION_CODE)"
)
PERMISSION_LITERAL_RE = re.compile(
    r"(?P<quote>['\"])(?P<code>[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)(?P=quote)"
)


@dataclass(frozen=True)
class PermissionReference:
    path: Path
    line_no: int
    permission_code: str


@dataclass(frozen=True)
class PermissionReferenceCheckResult:
    expected_count: int
    reference_count: int
    unknown_references: list[PermissionReference]


class Command(BaseCommand):
    help = "检查代码和模板中的静态权限码引用是否已登记为默认权限"

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            action="append",
            default=[],
            help="指定要扫描的路径；可重复传入。默认扫描业务 app 和 templates",
        )

    def handle(self, *args, **options):
        scan_roots = [Path(path) for path in options["path"]] if options["path"] else None
        result = check_permission_references(scan_roots=scan_roots)
        if result.unknown_references:
            for reference in result.unknown_references:
                self.stderr.write(
                    "{path}:{line_no} 未登记权限码 `{permission_code}`".format(
                        path=_display_path(reference.path),
                        line_no=reference.line_no,
                        permission_code=reference.permission_code,
                    )
                )
            raise CommandError(f"发现 {len(result.unknown_references)} 个未登记的静态权限码引用")

        self.stdout.write(
            self.style.SUCCESS(
                "权限引用检查通过：{reference_count} 个静态权限引用，{expected_count} 个默认权限".format(
                    reference_count=result.reference_count,
                    expected_count=result.expected_count,
                )
            )
        )


def check_permission_references(scan_roots: list[Path] | None = None) -> PermissionReferenceCheckResult:
    expected_codes = {code for code, _name, _permission_type in DEFAULT_PERMISSIONS}
    references = collect_static_permission_references(scan_roots)
    unknown = [reference for reference in references if reference.permission_code not in expected_codes]
    return PermissionReferenceCheckResult(
        expected_count=len(expected_codes),
        reference_count=len(references),
        unknown_references=unknown,
    )


def collect_static_permission_references(scan_roots: list[Path] | None = None) -> list[PermissionReference]:
    references: list[PermissionReference] = []
    for path in _iter_scan_files(scan_roots):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        for line_no, line in enumerate(lines, start=1):
            if not PERMISSION_CONTEXT_RE.search(line):
                continue
            for match in PERMISSION_LITERAL_RE.finditer(line):
                references.append(
                    PermissionReference(
                        path=path,
                        line_no=line_no,
                        permission_code=match.group("code"),
                    )
                )
    return references


def _iter_scan_files(scan_roots: list[Path] | None) -> list[Path]:
    roots = scan_roots or [Path(settings.BASE_DIR) / dirname for dirname in DEFAULT_SCAN_DIRS]
    files: list[Path] = []
    for root in roots:
        path = root if root.is_absolute() else Path(settings.BASE_DIR) / root
        if path.is_file() and path.suffix in {".py", ".html"}:
            files.append(path)
            continue
        if not path.exists():
            continue
        for candidate in path.rglob("*"):
            if candidate.suffix not in {".py", ".html"}:
                continue
            if _should_skip(candidate):
                continue
            files.append(candidate)
    return sorted(files)


def _should_skip(path: Path) -> bool:
    skip_parts = {"__pycache__", "migrations", "staticfiles", ".venv"}
    return any(part in skip_parts for part in path.parts) or path.name.startswith("test")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        return str(path)
