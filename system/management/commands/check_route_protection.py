from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.management.base import BaseCommand, CommandError
from django.urls import URLPattern, URLResolver, get_resolver

from accounts.permissions import ErpPermissionRequiredMixin


PUBLIC_URL_NAMES = {"login", "logout"}
SKIPPED_NAMESPACES = {"admin"}


@dataclass(frozen=True)
class UnprotectedRoute:
    url_name: str
    pattern: str
    view_name: str


@dataclass(frozen=True)
class RouteProtectionCheckResult:
    route_count: int
    unprotected_routes: list[UnprotectedRoute]


class Command(BaseCommand):
    help = "检查业务 URL 是否都有登录或 ERP 权限保护"

    def handle(self, *args, **options):
        result = check_route_protection()
        if result.unprotected_routes:
            for route in result.unprotected_routes:
                self.stderr.write(
                    "{url_name} ({pattern}) 未继承登录或 ERP 权限保护：{view_name}".format(
                        url_name=route.url_name,
                        pattern=route.pattern,
                        view_name=route.view_name,
                    )
                )
            raise CommandError(f"发现 {len(result.unprotected_routes)} 个未保护业务 URL")

        self.stdout.write(
            self.style.SUCCESS(f"路由保护检查通过：{result.route_count} 个业务 URL 均有登录或权限保护")
        )


def check_route_protection() -> RouteProtectionCheckResult:
    routes = collect_unprotected_routes(get_resolver().url_patterns)
    return RouteProtectionCheckResult(
        route_count=_protected_route_count(get_resolver().url_patterns),
        unprotected_routes=routes,
    )


def collect_unprotected_routes(patterns, namespace_prefix: str = "") -> list[UnprotectedRoute]:
    unprotected: list[UnprotectedRoute] = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace in SKIPPED_NAMESPACES:
                continue
            next_prefix = f"{namespace_prefix}{pattern.namespace}:" if pattern.namespace else namespace_prefix
            unprotected.extend(collect_unprotected_routes(pattern.url_patterns, next_prefix))
            continue

        if not isinstance(pattern, URLPattern):
            continue
        url_name = f"{namespace_prefix}{pattern.name}" if pattern.name else ""
        if _is_public_route(url_name):
            continue
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class and _is_protected_view_class(view_class):
            continue
        unprotected.append(
            UnprotectedRoute(
                url_name=url_name or "(unnamed)",
                pattern=str(pattern.pattern),
                view_name=getattr(view_class, "__name__", repr(pattern.callback)),
            )
        )
    return unprotected


def _protected_route_count(patterns, namespace_prefix: str = "") -> int:
    count = 0
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace in SKIPPED_NAMESPACES:
                continue
            next_prefix = f"{namespace_prefix}{pattern.namespace}:" if pattern.namespace else namespace_prefix
            count += _protected_route_count(pattern.url_patterns, next_prefix)
            continue
        if isinstance(pattern, URLPattern):
            url_name = f"{namespace_prefix}{pattern.name}" if pattern.name else ""
            if not _is_public_route(url_name):
                count += 1
    return count


def _is_public_route(url_name: str) -> bool:
    return not url_name or url_name in PUBLIC_URL_NAMES


def _is_protected_view_class(view_class) -> bool:
    return issubclass(view_class, LoginRequiredMixin) or issubclass(view_class, ErpPermissionRequiredMixin)
