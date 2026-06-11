from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import get_template


@dataclass(frozen=True)
class TemplateCheckError:
    template_name: str
    message: str


@dataclass(frozen=True)
class TemplateCheckResult:
    checked_count: int
    errors: list[TemplateCheckError]


class Command(BaseCommand):
    help = "编译检查 templates 目录下的 Django 模板语法"

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            action="append",
            default=[],
            help="指定要检查的模板文件或目录；可重复传入。默认检查项目 templates 目录",
        )

    def handle(self, *args, **options):
        roots = [Path(path) for path in options["path"]] if options["path"] else None
        result = check_templates(roots=roots)
        if result.errors:
            for error in result.errors:
                self.stderr.write(f"{error.template_name}: {error.message}")
            raise CommandError(f"发现 {len(result.errors)} 个模板语法或加载错误")

        self.stdout.write(self.style.SUCCESS(f"模板语法检查通过：{result.checked_count} 个模板"))


def check_templates(roots: list[Path] | None = None) -> TemplateCheckResult:
    errors: list[TemplateCheckError] = []
    checked_count = 0
    for template_path in _iter_template_files(roots):
        template_name = _template_name(template_path)
        checked_count += 1
        try:
            get_template(template_name)
        except Exception as exc:
            errors.append(TemplateCheckError(template_name=template_name, message=str(exc)))
    return TemplateCheckResult(checked_count=checked_count, errors=errors)


def _iter_template_files(roots: list[Path] | None) -> list[Path]:
    scan_roots = roots or [Path(settings.BASE_DIR) / "templates"]
    files: list[Path] = []
    for root in scan_roots:
        path = root if root.is_absolute() else Path(settings.BASE_DIR) / root
        if path.is_file() and path.suffix == ".html":
            files.append(path)
            continue
        if not path.exists():
            continue
        files.extend(candidate for candidate in path.rglob("*.html") if candidate.is_file())
    return sorted(files)


def _template_name(path: Path) -> str:
    try:
        return path.relative_to(Path(settings.BASE_DIR) / "templates").as_posix()
    except ValueError:
        return path.name
