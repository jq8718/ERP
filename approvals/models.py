from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Approval(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审批"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已驳回"
        TRANSFERRED = "transferred", "已转交"
        WITHDRAWN = "withdrawn", "已撤回"

    approval_no = models.CharField(max_length=100, unique=True)
    approval_type = models.CharField(max_length=80)
    source_content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    source_object_id = models.PositiveBigIntegerField()
    source_object = GenericForeignKey("source_content_type", "source_object_id")
    source_doc_type = models.CharField(max_length=80)
    source_no = models.CharField(max_length=100)
    source_title = models.CharField(max_length=200)
    source_summary = models.JSONField(default=dict, blank=True)
    current_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pending_approvals",
    )
    return_to_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_approvals",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "approvals"
        indexes = [
            models.Index(fields=["current_approver", "status", "submitted_at"]),
            models.Index(fields=["source_doc_type", "source_object_id"]),
            models.Index(fields=["approval_type", "status"]),
        ]


class ApprovalRule(models.Model):
    class RuleStatus(models.TextChoices):
        ACTIVE = "active", "启用"
        INACTIVE = "inactive", "停用"

    doc_type = models.CharField(max_length=80)
    condition_json = models.JSONField(default=dict, blank=True)
    level_no = models.PositiveIntegerField()
    approver_role = models.ForeignKey("accounts.Role", null=True, blank=True, on_delete=models.PROTECT)
    approver_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    allow_auto_skip_same_user = models.BooleanField(default=True)
    require_second_verify = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=RuleStatus.choices, default=RuleStatus.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    remark = models.TextField(blank=True)

    class Meta:
        db_table = "approval_rules"
        indexes = [
            models.Index(fields=["doc_type", "status", "level_no"]),
            models.Index(fields=["approver_user", "status"]),
            models.Index(fields=["approver_role", "status"]),
        ]


class ApprovalLog(models.Model):
    class Action(models.TextChoices):
        SUBMIT = "submit", "提交"
        APPROVE = "approve", "同意"
        REJECT = "reject", "驳回"
        TRANSFER = "transfer", "转交"
        ADD_APPROVER = "add_approver", "加签"
        RETURN_TO_EDIT = "return_to_edit", "退回修改"
        WITHDRAW = "withdraw", "撤回"
        AUTO_SKIP = "auto_skip", "自动跳过"

    approval = models.ForeignKey(Approval, on_delete=models.CASCADE, related_name="logs")
    action = models.CharField(max_length=32, choices=Action.choices)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    from_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    to_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    comment = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "approval_logs"
        indexes = [
            models.Index(fields=["approval", "created_at"]),
            models.Index(fields=["operator", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]
