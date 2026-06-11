from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


FORM_TAG_RE = re.compile(r"<form\b[^>]*>", flags=re.IGNORECASE | re.DOTALL)
POST_METHOD_RE = re.compile(
    r"\bmethod\s*=\s*(?P<quote>['\"]?)post(?P=quote)(?:\s|>|/)",
    flags=re.IGNORECASE,
)
FORM_CLOSE_RE = re.compile(r"</form\s*>", flags=re.IGNORECASE)
CSRF_TOKEN_TEXT = "{% csrf_token %}"


@dataclass(frozen=True)
class MissingCsrfToken:
    path: Path
    line_no: int


@dataclass(frozen=True)
class CsrfTokenCheckResult:
    post_form_count: int
    missing_tokens: list[MissingCsrfToken]


class Command(BaseCommand):
    help = "检查模板中的 POST 表单是否包含 csrf_token"

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            action="append",
            default=[],
            help="指定要扫描的模板路径；可重复传入。默认扫描 templates 目录和各 app templates 目录",
        )

    def handle(self, *args, **options):
        scan_roots = [Path(path) for path in options["path"]] if options["path"] else None
        result = check_csrf_tokens(scan_roots=scan_roots)
        if result.missing_tokens:
            for missing in result.missing_tokens:
                self.stderr.write(
                    "{path}:{line_no} POST 表单缺少 csrf_token".format(
                        path=_display_path(missing.path),
                        line_no=missing.line_no,
                    )
                )
            raise CommandError(f"发现 {len(result.missing_tokens)} 个缺少 csrf_token 的 POST 表单")

        self.stdout.write(self.style.SUCCESS(f"CSRF 表单检查通过：{result.post_form_count} 个 POST 表单均包含 csrf_token"))


def check_csrf_tokens(scan_roots: list[Path] | None = None) -> CsrfTokenCheckResult:
    post_form_count = 0
    missing_tokens: list[MissingCsrfToken] = []
    for path in _iter_template_files(scan_roots):
        content = path.read_text(encoding="utf-8")
        for form_match in FORM_TAG_RE.finditer(content):
            form_tag = form_match.group(0)
            if not POST_METHOD_RE.search(form_tag):
                continue
            post_form_count += 1
            close_match = FORM_CLOSE_RE.search(content, form_match.end())
            form_block = content[form_match.start() : close_match.end() if close_match else len(content)]
            if CSRF_TOKEN_TEXT not in form_block:
                missing_tokens.append(
                    MissingCsrfToken(
                        path=path,
                        line_no=content.count("\n", 0, form_match.start()) + 1,
                    )
                )
    return CsrfTokenCheckResult(post_form_count=post_form_count, missing_tokens=missing_tokens)


def _iter_template_files(scan_roots: list[Path] | None) -> list[Path]:
    roots = scan_roots or _default_template_roots()
    files: list[Path] = []
    for root in roots:
        path = root if root.is_absolute() else Path(settings.BASE_DIR) / root
        if path.is_file() and path.suffix == ".html":
            files.append(path)
            continue
        if not path.exists():
            continue
        files.extend(candidate for candidate in path.rglob("*.html") if not _should_skip(candidate))
    return sorted(set(files))


def _default_template_roots() -> list[Path]:
    roots = [Path(settings.BASE_DIR) / "templates"]
    for app_dir in [
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
    ]:
        roots.append(Path(settings.BASE_DIR) / app_dir / "templates")
    return roots


def _should_skip(path: Path) -> bool:
    skip_parts = {"__pycache__", "staticfiles", ".venv"}
    return any(part in skip_parts for part in path.parts)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        return str(path)
