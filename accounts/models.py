from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class SecurityLevel(models.TextChoices):
        L1 = "L1", "普通账号"
        L2 = "L2", "敏感账号"
        L3 = "L3", "部门管理员"
        L4 = "L4", "系统管理员"

    class AccountStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"
        LOCKED = "locked", "锁定"

    display_name = models.CharField(max_length=80, blank=True)
    department = models.CharField(max_length=80, blank=True)
    position = models.CharField(max_length=80, blank=True)
    security_level = models.CharField(max_length=8, choices=SecurityLevel.choices, default=SecurityLevel.L1)
    status = models.CharField(max_length=16, choices=AccountStatus.choices, default=AccountStatus.ACTIVE)
    is_deleted = models.BooleanField(default=False)
    roles = models.ManyToManyField("Role", blank=True, related_name="users", db_table="user_roles")

    class Meta:
        db_table = "users"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["department"]),
        ]

    def __str__(self):
        return self.display_name or self.username


class Role(models.Model):
    class RoleStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    role_code = models.CharField(max_length=80, unique=True)
    role_name = models.CharField(max_length=120)
    status = models.CharField(max_length=16, choices=RoleStatus.choices, default=RoleStatus.ACTIVE)
    permissions = models.ManyToManyField("Permission", blank=True, related_name="roles", db_table="role_permissions")
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "roles"

    def __str__(self):
        return self.role_name


class Permission(models.Model):
    class PermissionType(models.TextChoices):
        MODULE = "module", "模块权限"
        ACTION = "action", "动作权限"
        FIELD = "field", "字段权限"
        DATA_SCOPE = "data_scope", "数据范围"

    permission_code = models.CharField(max_length=120, unique=True)
    permission_name = models.CharField(max_length=160)
    permission_type = models.CharField(max_length=24, choices=PermissionType.choices)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "permissions"

    def __str__(self):
        return self.permission_name or self.permission_code


class UserSession(models.Model):
    class SessionStatus(models.TextChoices):
        ACTIVE = "active", "有效"
        REVOKED = "revoked", "已强制失效"
        EXPIRED = "expired", "已过期"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="erp_sessions")
    session_key = models.CharField(max_length=80, unique=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=SessionStatus.choices, default=SessionStatus.ACTIVE)

    class Meta:
        db_table = "user_sessions"
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["session_key"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.ip_address or ''} - {self.get_status_display()}"
