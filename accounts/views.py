from django import forms
from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.http import Http404
from django.shortcuts import redirect
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView
from django.views.generic.edit import CreateView, UpdateView

from accounts.forms import AccountUserCreateForm, AccountUserPasswordResetForm, AccountUserUpdateForm, RoleForm
from accounts.permissions import ErpPermissionRequiredMixin, PermissionCode, require_erp_permission
from system.services import record_audit_log_from_request
from system.view_helpers import ErpListView, require_post_reason, require_second_verify

from .models import Permission, Role, User, UserSession


class ErpAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "inactive": "账号已停用、锁定或删除，请联系系统管理员。",
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.is_deleted or user.status != User.AccountStatus.ACTIVE:
            raise forms.ValidationError(
                self.error_messages["inactive"],
                code="inactive",
            )


class ErpLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = ErpAuthenticationForm
    redirect_authenticated_user = True


class ErpLogoutView(LogoutView):
    next_page = "login"


class ErpPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = "registration/password_change.html"
    success_url = reverse_lazy("password_change")

    def form_valid(self, form):
        messages.success(self.request, "登录密码已更新")
        return super().form_valid(form)


class AccountUserListView(ErpPermissionRequiredMixin, ErpListView):
    model = User
    page_title = "用户管理"
    create_url_name = "account_user_create"
    create_permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    detail_url_name = "account_user_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看用户权限"
    columns = (
        ("用户名", "username"),
        ("姓名", "display_name"),
        ("部门", "department"),
        ("职位", "position"),
        ("账号状态", "get_status_display"),
        ("安全级别", "get_security_level_display"),
    )
    ordering = ["username"]
    search_fields = ("username", "display_name", "department", "position", "email")
    status_filter_field = "status"
    field_filters = (
        {"label": "用户名", "param": "username", "field": "username", "placeholder": "用户名"},
        {"label": "姓名", "param": "display_name", "field": "display_name", "placeholder": "姓名"},
        {"label": "部门", "param": "department", "field": "department", "placeholder": "部门"},
        {"label": "职位", "param": "position", "field": "position", "placeholder": "职位"},
        {
            "label": "安全级别",
            "param": "security_level",
            "field": "security_level",
            "lookup": "exact",
            "type": "select",
            "choices": User.SecurityLevel.choices,
        },
    )

    def get_queryset(self):
        return super().get_queryset().prefetch_related("roles")


class AccountUserCreateView(ErpPermissionRequiredMixin, CreateView):
    model = User
    form_class = AccountUserCreateForm
    template_name = "accounts/user_form.html"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少维护用户权限"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["operator"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建用户"
        context["is_edit"] = False
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        role_ids = list(self.object.roles.order_by("id").values_list("id", flat=True))
        record_audit_log_from_request(
            self.request,
            "account_user_create",
            "user",
            self.object.id,
            self.object.username,
            after_snapshot=_user_snapshot(self.object, role_ids, form.cleaned_data["reason"]),
        )
        messages.success(self.request, "用户已创建")
        return response

    def get_success_url(self):
        return f"/users/{self.object.pk}/"


