from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client


NAV_LINK_RE = re.compile(r'<a\s+class="nav-link"[^>]*href="(?P<href>[^"]+)"', flags=re.IGNORECASE)
SUCCESS_STATUS_CODES = {200}


@dataclass(frozen=True)
class BrokenNavigationPage:
    path: str
    status_code: int
    reason: str


@dataclass(frozen=True)
class NavigationPageCheckResult:
    checked_count: int
    broken_pages: list[BrokenNavigationPage]


class Command(BaseCommand):
    help = "登录管理员并检查主导航页面是否可正常打开"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="", help="指定用于烟测的启用账号；默认使用第一个启用超级管理员")

    def handle(self, *args, **options):
        user = _resolve_smoke_user(options["username"].strip())
        result = check_navigation_pages(user=user)
        if result.broken_pages:
            for page in result.broken_pages:
                self.stderr.write(f"{page.path} 返回 {page.status_code}：{page.reason}")
            raise CommandError(f"发现 {len(result.broken_pages)} 个主导航页面不可访问")

        self.stdout.write(
            self.style.SUCCESS(
                f"导航页面烟测通过：{result.checked_count} 个主导航页面可正常打开，用户 {user.username}"
            )
        )


def check_navigation_pages(user) -> NavigationPageCheckResult:
    client = Client()
    client.force_login(user)
    dashboard_response = client.get("/")
    if dashboard_response.status_code != 200:
        return NavigationPageCheckResult(
            checked_count=1,
            broken_pages=[BrokenNavigationPage("/", dashboard_response.status_code, "工作台无法打开")],
        )

    paths = collect_navigation_paths(dashboard_response.content.decode(dashboard_response.charset or "utf-8"))
    broken: list[BrokenNavigationPage] = []
    for path in paths:
        response = client.get(path)
        if response.status_code not in SUCCESS_STATUS_CODES:
            broken.append(BrokenNavigationPage(path, response.status_code, _response_reason(response)))
    return NavigationPageCheckResult(checked_count=len(paths), broken_pages=broken)


def collect_navigation_paths(html: str) -> tuple[str, ...]:
    paths = []
    seen = set()
    for match in NAV_LINK_RE.finditer(html):
        href = unescape(match.group("href")).strip()
        parsed = urlparse(href)
        if parsed.scheme or parsed.netloc:
            continue
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def _resolve_smoke_user(username: str):
    User = get_user_model()
    queryset = User.objects.filter(is_active=True, is_deleted=False, status="active").order_by("id")
    if username:
        user = queryset.filter(username=username).first()
        if user is None:
            raise CommandError(f"导航烟测用户不存在或不可用：{username}")
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
