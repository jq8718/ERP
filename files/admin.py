from django.contrib import admin

from .models import Attachment, AttachmentAccessLog, ExportLog, ImportJob, InitializationJob, PrintLog


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("attachment_no", "source_doc_type", "source_doc_no", "original_filename", "file_size", "status", "uploaded_at")
    list_filter = ("source_doc_type", "status", "scan_status", "is_sensitive")
    search_fields = ("attachment_no", "source_doc_no", "original_filename", "checksum_sha256")


@admin.register(AttachmentAccessLog)
class AttachmentAccessLogAdmin(admin.ModelAdmin):
    list_display = ("attachment", "operator", "action", "ip_address", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("attachment__attachment_no", "operator__username", "ip_address")


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("job_no", "template_type", "template_version", "status", "success_count", "failed_count", "created_at")
    list_filter = ("template_type", "status")
    search_fields = ("job_no", "template_type")


@admin.register(InitializationJob)
class InitializationJobAdmin(admin.ModelAdmin):
    list_display = ("job_no", "template_type", "status", "success_count", "failed_count", "confirmed_by", "confirmed_at")
    list_filter = ("template_type", "status")
    search_fields = ("job_no", "template_type")


@admin.register(ExportLog)
class ExportLogAdmin(admin.ModelAdmin):
    list_display = ("export_no", "module", "row_count", "exported_by", "created_at")
    list_filter = ("module", "created_at")
    search_fields = ("export_no", "module", "exported_by__username")


@admin.register(PrintLog)
class PrintLogAdmin(admin.ModelAdmin):
    list_display = ("print_no", "template_type", "source_doc_type", "source_doc_no", "printed_by", "created_at")
    list_filter = ("template_type", "source_doc_type", "created_at")
    search_fields = ("print_no", "source_doc_no", "printed_by__username")