class AccountUserDetailView(ErpPermissionRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "account_user"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看用户权限"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("roles__permissions", "erp_sessions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.object
        permissions = Permission.objects.filter(roles__users=user, roles__status=Role.RoleStatus.ACTIVE).distinct().order_by(
            "permission_type",
            "permission_name",
        )
        context.update(
            {
                "page_title": f"用户 {user.username}",
                "effective_permissions": permissions,
                "recent_sessions": user.erp_sessions.order_by("-last_seen_at", "-created_at")[:10],
            }
        )
        return context


class AccountUserUpdateView(ErpPermissionRequiredMixin, UpdateView):
    model = User
    form_class = AccountUserUpdateForm
    template_name = "accounts/user_form.html"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少维护用户权限"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("roles")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["operator"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑用户 {self.object.username}"
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_roles = list(self.object.roles.order_by("id").values_list("id", flat=True))
        before_snapshot = _user_snapshot(self.object, before_roles)
        response = super().form_valid(form)
        after_roles = list(self.object.roles.order_by("id").values_list("id", flat=True))
        record_audit_log_from_request(
            self.request,
            "account_user_update",
            "user",
            self.object.id,
            self.object.username,
            before_snapshot=before_snapshot,
            after_snapshot=_user_snapshot(self.object, after_roles, form.cleaned_data["reason"]),
        )
        messages.success(self.request, "用户已更新")
        return response

    def get_success_url(self):
        return f"/users/{self.object.pk}/"


class AccountUserPasswordResetView(ErpPermissionRequiredMixin, View):
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少重置用户密码权限"
    template_name = "accounts/user_password_reset.html"

    def get(self, request, pk):
        user = self._get_user(pk)
        return render(
            request,
            self.template_name,
            {
                "page_title": f"重置密码 {user.username}",
                "account_user": user,
                "form": AccountUserPasswordResetForm(operator=request.user, target_user=user),
            },
        )

    def post(self, request, pk):
        user = self._get_user(pk)
        form = AccountUserPasswordResetForm(request.POST, operator=request.user, target_user=user)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"page_title": f"重置密码 {user.username}", "account_user": user, "form": form},
                status=200,
            )

        before_snapshot = {
            "username": user.username,
            "status": user.status,
            "is_active": user.is_active,
            "active_session_count": user.erp_sessions.filter(status=UserSession.SessionStatus.ACTIVE).count(),
        }
        user.set_password(form.cleaned_data["new_password1"])
        user.save(update_fields=["password"])
        revoked_count = user.erp_sessions.filter(status=UserSession.SessionStatus.ACTIVE).update(
            status=UserSession.SessionStatus.REVOKED,
            revoked_at=timezone.now(),
        )
        record_audit_log_from_request(
            request,
            "account_user_password_reset",
            "user",
            user.id,
            user.username,
            before_snapshot=before_snapshot,
            after_snapshot={
                "password_reset": True,
                "revoked_session_count": revoked_count,
                "reason": form.cleaned_data["reason"],
            },
        )
        messages.success(request, "用户密码已重置，相关有效会话已强制失效")
        return redirect("account_user_detail", pk=pk)

    def _get_user(self, pk):
        try:
            return User.objects.prefetch_related("erp_sessions").get(pk=pk)
        except User.DoesNotExist as exc:
            raise Http404("用户不存在") from exc


class RoleListView(ErpPermissionRequiredMixin, ErpListView):
    model = Role
    page_title = "角色管理"
    create_url_name = "role_create"
    create_permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    detail_url_name = "role_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看角色权限"
    columns = (
        ("角色编码", "role_code"),
        ("角色名称", "role_name"),
        ("状态", "get_status_display"),
        ("备注", "remark"),
    )
    ordering = ["role_code"]
    search_fields = ("role_code", "role_name", "remark")
    status_filter_field = "status"
    field_filters = (
        {"label": "角色编码", "param": "role_code", "field": "role_code", "placeholder": "角色编码"},
        {"label": "角色名称", "param": "role_name", "field": "role_name", "placeholder": "角色名称"},
    )

    def get_queryset(self):
        return super().get_queryset().prefetch_related("permissions", "users")


class RoleCreateView(ErpPermissionRequiredMixin, CreateView):
    model = Role
    form_class = RoleForm
    template_name = "accounts/role_form.html"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少维护角色权限"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["operator"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建角色"
        context["is_edit"] = False
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        permission_ids = list(self.object.permissions.order_by("id").values_list("id", flat=True))
        record_audit_log_from_request(
            self.request,
            "role_create",
            "role",
            self.object.id,
            self.object.role_code,
            after_snapshot=_role_snapshot(self.object, permission_ids, form.cleaned_data["reason"]),
        )
        messages.success(self.request, "角色已创建")
        return response

    def get_success_url(self):
        return f"/roles/{self.object.pk}/"


class RoleDetailView(ErpPermissionRequiredMixin, DetailView):
    model = Role
    template_name = "accounts/role_detail.html"
    context_object_name = "role"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看角色权限"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("permissions", "users")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"角色 {self.object.role_name}"
        return context


class RoleUpdateView(ErpPermissionRequiredMixin, UpdateView):
    model = Role
    form_class = RoleForm
    template_name = "accounts/role_form.html"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少维护角色权限"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("permissions")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["operator"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑角色 {self.object.role_name}"
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        before_permissions = list(self.object.permissions.order_by("id").values_list("id", flat=True))
        before_snapshot = _role_snapshot(self.object, before_permissions)
        response = super().form_valid(form)
        after_permissions = list(self.object.permissions.order_by("id").values_list("id", flat=True))
        record_audit_log_from_request(
            self.request,
            "role_update",
            "role",
            self.object.id,
            self.object.role_code,
            before_snapshot=before_snapshot,
            after_snapshot=_role_snapshot(self.object, after_permissions, form.cleaned_data["reason"]),
        )
        messages.success(self.request, "角色已更新")
        return response

    def get_success_url(self):
        return f"/roles/{self.object.pk}/"


