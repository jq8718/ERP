from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse


LOW_FREQUENCY_SUFFIXES = (
    "_export",
    "_import_template",
    "_import",
    "_print",
)
SKIPPED_NAMESPACES = {"admin"}
SUCCESS_STATUS_CODES = {200}


@dataclass(frozen=True)
class LowFrequencyEntrypoint:
    url_name: str
    pattern: str
    category: str
    kwarg_names: tuple[str, ...]
    view_class: type | None = None


@dataclass(frozen=True)
class BrokenLowFrequencyEntrypoint:
    entrypoint: LowFrequencyEntrypoint
    path: str
    status_code: int | None
    reason: str


@dataclass(frozen=True)
class LowFrequencyEntrypointCheckResult:
    entrypoint_count: int
    reversed_count: int
    smoked_count: int
    reverse_only_count: int
    broken_entrypoints: list[BrokenLowFrequencyEntrypoint]


class Command(BaseCommand):
    help = "检查导入、导出和打印等低频入口是否可反转，且非写入口可访问"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="", help="指定用于烟测的启用账号；默认使用第一个启用超级管理员")

    def handle(self, *args, **options):
        user = _resolve_smoke_user(options["username"].strip())
        result = check_low_frequency_entrypoints(user=user)
        if result.broken_entrypoints:
            for broken in result.broken_entrypoints:
                self.stderr.write(
                    "{url_name} ({pattern}) {path}：{reason}".format(
                        url_name=broken.entrypoint.url_name,
                        pattern=broken.entrypoint.pattern,
                        path=broken.path or "-",
                        reason=broken.reason,
                    )
                )
            raise CommandError(f"发现 {len(result.broken_entrypoints)} 个低频入口不可用")

        self.stdout.write(
            self.style.SUCCESS(
                "低频入口烟测通过：{entrypoint_count} 个入口可反转，{smoked_count} 个非写入口可访问，{reverse_only_count} 个导出或对象级入口仅做反转检查，用户 {username}".format(
                    entrypoint_count=result.entrypoint_count,
                    smoked_count=result.smoked_count,
                    reverse_only_count=result.reverse_only_count,
                    username=user.username,
                )
            )
        )


def check_low_frequency_entrypoints(user) -> LowFrequencyEntrypointCheckResult:
    entrypoints = collect_low_frequency_entrypoints(get_resolver().url_patterns)
    client = Client()
    client.force_login(user)
    broken: list[BrokenLowFrequencyEntrypoint] = []
    reversed_count = 0
    smoked_count = 0
    reverse_only_count = 0

    for entrypoint in entrypoints:
        try:
            path = _reverse_entrypoint(entrypoint)
        except NoReverseMatch as exc:
            broken.append(BrokenLowFrequencyEntrypoint(entrypoint, "", None, f"URL 反转失败：{exc}"))
            continue
        reversed_count += 1

        smoke_path = _smoke_path(entrypoint, path)
        if not smoke_path:
            reverse_only_count += 1
            continue

        response = client.get(smoke_path)
        smoked_count += 1
        if response.status_code not in SUCCESS_STATUS_CODES:
            broken.append(
                BrokenLowFrequencyEntrypoint(
                    entrypoint,
                    smoke_path,
                    response.status_code,
                    _response_reason(response),
                )
            )

    return LowFrequencyEntrypointCheckResult(
        entrypoint_count=len(entrypoints),
        reversed_count=reversed_count,
        smoked_count=smoked_count,
        reverse_only_count=reverse_only_count,
        broken_entrypoints=broken,
    )


def collect_low_frequency_entrypoints(patterns, namespace_prefix: str = "") -> list[LowFrequencyEntrypoint]:
    entrypoints: list[LowFrequencyEntrypoint] = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace in SKIPPED_NAMESPACES:
                continue
            next_prefix = f"{namespace_prefix}{pattern.namespace}:" if pattern.namespace else namespace_prefix
            entrypoints.extend(collect_low_frequency_entrypoints(pattern.url_patterns, next_prefix))
            continue

        if not isinstance(pattern, URLPattern) or not pattern.name:
            continue

        url_name = f"{namespace_prefix}{pattern.name}"
        category = _entrypoint_category(url_name)
        if not category:
            continue
        entrypoints.append(
            LowFrequencyEntrypoint(
                url_name=url_name,
                pattern=str(pattern.pattern),
                category=category,
                kwarg_names=tuple(getattr(pattern.pattern, "converters", {}).keys()),
                view_class=getattr(pattern.callback, "view_class", None),
            )
        )
    return entrypoints


def _entrypoint_category(url_name: str) -> str:
    local_name = url_name.rsplit(":", 1)[-1]
    if local_name.endswith("_import_template"):
        return "import_template"
    if local_name.endswith("_export"):
        return "export"
    if local_name.endswith("_import"):
        return "import"
    if local_name.endswith("_print"):
        return "print"
    return ""


def _reverse_entrypoint(entrypoint: LowFrequencyEntrypoint) -> str:
    if entrypoint.kwarg_names:
        return reverse(
            entrypoint.url_name,
            kwargs={name: _sample_kwarg_value(name) for name in entrypoint.kwarg_names},
        )
    return reverse(entrypoint.url_name)


def _smoke_path(entrypoint: LowFrequencyEntrypoint, reversed_path: str) -> str:
    if entrypoint.category not in {"import", "import_template"}:
        return ""
    if not entrypoint.kwarg_names:
        return reversed_path
    return ""


def _sample_kwarg_value(name: str) -> str:
    if name.endswith("_pk") or name == "pk" or name.endswith("_id"):
        return "1"
    return "sample"


def _resolve_smoke_user(username: str):
    User = get_user_model()
    queryset = User.objects.filter(is_active=True, is_deleted=False, status="active").order_by("id")
    if username:
        user = queryset.filter(username=username).first()
        if user is None:
            raise CommandError(f"低频入口烟测用户不存在或不可用：{username}")
        return user

    user = queryset.filter(is_superuser=True).first()
    if user is None:
        raise CommandError("缺少可用超级管理员账号，请先执行 bootstrap_admin")
    return user


def _response_reason(response) -> str:
    if response.status_code == 403:
        return "无权限"
    if response.status_code == 404:
        return "页面不存在"
    if response.status_code >= 500:
        return "服务器错误"
    return "非预期状态"
