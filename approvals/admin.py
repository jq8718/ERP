from django.contrib import admin

from .models import Approval, ApprovalLog, ApprovalRule


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ("approval_no", "approval_type", "source_no", "current_approver", "status", "submitted_at")
    list_filter = ("approval_type", "status", "source_doc_type")
    search_fields = ("approval_no", "source_no", "source_title")


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ("doc_type", "level_no", "approver_role", "approver_user", "status", "require_second_verify")
    list_filter = ("doc_type", "status", "require_second_verify")
    search_fields = ("doc_type", "remark")


@admin.register(ApprovalLog)
class ApprovalLogAdmin(admin.ModelAdmin):
    list_display = ("approval", "action", "operator", "from_approver", "to_approver", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("approval__approval_no", "comment", "operator__username")