class PermissionListView(ErpPermissionRequiredMixin, ErpListView):
    model = Permission
    page_title = "权限清单"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看权限清单权限"
    columns = (
        ("权限名称", "permission_name"),
        ("类型", "get_permission_type_display"),
        ("备注", "remark"),
    )
    ordering = ["permission_type", "permission_name"]
    search_fields = ("permission_code", "permission_name", "remark")
    filter_fields = (("类型", "permission_type", Permission.PermissionType.choices),)
    field_filters = (
        {"label": "权限编码", "param": "permission_code", "field": "permission_code", "placeholder": "权限编码"},
        {"label": "权限名称", "param": "permission_name", "field": "permission_name", "placeholder": "权限名称"},
        {"label": "备注", "param": "remark", "field": "remark", "placeholder": "备注"},
    )


def _user_snapshot(user: User, role_ids: list[int], reason: str = "") -> dict:
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "department": user.department,
        "position": user.position,
        "security_level": user.security_level,
        "status": user.status,
        "is_active": user.is_active,
        "is_deleted": user.is_deleted,
        "role_ids": role_ids,
        "reason": reason,
    }


def _role_snapshot(role: Role, permission_ids: list[int], reason: str = "") -> dict:
    return {
        "role_code": role.role_code,
        "role_name": role.role_name,
        "status": role.status,
        "permission_ids": permission_ids,
        "remark": role.remark,
        "reason": reason,
    }


class UserSessionListView(ErpPermissionRequiredMixin, ErpListView):
    model = UserSession
    page_title = "登录会话"
    detail_url_name = "user_session_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少管理登录会话权限"
    columns = (
        ("用户", "user.username"),
        ("状态", "get_status_display"),
        ("IP", "ip_address"),
        ("最近访问", "last_seen_at"),
        ("创建时间", "created_at"),
    )
    ordering = ["-last_seen_at", "-created_at"]
    search_fields = ("user__username", "user__display_name", "ip_address", "user_agent", "session_key")
    status_filter_field = "status"
    field_filters = (
        {"label": "用户名", "param": "username", "field": "user__username", "placeholder": "用户名"},
        {"label": "姓名", "param": "display_name", "field": "user__display_name", "placeholder": "姓名"},
        {"label": "IP", "param": "ip_address", "field": "ip_address", "placeholder": "IP 地址"},
        {"label": "会话", "param": "session_key", "field": "session_key", "placeholder": "会话标识"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("user")


class UserSessionDetailView(ErpPermissionRequiredMixin, DetailView):
    model = UserSession
    template_name = "accounts/user_session_detail.html"
    context_object_name = "user_session"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少管理登录会话权限"

    def get_queryset(self):
        return super().get_queryset().select_related("user")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"登录会话 {self.object.user.username}"
        context["is_current_session"] = self.object.session_key == self.request.session.session_key
        context["can_revoke"] = (
            self.object.status == UserSession.SessionStatus.ACTIVE
            and not context["is_current_session"]
        )
        return context


class UserSessionRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.ADMIN_PERMISSION_MANAGE, "缺少管理登录会话权限")

        verification_response = require_second_verify(request, "user_session_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(request, "user_session_detail", pk)
        if reason_response:
            return reason_response

        session = UserSession.objects.select_related("user").filter(pk=pk).first()
        if session is None:
            messages.error(request, "登录会话不存在")
            return redirect("user_session_list")
        if session.session_key == request.session.session_key:
            messages.error(request, "不能撤销自己的当前会话")
            return redirect("user_session_detail", pk=pk)
        if session.status != UserSession.SessionStatus.ACTIVE:
            messages.error(request, "该会话已不是有效状态")
            return redirect("user_session_detail", pk=pk)

        before_snapshot = {
            "status": session.status,
            "session_key": session.session_key,
            "user_id": session.user_id,
            "username": session.user.username,
        }
        session.status = UserSession.SessionStatus.REVOKED
        session.revoked_at = timezone.now()
        session.save(update_fields=["status", "revoked_at"])
        record_audit_log_from_request(
            request,
            "user_session_revoke",
            "user_session",
            session.id,
            session.session_key,
            before_snapshot=before_snapshot,
            after_snapshot={
                "status": session.status,
                "revoked_at": session.revoked_at.isoformat(),
                "reason": reason,
            },
        )
        messages.success(request, "登录会话已强制失效")
        return redirect("user_session_detail", pk=pk)
