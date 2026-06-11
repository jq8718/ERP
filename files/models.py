from django.conf import settings
from django.db import models


class Attachment(models.Model):
    class AttachmentStatus(models.TextChoices):
        ACTIVE = "active", "有效"
        DELETED = "deleted", "已删除"

    class ScanStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "无需扫描"
        PENDING = "pending", "待扫描"
        PASSED = "passed", "通过"
        FAILED = "failed", "失败"

    attachment_no = models.CharField(max_length=100, unique=True)
    source_doc_type = models.CharField(max_length=80)
    source_doc_id = models.PositiveBigIntegerField()
    source_doc_no = models.CharField(max_length=100, blank=True)
    original_filename = models.CharField(max_length=255)
    stored_filename = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500)
    file_size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True)
    checksum_sha256 = models.CharField(max_length=128, blank=True)
    is_sensitive = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=AttachmentStatus.choices, default=AttachmentStatus.ACTIVE)
    scan_status = models.CharField(max_length=24, choices=ScanStatus.choices, default=ScanStatus.NOT_REQUIRED)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="uploaded_attachments",
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    delete_reason = models.TextField(blank=True)

    class Meta:
        db_table = "attachments"
        indexes = [
            models.Index(fields=["source_doc_type", "source_doc_id", "status"]),
            models.Index(fields=["uploaded_by", "uploaded_at"]),
            models.Index(fields=["checksum_sha256"]),
        ]


class AttachmentAccessLog(models.Model):
    attachment = models.ForeignKey(Attachment, on_delete=models.CASCADE, related_name="access_logs")
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "attachment_access_logs"
        indexes = [
            models.Index(fields=["attachment", "created_at"]),
            models.Index(fields=["operator", "created_at"]),
        ]


class ImportJob(models.Model):
    class JobStatus(models.TextChoices):
        PENDING = "pending", "待执行"
        VALIDATING = "validating", "校验中"
        IMPORTING = "importing", "导入中"
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    job_no = models.CharField(max_length=100, unique=True)
    template_type = models.CharField(max_length=80)
    template_version = models.CharField(max_length=40, blank=True)
    source_file = models.ForeignKey(Attachment, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING)
    success_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error_summary = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "import_jobs"
        indexes = [
            models.Index(fields=["template_type", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]


class InitializationJob(models.Model):
    class JobStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        VALIDATING = "validating", "校验中"
        PENDING_CONFIRM = "pending_confirm", "待确认"
        IMPORTING = "importing", "导入中"
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    job_no = models.CharField(max_length=100, unique=True)
    template_type = models.CharField(max_length=80)
    source_file = models.ForeignKey(Attachment, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=24, choices=JobStatus.choices, default=JobStatus.DRAFT)
    success_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error_summary = models.JSONField(default=dict, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="confirmed_initialization_jobs",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "initialization_jobs"
        indexes = [
            models.Index(fields=["template_type", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]


class ExportLog(models.Model):
    export_no = models.CharField(max_length=100, unique=True)
    module = models.CharField(max_length=80)
    filter_json = models.JSONField(default=dict, blank=True)
    file_path = models.CharField(max_length=500, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    exported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "export_logs"
        indexes = [
            models.Index(fields=["module", "created_at"]),
            models.Index(fields=["exported_by", "created_at"]),
        ]


class PrintLog(models.Model):
    print_no = models.CharField(max_length=100, unique=True)
    template_type = models.CharField(max_length=80)
    source_doc_type = models.CharField(max_length=80)
    source_doc_id = models.PositiveBigIntegerField()
    source_doc_no = models.CharField(max_length=100, blank=True)
    printed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "print_logs"
        indexes = [
            models.Index(fields=["template_type", "created_at"]),
            models.Index(fields=["source_doc_type", "source_doc_id"]),
            models.Index(fields=["printed_by", "created_at"]),
        ]
