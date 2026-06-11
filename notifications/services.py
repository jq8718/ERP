from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from system.services import ServiceResult, next_document_no

from .models import SystemMessage


def create_system_message(
    receiver_id: int,
    title: str,
    content: str = "",
    level: str = SystemMessage.Level.NORMAL,
    source_doc_type: str = "",
    source_doc_id: int | None = None,
    source_doc_no: str = "",
    action_url: str = "",
    suggested_action: str = "",
) -> SystemMessage:
    return SystemMessage.objects.create(
        message_no=next_document_no("MSG"),
        receiver_id=receiver_id,
        title=title,
        content=content,
        level=level,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        source_doc_no=source_doc_no,
        action_url=action_url,
        suggested_action=suggested_action,
    )


def mark_message_read(message_id: int, operator_id: int) -> ServiceResult:
    try:
        message = SystemMessage.objects.get(id=message_id, receiver_id=operator_id)
    except SystemMessage.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "系统消息不存在")
    if message.status in [SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]:
        return ServiceResult(True, message="消息状态无需更新", data={"message_id": message.id})
    message.status = SystemMessage.Status.READ
    message.read_at = timezone.now()
    message.save(update_fields=["status", "read_at"])
    return ServiceResult(True, message="消息已读", data={"message_id": message.id})


def snooze_message(message_id: int, operator_id: int, snoozed_until: datetime) -> ServiceResult:
    if snoozed_until <= timezone.now():
        return ServiceResult(False, "VALIDATION_ERROR", "稍后提醒时间必须晚于当前时间")
    with transaction.atomic():
        try:
            message = SystemMessage.objects.select_for_update().get(id=message_id, receiver_id=operator_id)
        except SystemMessage.DoesNotExist:
            return ServiceResult(False, "DOC_NOT_FOUND", "系统消息不存在")
        if message.status in [SystemMessage.Status.PROCESSED, SystemMessage.Status.CLOSED]:
            return ServiceResult(False, "STATE_INVALID_TRANSITION", "已处理或已关闭消息不能稍后提醒")
        now = timezone.now()
        message.status = SystemMessage.Status.SNOOZED
        message.snoozed_until = snoozed_until
        if not message.read_at:
            message.read_at = now
        message.save(update_fields=["status", "snoozed_until", "read_at"])
    return ServiceResult(True, message="已设置稍后提醒", data={"message_id": message.id, "snoozed_until": snoozed_until})


def refresh_due_snoozed_messages(receiver_id: int | None = None) -> int:
    queryset = SystemMessage.objects.filter(status=SystemMessage.Status.SNOOZED, snoozed_until__lte=timezone.now())
    if receiver_id is not None:
        queryset = queryset.filter(receiver_id=receiver_id)
    return queryset.update(status=SystemMessage.Status.UNREAD, snoozed_until=None, read_at=None)


def mark_message_processed(message_id: int, operator_id: int) -> ServiceResult:
    try:
        message = SystemMessage.objects.get(id=message_id, receiver_id=operator_id)
    except SystemMessage.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "系统消息不存在")
    if message.status == SystemMessage.Status.CLOSED:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "已关闭消息不能标记为已处理")
    message.status = SystemMessage.Status.PROCESSED
    message.processed_at = timezone.now()
    message.snoozed_until = None
    message.save(update_fields=["status", "processed_at", "snoozed_until"])
    return ServiceResult(True, message="消息已处理", data={"message_id": message.id})


def close_message(message_id: int, operator_id: int) -> ServiceResult:
    try:
        message = SystemMessage.objects.get(id=message_id, receiver_id=operator_id)
    except SystemMessage.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "系统消息不存在")
    if message.status == SystemMessage.Status.PROCESSED:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "已处理消息不能关闭")
    message.status = SystemMessage.Status.CLOSED
    message.processed_at = timezone.now()
    message.snoozed_until = None
    if not message.read_at:
        message.read_at = message.processed_at
    message.save(update_fields=["status", "read_at", "processed_at", "snoozed_until"])
    return ServiceResult(True, message="消息已关闭", data={"message_id": message.id})
