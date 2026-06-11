from __future__ import annotations

from django.http import FileResponse, Http404

from .models import Attachment
from .permissions import can_access_attachment, filter_attachments_for_user
from .services import resolve_export_file_path


def build_attachment_panel(user, source_doc_type: str, source_doc_id: int, source_doc_no: str = "") -> dict:
    attachments = filter_attachments_for_user(
        Attachment.objects.filter(
            source_doc_type=source_doc_type,
            source_doc_id=source_doc_id,
            status=Attachment.AttachmentStatus.ACTIVE,
        )
        .select_related("uploaded_by")
        .order_by("-uploaded_at"),
        user,
    )
    return {
        "source_doc_type": source_doc_type,
        "source_doc_id": source_doc_id,
        "source_doc_no": source_doc_no,
        "attachments": [
            {
                "attachment": attachment,
                "can_download": can_access_attachment(user, attachment),
            }
            for attachment in attachments
        ],
    }


def export_file_response(result):
    if not result.success:
        raise Http404(result.message or "导出失败")
    file_path = resolve_export_file_path(result.data.get("file_path", ""), result.data.get("export_no", ""))
    if not file_path:
        raise Http404("导出文件不存在")
    return FileResponse(
        file_path.open("rb"),
        as_attachment=True,
        filename=result.data.get("filename") or file_path.name,
        content_type="text/csv",
    )
