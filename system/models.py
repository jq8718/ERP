from django.conf import settings
from django.db import models


class DocumentSequence(models.Model):
    prefix = models.CharField(max_length=24)
    sequence_date = models.DateField()
    current_value = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "document_sequences"
        constraints = [
            models.UniqueConstraint(fields=["prefix", "sequence_date"], name="uq_document_sequence_prefix_date"),
        ]

    def __str__(self):
        return f"{self.prefix} - {self.sequence_date} - {self.current_value}"


class PendingEvent(models.Model):
    class EventStatus(models.TextChoices):
        PENDING = "pending", "待处理"
        RUNNING = "running", "处理中"
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    event_type = models.CharField(max_length=80)
    idempotency_key = models.CharField(max_length=200, unique=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=EventStatus.choices, default=EventStatus.PENDING)
    retry_count = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pending_events"
        indexes = [
            models.Index(fields=["status", "event_type", "next_retry_at"]),
        ]

    def __str__(self):
        return f"{self.event_type} - {self.idempotency_key} - {self.get_status_display()}"


class BackgroundJob(models.Model):
    class JobStatus(models.TextChoices):
        PENDING = "pending", "待执行"
        RUNNING = "running", "运行中"
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    job_no = models.CharField(max_length=100, unique=True)
    job_type = models.CharField(max_length=80)
    trigger_type = models.CharField(max_length=40, blank=True)
    input_params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    result_summary = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "background_jobs"
        indexes = [
            models.Index(fields=["job_type", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.job_no} - {self.job_type} - {self.get_status_display()}"


class SystemSetting(models.Model):
    setting_key = models.CharField(max_length=120, unique=True)
    setting_value = models.TextField(blank=True)
    value_type = models.CharField(max_length=40, default="string")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "system_settings"

    def __str__(self):
        return self.setting_key


class SavedFilter(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    module = models.CharField(max_length=80)
    filter_name = models.CharField(max_length=120)
    filter_json = models.JSONField(default=dict)
    is_default = models.BooleanField(default=False)

    class Meta:
        db_table = "saved_filters"
        constraints = [
            models.UniqueConstraint(fields=["user", "module", "filter_name"], name="uq_saved_filter_name"),
        ]

    def __str__(self):
        return f"{self.user} - {self.module} - {self.filter_name}"


class AuditLog(models.Model):
    log_no = models.CharField(max_length=100, unique=True)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=100)
    source_doc_type = models.CharField(max_length=80)
    source_doc_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_doc_no = models.CharField(max_length=100, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    before_snapshot = models.JSONField(default=dict, blank=True)
    after_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_logs"
        indexes = [
            models.Index(fields=["operator", "created_at"]),
            models.Index(fields=["source_doc_type", "source_doc_id"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.log_no} - {self.action} - {self.source_doc_no}"


class Backup(models.Model):
    class BackupStatus(models.TextChoices):
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"

    backup_no = models.CharField(max_length=100, unique=True)
    backup_type = models.CharField(max_length=40)
    file_path = models.CharField(max_length=500)
    file_size = models.PositiveBigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=BackupStatus.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        db_table = "backups"

    def __str__(self):
        return f"{self.backup_no} - {self.backup_type} - {self.get_status_display()}"


class ReleaseRecord(models.Model):
    version_no = models.CharField(max_length=80, unique=True)
    released_at = models.DateTimeField()
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    summary = models.TextField(blank=True)

    class Meta:
        db_table = "release_records"

    def __str__(self):
        return f"{self.version_no} - {self.released_at:%Y-%m-%d %H:%M:%S}"
