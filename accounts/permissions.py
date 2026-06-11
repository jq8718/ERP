from __future__ import annotations

from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.contrib import messages

from .models import Permission, Role, User


class PermissionCode:
    ADMIN_PERMISSION_MANAGE = "admin.permission_manage"
    SALES_VIEW_ALL = "sales.view_all"
    SALES_PROCESS = "sales.process"
    BOM_PROCESS = "bom.process"
    PURCHASE_PROCESS = "purchase.process"
    INVENTORY_PROCESS = "inventory.process"
    PRODUCTION_PROCESS = "production.process"
    FINANCE_VIEW_AMOUNT = "finance.view_amount"
    FINANCE_PAYMENT_PROCESS = "finance.payment_process"
    MASTERDATA_VIEW_PERSONAL_INFO = "masterdata.view_personal_info"
    ATTACHMENT_VIEW_SENSITIVE = "files.attachment_sensitive_view"
    ATTACHMENT_DELETE = "files.attachment_delete"


DEFAULT_PERMISSIONS = [
    (PermissionCode.ADMIN_PERMISSION_MANAGE, "权限与审批规则管理", Permission.PermissionType.ACTION),
    (PermissionCode.SALES_VIEW_ALL, "查看全部销售数据", Permission.PermissionType.DATA_SCOPE),
    (PermissionCode.SALES_PROCESS, "处理销售单据", Permission.PermissionType.ACTION),
    (PermissionCode.BOM_PROCESS, "维护和启停 BOM", Permission.PermissionType.ACTION),
    (PermissionCode.PURCHASE_PROCESS, "处理采购单据", Permission.PermissionType.ACTION),
    (PermissionCode.INVENTORY_PROCESS, "处理库存单据", Permission.PermissionType.ACTION),
    (PermissionCode.PRODUCTION_PROCESS, "处理生产单据", Permission.PermissionType.ACTION),
    (PermissionCode.FINANCE_VIEW_AMOUNT, "查看财务金额", Permission.PermissionType.FIELD),
    (PermissionCode.FINANCE_PAYMENT_PROCESS, "处理收付款和余额", Permission.PermissionType.ACTION),
    (PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO, "查看客户和供应商联系信息", Permission.PermissionType.FIELD),
    (PermissionCode.ATTACHMENT_VIEW_SENSITIVE, "查看敏感附件", Permission.PermissionType.FIELD),
    (PermissionCode.ATTACHMENT_DELETE, "删除附件", Permission.PermissionType.ACTION),
]


def ensure_default_permissions() -> None:
    for code, name, permission_type in DEFAULT_PERMISSIONS:
        Permission.objects.get_or_create(
            permission_code=code,
            defaults={"permission_name": name, "permission_type": permission_type},
        )


def user_has_permission(user: User, permission_code: str) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    if not user.is_active or user.is_deleted or user.status != User.AccountStatus.ACTIVE:
        return False
    return Role.objects.filter(
        users=user,
        status=Role.RoleStatus.ACTIVE,
        permissions__permission_code=permission_code,
    ).exists()


def can_view_amount(user: User) -> bool:
    return user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT)


def can_view_personal_info(user: User) -> bool:
    return user_has_permission(user, PermissionCode.MASTERDATA_VIEW_PERSONAL_INFO)


def require_erp_permission(user: User, permission_code: str, message: str = "无权限执行此操作") -> None:
    if not user_has_permission(user, permission_code):
        raise PermissionDenied(message)


class ErpPermissionRequiredMixin(UserPassesTestMixin):
    permission_required = ""
    permission_denied_message = "无权限执行此操作"

    def test_func(self):
        return user_has_permission(self.request.user, self.permission_required)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return redirect("login")
        messages.error(self.request, self.permission_denied_message)
        raise PermissionDenied(self.permission_denied_message)
