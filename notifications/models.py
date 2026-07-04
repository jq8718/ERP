from django.conf import settings
from django.db import models


class SystemMessage(models.Model):
    class Level(models.TextChoices):
        URGENT = "urgent", "紧急"
        NORMAL = "normal", "普通"
        INFO = "info", "通知"

    class Status(models.TextChoices):
        UNREAD = "unread", "未读"
        READ = "read", "已读"
        PROCESSED = "processed", "已处理"
        SNOOZED = "snoozed", "稍后提醒"
        CLOSED = "closed", "已关闭"

    message_no = models.CharField(max_length=100, unique=True)
    receiver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="system_messages")
    title = models.CharField(max_length=200)
    content = models.TextField(blank=True)
    level = models.CharField(max_length=16, choices=Level.choices, default=Level.NORMAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNREAD)
    source_doc_type = models.CharField(max_length=80, blank=True)
    source_doc_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_doc_no = models.CharField(max_length=100, blank=True)
    action_url = models.CharField(max_length=500, blank=True)
    suggested_action = models.CharField(max_length=200, blank=True)
    remind_at = models.DateTimeField(null=True, blank=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "system_messages"
        indexes = [
            models.Index(fields=["receiver", "status", "level", "created_at"]),
            models.Index(fields=["source_doc_type", "source_doc_id"]),
            models.Index(fields=["remind_at", "status"]),
        ]

    def __str__(self):
        return f"{self.message_no} - {self.title} - {self.get_status_display()}"
