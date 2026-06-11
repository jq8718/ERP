from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Permission
from accounts.permissions import DEFAULT_PERMISSIONS, PermissionCode


@dataclass(frozen=True)
class PermissionCheckResult:
    expected_count: int
    missing_permission_codes: list[str]
    undeclared_permission_codes: list[str]


class Command(BaseCommand):
    help = "检查代码 PermissionCode、默认权限清单和数据库默认权限记录是否一致"

    def handle(self, *args, **options):
        result = check_permissions()
        if result.missing_permission_codes or result.undeclared_permission_codes:
            if result.missing_permission_codes:
                self.stderr.write("数据库缺少默认权限：" + ", ".join(result.missing_permission_codes))
            if result.undeclared_permission_codes:
                self.stderr.write("PermissionCode 未加入 DEFAULT_PERMISSIONS：" + ", ".join(result.undeclared_permission_codes))
            raise CommandError("权限配置一致性检查未通过")

        self.stdout.write(self.style.SUCCESS(f"权限配置检查通过：{result.expected_count} 个默认权限"))


def check_permissions() -> PermissionCheckResult:
    expected_codes = [code for code, _name, _permission_type in DEFAULT_PERMISSIONS]
    existing_codes = set(
        Permission.objects.filter(permission_code__in=expected_codes).values_list("permission_code", flat=True)
    )
    declared_codes = set(expected_codes)
    permission_code_constants = {
        value
        for name, value in vars(PermissionCode).items()
        if name.isupper() and isinstance(value, str)
    }
    return PermissionCheckResult(
        expected_count=len(expected_codes),
        missing_permission_codes=sorted(set(expected_codes) - existing_codes),
        undeclared_permission_codes=sorted(permission_code_constants - declared_codes),
    )
