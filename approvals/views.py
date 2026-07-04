from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect
from django.views import View
from django.views.generic import DetailView
from django.views.generic.edit import CreateView

from accounts.permissions import ErpPermissionRequiredMixin, PermissionCode, user_has_permission
from files.view_helpers import build_attachment_panel
from system.display import code_label
from system.view_helpers import ErpListView

from .forms import ApprovalRuleForm
from .models import Approval, ApprovalLog
from .models import ApprovalRule
from .services import apply_approval_action


class ApprovalListView(ErpListView):
    model = Approval
    page_title = "审批"
    detail_url_name = "approvals:approval_detail"
    page_actions = (("审批规则", "approvals:approval_rule_list", ""),)
    page_action_permissions = {"approvals:approval_rule_list": PermissionCode.ADMIN_PERMISSION_MANAGE}
    columns = (
        ("审批单号", "approval_no"),
        ("类型", "approval_type"),
        ("来源标题", "source_title"),
        ("当前审批人", "current_approver"),
        ("状态", "get_status_display"),
        ("提交时间", "submitted_at"),
    )
    ordering = ["-submitted_at"]
    search_fields = (
        "approval_no",
        "approval_type",
        "source_no",
        "source_title",
        "current_approver__username",
        "submitted_by__username",
    )
    status_filter_field = "status"

    def get_queryset(self):
        return _filter_approvals_for_user(super().get_queryset(), self.request.user).select_related(
            "current_approver",
            "submitted_by",
        )


class ApprovalDetailView(LoginRequiredMixin, DetailView):
    model = Approval
    template_name = "approvals/approval_detail.html"
    context_object_name = "approval"

    def get_queryset(self):
        return (
            _filter_approvals_for_user(super().get_queryset(), self.request.user)
            .select_related("current_approver", "return_to_approver", "submitted_by", "source_content_type")
            .prefetch_related("logs__operator", "logs__from_approver", "logs__to_approver")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"审批 {self.object.approval_no}"
        context["can_process"] = (
            self.object.status == Approval.Status.PENDING and self.object.current_approver_id == self.request.user.id
        )
        context["can_withdraw"] = (
            self.object.status == Approval.Status.PENDING and self.object.submitted_by_id == self.request.user.id
        )
        context["transfer_users"] = (
            get_user_model()
            .objects.filter(is_active=True, is_deleted=False)
            .exclude(id=self.request.user.id)
            .order_by("username")
        )
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "approval",
            self.object.id,
            self.object.approval_no,
        )
        return context


class ApprovalActionView(LoginRequiredMixin, View):
    allowed_actions = {
        ApprovalLog.Action.APPROVE,
        ApprovalLog.Action.REJECT,
        ApprovalLog.Action.TRANSFER,
        ApprovalLog.Action.ADD_APPROVER,
        ApprovalLog.Action.RETURN_TO_EDIT,
        ApprovalLog.Action.WITHDRAW,
    }

    def post(self, request, pk, action):
        if action not in self.allowed_actions:
            messages.error(request, "不支持的审批动作")
            return redirect("approvals:approval_detail", pk=pk)

        target_user_id = request.POST.get("target_user") or None
        result = apply_approval_action(
            approval_id=pk,
            action=action,
            operator_id=request.user.id,
            comment=request.POST.get("comment", "").strip(),
            target_user_id=int(target_user_id) if target_user_id else None,
            ip_address=_request_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "审批动作处理失败")
        return redirect("approvals:approval_detail", pk=pk)


class ApprovalRuleListView(ErpPermissionRequiredMixin, ErpListView):
    model = ApprovalRule
    page_title = "审批规则"
    create_url_name = "approvals:approval_rule_create"
    detail_url_name = "approvals:approval_rule_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少权限与审批规则管理权限"
    columns = (
        ("单据类型", "doc_type"),
        ("级次", "level_no"),
        ("审批角色", "approver_role"),
        ("审批人员", "approver_user"),
        ("二次验证", "require_second_verify"),
        ("状态", "get_status_display"),
    )
    ordering = ["doc_type", "level_no", "id"]
    search_fields = ("doc_type", "approver_role__role_name", "approver_user__username", "remark")
    status_filter_field = "status"

    def get_queryset(self):
        return super().get_queryset().select_related("approver_role", "approver_user")


class ApprovalRuleCreateView(ErpPermissionRequiredMixin, LoginRequiredMixin, CreateView):
    model = ApprovalRule
    form_class = ApprovalRuleForm
    template_name = "approvals/approval_rule_form.html"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少权限与审批规则管理权限"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建审批规则"
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, "审批规则已创建")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/approvals/rules/{self.object.pk}/"


class ApprovalRuleDetailView(ErpPermissionRequiredMixin, LoginRequiredMixin, DetailView):
    model = ApprovalRule
    template_name = "approvals/approval_rule_detail.html"
    context_object_name = "rule"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少权限与审批规则管理权限"

    def get_queryset(self):
        return super().get_queryset().select_related("approver_role", "approver_user", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"审批规则 {code_label(self.object.doc_type)}"
        return context


def _filter_approvals_for_user(queryset, user):
    if getattr(user, "is_superuser", False) or user_has_permission(user, PermissionCode.ADMIN_PERMISSION_MANAGE):
        return queryset
    return queryset.filter(Q(current_approver=user) | Q(submitted_by=user))


def _request_ip(request) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
