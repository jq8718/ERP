from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import DetailView, TemplateView

from accounts.permissions import ErpPermissionRequiredMixin, PermissionCode, user_has_permission
from system.view_helpers import ErpListView

from .models import Attachment, AttachmentAccessLog, ExportLog, ImportJob, InitializationJob, PrintLog
from .permissions import (
    can_access_attachment,
    can_access_attachment_source,
    can_access_source_doc,
    filter_attachments_for_user,
    resolve_source_doc_no,
    source_doc_type_choices_for_user,
)
from .services import (
    ALLOWED_EXTENSIONS,
    MAX_ATTACHMENT_SIZE,
    delete_attachment,
    record_attachment_access,
    register_attachment,
    resolve_attachment_storage_path,
    resolve_export_file_path,
)


class AttachmentListView(ErpListView):
    model = Attachment
    page_title = "附件"
    create_url_name = "files:attachment_upload"
    detail_url_name = "files:attachment_detail"
    columns = (
        ("文件名", "original_filename"),
        ("来源", "source_doc_type"),
        ("来源单号", "source_doc_no"),
        ("大小", "file_size"),
        ("状态", "get_status_display"),
        ("上传时间", "uploaded_at"),
    )
    ordering = ["-uploaded_at"]
    search_fields = (
        "attachment_no",
        "original_filename",
        "source_doc_type",
        "source_doc_no",
        "uploaded_by__username",
    )
    status_filter_field = "status"

    def get_queryset(self):
        return filter_attachments_for_user(super().get_queryset(), self.request.user).select_related("uploaded_by")


class AttachmentAccessLogListView(ErpPermissionRequiredMixin, ErpListView):
    model = AttachmentAccessLog
    template_name = "files/attachment_access_log_list.html"
    page_title = "附件访问日志"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看附件访问日志权限"
    columns = (
        ("时间", "created_at"),
        ("动作", "action"),
        ("附件", "attachment.original_filename"),
        ("来源单号", "attachment.source_doc_no"),
        ("操作人", "operator.username"),
        ("IP", "ip_address"),
    )
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = super().get_queryset().select_related("attachment", "operator")
        query = self.request.GET.get("q", "").strip()
        action = self.request.GET.get("action", "").strip()
        operator = self.request.GET.get("operator", "").strip()
        date_from = self.request.GET.get("date_from", "").strip()
        date_to = self.request.GET.get("date_to", "").strip()

        if query:
            queryset = queryset.filter(
                Q(attachment__original_filename__icontains=query)
                | Q(attachment__source_doc_no__icontains=query)
                | Q(attachment__attachment_no__icontains=query)
            )
        if action:
            queryset = queryset.filter(action=action)
        if operator:
            queryset = queryset.filter(operator__username__icontains=operator)
        parsed_date_from = parse_date(date_from)
        parsed_date_to = parse_date(date_to)
        if parsed_date_from:
            queryset = queryset.filter(created_at__date__gte=parsed_date_from)
        if parsed_date_to:
            queryset = queryset.filter(created_at__date__lte=parsed_date_to)
        return queryset.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filters"] = {
            "q": self.request.GET.get("q", "").strip(),
            "action": self.request.GET.get("action", "").strip(),
            "operator": self.request.GET.get("operator", "").strip(),
            "date_from": self.request.GET.get("date_from", "").strip(),
            "date_to": self.request.GET.get("date_to", "").strip(),
        }
        context["action_options"] = (
            ("download", "下载"),
            ("delete", "删除"),
        )
        return context


