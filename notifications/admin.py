from django.contrib import admin

from .models import SystemMessage


@admin.register(SystemMessage)
class SystemMessageAdmin(admin.ModelAdmin):
    list_display = ("message_no", "receiver", "title", "level", "status", "source_doc_no", "created_at")
    list_filter = ("level", "status", "source_doc_type", "created_at")
    search_fields = ("message_no", "title", "receiver__username", "source_doc_no")
