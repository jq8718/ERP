from django.contrib import admin

from .models import (
    AuditLog,
    BackgroundJob,
    Backup,
    DocumentSequence,
    PendingEvent,
    ReleaseRecord,
    SavedFilter,
    SystemSetting,
)


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ("prefix", "sequence_date", "current_value")
    list_filter = ("prefix", "sequence_date")


@admin.register(PendingEvent)
class PendingEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "idempotency_key", "status", "retry_count", "next_retry_at", "created_at")
    list_filter = ("event_type", "status")
    search_fields = ("event_type", "idempotency_key", "last_error")


@admin.register(BackgroundJob)
class BackgroundJobAdmin(admin.ModelAdmin):
    list_display = ("job_no", "job_type", "trigger_type", "status", "started_at", "finished_at", "created_at")
    list_filter = ("job_type", "status", "trigger_type")
    search_fields = ("job_no", "job_type", "error_message")


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("setting_key", "value_type", "updated_at", "updated_by")
    search_fields = ("setting_key", "setting_value")


@admin.register(SavedFilter)
class SavedFilterAdmin(admin.ModelAdmin):
    list_display = ("user", "module", "filter_name", "is_default")
    list_filter = ("module", "is_default")
    search_fields = ("user__username", "module", "filter_name")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("log_no", "operator", "action", "source_doc_type", "source_doc_no", "created_at")
    list_filter = ("action", "source_doc_type", "created_at")
    search_fields = ("log_no", "operator__username", "source_doc_no", "action")


@admin.register(Backup)
class BackupAdmin(admin.ModelAdmin):
    list_display = ("backup_no", "backup_type", "file_size", "status", "created_at", "created_by")
    list_filter = ("backup_type", "status", "created_at")
    search_fields = ("backup_no", "file_path", "checksum_sha256")


@admin.register(ReleaseRecord)
class ReleaseRecordAdmin(admin.ModelAdmin):
    list_display = ("version_no", "released_at", "released_by")
    search_fields = ("version_no", "summary")
