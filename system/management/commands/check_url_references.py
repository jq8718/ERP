from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse
from django.utils.text import smart_split


TEMPLATE_URL_TAG_RE = re.compile(r"{%\s*url\s+(?P<body>.*?)\s*%}")
STATIC_URL_NAME_RE = re.compile(r"^\s*(['\"])(?P<name>[^'\"]+)\1(?P<tail>.*)$")
PYTHON_URL_RE = re.compile(
    r"\b(?:reverse|reverse_lazy|redirect|resolve_url)\(\s*(['\"])(?P<name>[^'\"/#?]+)\1"
)

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


@dataclass(frozen=True)
class UrlReference:
    path: Path
    line_no: int
    url_name: str
    source_type: str
    arg_count: int = 0
    kwarg_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvalidUrlReference:
    reference: UrlReference
    message: str


@dataclass(frozen=True)
class UrlReferenceCheckResult:
    url_name_count: int
    reference_count: int
    missing_references: list[UrlReference]
    invalid_references: list[InvalidUrlReference]
    checked_template_argument_count: int


class Command(BaseCommand):
    help = "检查模板和 Python 代码中的静态 URL name 是否存在于 URLConf"

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            action="append",
            default=[],
            help="指定要扫描的路径；可重复传入。默认扫描业务 app 和 templates",
        )

    def handle(self, *args, **options):
        scan_roots = [Path(path) for path in options["path"]] if options["path"] else None
        result = check_url_references(scan_roots=scan_roots)
        if result.missing_references or result.invalid_references:
            for reference in result.missing_references:
                self.stderr.write(
                    "{path}:{line_no} 缺失 URL name `{url_name}` ({source_type})".format(
                        path=_display_path(reference.path),
                        line_no=reference.line_no,
                        url_name=reference.url_name,
                        source_type=reference.source_type,
                    )
                )
            for invalid in result.invalid_references:
                reference = invalid.reference
                self.stderr.write(
                    "{path}:{line_no} URL `{url_name}` 参数不匹配：{message}".format(
                        path=_display_path(reference.path),
                        line_no=reference.line_no,
                        url_name=reference.url_name,
                        message=invalid.message,
                    )
                )
            raise CommandError(
                "发现 {missing_count} 个不存在的静态 URL 引用，{invalid_count} 个参数不匹配的模板 URL 引用".format(
                    missing_count=len(result.missing_references),
                    invalid_count=len(result.invalid_references),
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                "URL 引用检查通过：{reference_count} 个静态引用，{url_name_count} 个 URL 名称，{checked_count} 个模板参数引用".format(
                    reference_count=result.reference_count,
                    url_name_count=result.url_name_count,
                    checked_count=result.checked_template_argument_count,
                )
            )
        )


def check_url_references(scan_roots: list[Path] | None = None) -> UrlReferenceCheckResult:
    known_names = collect_url_names()
    references = collect_static_url_references(scan_roots)
    missing = [reference for reference in references if reference.url_name not in known_names]
    invalid = validate_template_url_arguments(reference for reference in references if reference.url_name in known_names)
    return UrlReferenceCheckResult(
        url_name_count=len(known_names),
        reference_count=len(references),
        missing_references=missing,
        invalid_references=invalid,
        checked_template_argument_count=sum(1 for reference in references if _should_validate_arguments(reference, known_names)),
    )


def collect_url_names() -> set[str]:
    return _collect_url_names_from_resolver(get_resolver())


def _collect_url_names_from_resolver(resolver: URLResolver, prefix: str = "") -> set[str]:
    names: set[str] = set()
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLPattern):
            if pattern.name:
                names.add(f"{prefix}{pattern.name}")
            continue

        if isinstance(pattern, URLResolver):
            namespace_prefix = f"{prefix}{pattern.namespace}:" if pattern.namespace else prefix
            names.update(_collect_url_names_from_resolver(pattern, namespace_prefix))
    return names


def collect_static_url_references(scan_roots: list[Path] | None = None) -> list[UrlReference]:
    references: list[UrlReference] = []
    for path in _iter_scan_files(scan_roots):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        for line_no, line in enumerate(lines, start=1):
            if path.suffix == ".html":
                references.extend(_template_url_references(path, line_no, line))
            else:
                for match in PYTHON_URL_RE.finditer(line):
                    url_name = match.group("name").strip()
                    if _looks_like_url_name(url_name):
                        references.append(UrlReference(path=path, line_no=line_no, url_name=url_name, source_type="python"))
    return references


def validate_template_url_arguments(references) -> list[InvalidUrlReference]:
    invalid: list[InvalidUrlReference] = []
    for reference in references:
        if reference.source_type != "template":
            continue
        try:
            reverse(
                reference.url_name,
                args=["1"] * reference.arg_count,
                kwargs={name: "1" for name in reference.kwarg_names},
            )
        except (NoReverseMatch, ValueError) as exc:
            invalid.append(InvalidUrlReference(reference=reference, message=str(exc)))
    return invalid


def _template_url_references(path: Path, line_no: int, line: str) -> list[UrlReference]:
    references = []
    for tag_match in TEMPLATE_URL_TAG_RE.finditer(line):
        body = tag_match.group("body")
        name_match = STATIC_URL_NAME_RE.match(body)
        if not name_match:
            continue
        url_name = name_match.group("name").strip()
        if not _looks_like_url_name(url_name):
            continue
        arg_count, kwarg_names = _parse_template_url_arguments(name_match.group("tail"))
        references.append(
            UrlReference(
                path=path,
                line_no=line_no,
                url_name=url_name,
                source_type="template",
                arg_count=arg_count,
                kwarg_names=tuple(kwarg_names),
            )
        )
    return references


def _parse_template_url_arguments(tail: str) -> tuple[int, list[str]]:
    arg_count = 0
    kwarg_names: list[str] = []
    tokens = list(smart_split(tail.strip()))
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token == "as":
            break
        if "=" in token:
            key, _value = token.split("=", 1)
            if re.fullmatch(r"[A-Za-z_]\w*", key):
                kwarg_names.append(key)
            else:
                arg_count += 1
        else:
            arg_count += 1
        token_index += 1
    return arg_count, kwarg_names


def _should_validate_arguments(reference: UrlReference, known_names: set[str]) -> bool:
    return reference.source_type == "template" and reference.url_name in known_names


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
    return any(part in skip_parts for part in path.parts)


def _looks_like_url_name(value: str) -> bool:
    if not value or value.startswith(("http:", "https:", "/", "#", "?")):
        return False
    return bool(re.fullmatch(r"[\w.-]+(?::[\w.-]+)?", value))


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        return str(path)