class ImportJobListView(ErpPermissionRequiredMixin, ErpListView):
    model = ImportJob
    page_title = "导入任务"
    detail_url_name = "files:import_job_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看导入任务权限"
    columns = (
        ("任务号", "job_no"),
        ("模板类型", "template_type"),
        ("状态", "get_status_display"),
        ("成功行", "success_count"),
        ("失败行", "failed_count"),
        ("创建人", "created_by.username"),
        ("开始时间", "started_at"),
        ("完成时间", "finished_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("job_no", "template_type", "created_by__username")
    status_filter_field = "status"

    def get_queryset(self):
        return super().get_queryset().select_related("created_by")


class InitializationJobListView(ErpPermissionRequiredMixin, ErpListView):
    model = InitializationJob
    page_title = "初始化任务"
    detail_url_name = "files:initialization_job_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看初始化任务权限"
    columns = (
        ("任务号", "job_no"),
        ("模板类型", "template_type"),
        ("状态", "get_status_display"),
        ("成功行", "success_count"),
        ("失败行", "failed_count"),
        ("确认人", "confirmed_by.username"),
        ("创建人", "created_by.username"),
        ("创建时间", "created_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("job_no", "template_type", "created_by__username", "confirmed_by__username")
    status_filter_field = "status"

    def get_queryset(self):
        return super().get_queryset().select_related("created_by", "confirmed_by")


class ImportJobDetailView(ErpPermissionRequiredMixin, DetailView):
    model = ImportJob
    template_name = "files/import_job_detail.html"
    context_object_name = "job"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看导入任务权限"

    def get_queryset(self):
        return super().get_queryset().select_related("created_by", "source_file")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"导入任务 {self.object.job_no}"
        context["errors"] = self.object.error_summary.get("errors", []) if isinstance(self.object.error_summary, dict) else []
        return context


class InitializationJobDetailView(ErpPermissionRequiredMixin, DetailView):
    model = InitializationJob
    template_name = "files/initialization_job_detail.html"
    context_object_name = "job"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看初始化任务权限"

    def test_func(self):
        return user_has_permission(self.request.user, PermissionCode.ADMIN_PERMISSION_MANAGE) or user_has_permission(
            self.request.user,
            PermissionCode.INVENTORY_PROCESS,
        )

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return redirect("login")
        raise PermissionDenied(self.permission_denied_message)

    def get_queryset(self):
        return super().get_queryset().select_related("created_by", "confirmed_by", "source_file")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"初始化任务 {self.object.job_no}"
        context["errors"] = self.object.error_summary.get("errors", []) if isinstance(self.object.error_summary, dict) else []
        context["preview_rows"] = self.object.error_summary.get("preview_rows", []) if isinstance(self.object.error_summary, dict) else []
        can_process_inventory = user_has_permission(self.request.user, PermissionCode.INVENTORY_PROCESS)
        context["can_confirm_initial_inventory"] = (
            can_process_inventory
            and self.object.template_type == "initial_inventory"
            and self.object.status == InitializationJob.JobStatus.PENDING_CONFIRM
        )
        context["can_cancel_initial_inventory"] = (
            can_process_inventory
            and self.object.template_type == "initial_inventory"
            and self.object.status in [InitializationJob.JobStatus.PENDING_CONFIRM, InitializationJob.JobStatus.SUCCESS]
        )
        return context


class ExportLogListView(ErpPermissionRequiredMixin, ErpListView):
    model = ExportLog
    page_title = "导出日志"
    detail_url_name = "files:export_log_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看导出日志权限"
    columns = (
        ("导出号", "export_no"),
        ("模块", "module"),
        ("行数", "row_count"),
        ("导出人", "exported_by.username"),
        ("导出时间", "created_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("export_no", "module", "exported_by__username")

    def get_queryset(self):
        return super().get_queryset().select_related("exported_by")


class ExportLogDetailView(ErpPermissionRequiredMixin, DetailView):
    model = ExportLog
    template_name = "files/export_log_detail.html"
    context_object_name = "export_log"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看导出日志权限"

    def get_queryset(self):
        return super().get_queryset().select_related("exported_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_download_file = _can_download_export_file(self.request.user, self.object)
        file_path = resolve_export_file_path(self.object.file_path, self.object.export_no)
        context["page_title"] = f"导出日志 {self.object.export_no}"
        context["file_exists"] = file_path is not None
        context["can_download_file"] = context["file_exists"] and can_download_file
        return context


class ExportLogDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        if not user_has_permission(request.user, PermissionCode.ADMIN_PERMISSION_MANAGE):
            raise Http404("导出文件不存在")
        export_log = ExportLog.objects.filter(pk=pk).first()
        if not export_log or not export_log.file_path:
            raise Http404("导出文件不存在")
        if not _can_download_export_file(request.user, export_log):
            raise Http404("导出文件不存在")
        file_path = resolve_export_file_path(export_log.file_path, export_log.export_no)
        if not file_path:
            raise Http404("导出文件不存在")
        return FileResponse(
            file_path.open("rb"),
            as_attachment=True,
            filename=f"{export_log.export_no}.csv",
            content_type="text/csv",
        )


def _can_download_export_file(user, export_log: ExportLog) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    return bool(export_log.exported_by_id and export_log.exported_by_id == user.id)


class PrintLogListView(ErpPermissionRequiredMixin, ErpListView):
    model = PrintLog
    page_title = "打印日志"
    detail_url_name = "files:print_log_detail"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看打印日志权限"
    columns = (
        ("打印号", "print_no"),
        ("模板", "template_type"),
        ("来源类型", "source_doc_type"),
        ("来源单号", "source_doc_no"),
        ("打印人", "printed_by.username"),
        ("打印时间", "created_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("print_no", "template_type", "source_doc_type", "source_doc_no", "printed_by__username")

    def get_queryset(self):
        return super().get_queryset().select_related("printed_by")


class PrintLogDetailView(ErpPermissionRequiredMixin, DetailView):
    model = PrintLog
    template_name = "files/print_log_detail.html"
    context_object_name = "print_log"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看打印日志权限"

    def get_queryset(self):
        return super().get_queryset().select_related("printed_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印日志 {self.object.print_no}"
        return context


class AttachmentUploadView(LoginRequiredMixin, TemplateView):
    template_name = "files/attachment_upload.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "上传附件"
        context["source_doc_type_choices"] = source_doc_type_choices_for_user(self.request.user)
        return context

    def post(self, request):
        uploaded_file = request.FILES.get("file")
        source_doc_type = request.POST.get("source_doc_type", "").strip()
        is_sensitive = request.POST.get("is_sensitive") == "on"

        try:
            source_doc_id = int(request.POST.get("source_doc_id", "0"))
        except ValueError:
            source_doc_id = 0

        if not uploaded_file or not source_doc_type or source_doc_id <= 0:
            messages.error(request, "来源单据和附件文件必须填写")
            return redirect("files:attachment_upload")
        if not can_access_source_doc(request.user, source_doc_type, source_doc_id):
            messages.error(request, "来源单据不存在或无权限上传附件")
            return redirect("files:attachment_upload")
        source_doc_no = resolve_source_doc_no(source_doc_type, source_doc_id)
        if not source_doc_no:
            messages.error(request, "来源单据不存在或无权限上传附件")
            return redirect("files:attachment_upload")

        suffix = Path(uploaded_file.name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            messages.error(request, "附件类型不允许上传")
            return redirect("files:attachment_upload")
        if uploaded_file.size > MAX_ATTACHMENT_SIZE:
            messages.error(request, "附件大小超过限制")
            return redirect("files:attachment_upload")

        file_bytes = uploaded_file.read()
        checksum = sha256(file_bytes).hexdigest()
        stored_filename = f"{uuid4().hex}{suffix}"
        file_path = f"attachments/{stored_filename}"
        default_storage.save(file_path, ContentFile(file_bytes))

        result = register_attachment(
            source_doc_type=source_doc_type,
            source_doc_id=source_doc_id,
            source_doc_no=source_doc_no,
            original_filename=uploaded_file.name,
            stored_filename=stored_filename,
            file_path=file_path,
            file_size=uploaded_file.size,
            mime_type=getattr(uploaded_file, "content_type", "") or "",
            checksum_sha256=checksum,
            is_sensitive=is_sensitive,
            uploaded_by_id=request.user.id,
        )
        if not result.success:
            default_storage.delete(file_path)
            messages.error(request, result.message or result.error_code or "附件上传失败")
            return redirect("files:attachment_upload")

        messages.success(request, result.message)
        return redirect("files:attachment_detail", pk=result.data["attachment_id"])


class AttachmentDetailView(LoginRequiredMixin, DetailView):
    model = Attachment
    template_name = "files/attachment_detail.html"
    context_object_name = "attachment"

    def get_queryset(self):
        return filter_attachments_for_user(
            super().get_queryset().select_related("uploaded_by", "deleted_by").prefetch_related("access_logs__operator"),
            self.request.user,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"附件 {self.object.original_filename}"
        context["can_download"] = self.object.status == Attachment.AttachmentStatus.ACTIVE and can_access_attachment(self.request.user, self.object)
        context["can_delete"] = (
            self.object.status == Attachment.AttachmentStatus.ACTIVE
            and can_access_attachment_source(self.request.user, self.object)
            and user_has_permission(self.request.user, PermissionCode.ATTACHMENT_DELETE)
        )
        return context


class AttachmentDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        attachment = Attachment.objects.filter(pk=pk).first()
        if not attachment or not can_access_attachment(request.user, attachment):
            raise Http404("附件不存在或无权限访问")
        file_path = resolve_attachment_storage_path(attachment.file_path)
        if not file_path:
            raise Http404("附件文件不存在")

        try:
            file_handle = default_storage.open(file_path, "rb")
        except (FileNotFoundError, OSError, ValueError, SuspiciousFileOperation) as exc:
            raise Http404("附件文件不存在") from exc

        result = record_attachment_access(
            pk,
            request.user.id,
            action="download",
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        if not result.success:
            file_handle.close()
            raise Http404(result.message or "附件不存在")

        return FileResponse(file_handle, as_attachment=True, filename=attachment.original_filename, content_type=attachment.mime_type or None)


class AttachmentDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not user_has_permission(request.user, PermissionCode.ATTACHMENT_DELETE):
            messages.error(request, "缺少附件删除权限")
            return redirect("files:attachment_detail", pk=pk)
        attachment = Attachment.objects.filter(pk=pk).first()
        if not attachment or not can_access_attachment_source(request.user, attachment):
            raise Http404("附件不存在或无权限访问")
        reason = request.POST.get("reason", "").strip()
        result = delete_attachment(pk, request.user.id, reason)
        if result.success:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "附件删除失败")
        return redirect("files:attachment_detail", pk=pk)
