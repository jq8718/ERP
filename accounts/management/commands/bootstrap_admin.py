import os

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Permission, Role, User
from accounts.permissions import DEFAULT_PERMISSIONS, PermissionCode, ensure_default_permissions


DEFAULT_PASSWORD_ENV = "ERP_BOOTSTRAP_ADMIN_PASSWORD"


class Command(BaseCommand):
    help = "幂等创建或修复首个 ERP 超级管理员和权限管理员角色"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin", help="管理员用户名，默认 admin")
        parser.add_argument("--email", default="", help="管理员邮箱")
        parser.add_argument("--display-name", default="系统管理员", help="管理员显示名称")
        parser.add_argument("--password", default="", help="管理员初始密码；生产建议改用 --password-env")
        parser.add_argument(
            "--password-env",
            default=DEFAULT_PASSWORD_ENV,
            help=f"读取管理员密码的环境变量，默认 {DEFAULT_PASSWORD_ENV}",
        )
        parser.add_argument("--reset-password", action="store_true", help="用户已存在时也重置密码")
        parser.add_argument("--role-code", default="permission-admin", help="权限管理员角色编码")
        parser.add_argument("--role-name", default="权限管理员", help="权限管理员角色名称")
        parser.add_argument("--noinput", action="store_true", help="兼容部署脚本；本命令始终非交互执行")
        parser.add_argument("--check-only", action="store_true", help="只检查初始化是否完成，不创建或修改任何数据")

    @transaction.atomic
    def handle(self, *args, **options):
        username = options["username"].strip()
        if not username:
            raise CommandError("--username 不能为空")
        role_code = options["role_code"].strip()
        if options["check_only"]:
            self._check_bootstrap_state(username=username, role_code=role_code)
            self.stdout.write(self.style.SUCCESS(f"Bootstrap admin check passed: username={username}, role={role_code}"))
            return

        password = self._resolve_password(options["password"], options["password_env"])

        ensure_default_permissions()
        role = self._ensure_permission_admin_role(role_code, options["role_name"])
        user, created = User.objects.select_for_update().get_or_create(username=username)

        needs_password = created or options["reset_password"] or not user.has_usable_password()
        if needs_password:
            if not password:
                raise CommandError(
                    "创建管理员、重置密码或修复无可用密码账号时必须提供 --password 或 --password-env"
                )
            self._validate_password(password, user, username)
            user.set_password(password)

        user.email = options["email"] or user.email
        user.display_name = options["display_name"] or user.display_name
        user.security_level = User.SecurityLevel.L4
        user.status = User.AccountStatus.ACTIVE
        user.is_active = True
        user.is_deleted = False
        user.is_staff = True
        user.is_superuser = True
        user.save()
        user.roles.add(role)

        action = "created" if created else "updated"
        password_message = "password set" if needs_password else "password unchanged"
        self.stdout.write(
            self.style.SUCCESS(
                f"Bootstrap admin {action}: username={user.username}, role={role.role_code}, {password_message}"
            )
        )

    def _resolve_password(self, password: str, password_env: str) -> str:
        if password and password_env and os.getenv(password_env):
            raise CommandError("不能同时使用 --password 和已设置值的 --password-env，避免密码来源歧义")
        if password:
            return password
        if password_env:
            return os.getenv(password_env, "")
        return ""

    def _ensure_permission_admin_role(self, role_code: str, role_name: str) -> Role:
        role_code = role_code.strip()
        role_name = role_name.strip()
        if not role_code:
            raise CommandError("--role-code 不能为空")
        if not role_name:
            raise CommandError("--role-name 不能为空")

        admin_permission = Permission.objects.get(permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE)
        role, _ = Role.objects.select_for_update().get_or_create(
            role_code=role_code,
            defaults={"role_name": role_name, "status": Role.RoleStatus.ACTIVE},
        )
        changed = False
        if role.role_name != role_name:
            role.role_name = role_name
            changed = True
        if role.status != Role.RoleStatus.ACTIVE:
            role.status = Role.RoleStatus.ACTIVE
            changed = True
        if changed:
            role.save(update_fields=["role_name", "status"])
        role.permissions.add(admin_permission)
        return role

    def _validate_password(self, password: str, user: User, username: str) -> None:
        if getattr(settings, "IS_PRODUCTION", False) and len(password) < 12:
            raise CommandError("生产环境管理员密码长度至少 12 位")
        if username.lower() in password.lower():
            raise CommandError("管理员密码不能包含用户名")
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

    def _check_bootstrap_state(self, username: str, role_code: str) -> None:
        missing = []
        expected_permission_codes = [code for code, _name, _permission_type in DEFAULT_PERMISSIONS]
        existing_permission_codes = set(
            Permission.objects.filter(permission_code__in=expected_permission_codes).values_list(
                "permission_code", flat=True
            )
        )
        missing_permissions = sorted(set(expected_permission_codes) - existing_permission_codes)
        if missing_permissions:
            missing.append("缺少默认权限：" + ", ".join(missing_permissions))

        role = Role.objects.filter(role_code=role_code).first()
        if role is None:
            missing.append(f"缺少权限管理员角色：{role_code}")
        else:
            if role.status != Role.RoleStatus.ACTIVE:
                missing.append(f"权限管理员角色未启用：{role_code}")
            if not role.permissions.filter(permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE).exists():
                missing.append(f"权限管理员角色缺少权限：{PermissionCode.ADMIN_PERMISSION_MANAGE}")

        user = User.objects.filter(username=username).first()
        if user is None:
            missing.append(f"缺少初始化管理员账号：{username}")
        else:
            if not user.is_active or user.is_deleted or user.status != User.AccountStatus.ACTIVE:
                missing.append(f"初始化管理员账号未启用：{username}")
            if not user.is_staff or not user.is_superuser:
                missing.append(f"初始化管理员账号不是 staff/superuser：{username}")
            if user.security_level != User.SecurityLevel.L4:
                missing.append(f"初始化管理员账号安全等级不是 L4：{username}")
            if not user.has_usable_password():
                missing.append(f"初始化管理员账号没有可用密码：{username}")
            if role is not None and not user.roles.filter(id=role.id).exists():
                missing.append(f"初始化管理员账号未分配权限管理员角色：{username}")

        if missing:
            raise CommandError("初始化检查未通过：" + "；".join(missing))
