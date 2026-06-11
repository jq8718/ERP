from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from notifications.models import SystemMessage
from notifications.services import create_system_message
from system.services import ServiceResult

from .models import Approval, ApprovalLog


def apply_approval_action(
    approval_id: int,
    action: str,
    operator_id: int,
    comment: str = "",
    target_user_id: int | None = None,
    second_verify_token: str | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
) -> ServiceResult:
    try:
        with transaction.atomic():
            approval = Approval.objects.select_for_update().get(id=approval_id)
            if approval.status != Approval.Status.PENDING:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前审批单状态不能操作")
            previous_approver_id = approval.current_approver_id
            if action == ApprovalLog.Action.WITHDRAW:
                if approval.submitted_by_id != operator_id:
                    return ServiceResult(False, "AUTH_NO_PERMISSION", "只有提交人可以撤回审批")
            elif approval.current_approver_id != operator_id:
                return ServiceResult(False, "AUTH_NO_PERMISSION", "只有当前审批人可以处理审批")

            if action in [ApprovalLog.Action.REJECT, ApprovalLog.Action.RETURN_TO_EDIT] and not comment:
                return ServiceResult(False, "APPROVAL_COMMENT_REQUIRED", "驳回或退回必须填写原因")

            if action == ApprovalLog.Action.APPROVE:
                if approval.return_to_approver_id:
                    approval.current_approver_id = approval.return_to_approver_id
                    approval.return_to_approver = None
                    approval.status = Approval.Status.PENDING
                else:
                    approval.status = Approval.Status.APPROVED
                    approval.finished_at = timezone.now()
                    approval.current_approver = None
            elif action == ApprovalLog.Action.REJECT:
                approval.status = Approval.Status.REJECTED
                approval.finished_at = timezone.now()
                approval.current_approver = None
                approval.return_to_approver = None
            elif action == ApprovalLog.Action.RETURN_TO_EDIT:
                approval.status = Approval.Status.REJECTED
                approval.finished_at = timezone.now()
                approval.current_approver = None
                approval.return_to_approver = None
            elif action == ApprovalLog.Action.TRANSFER:
                if not target_user_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "转交必须选择目标审批人")
                if target_user_id == previous_approver_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "不能转交给自己")
                approval.current_approver_id = target_user_id
                approval.status = Approval.Status.PENDING
            elif action == ApprovalLog.Action.ADD_APPROVER:
                if not target_user_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "加签必须选择目标审批人")
                if target_user_id == previous_approver_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "不能加签给自己")
                approval.return_to_approver_id = previous_approver_id
                approval.current_approver_id = target_user_id
                approval.status = Approval.Status.PENDING
            elif action == ApprovalLog.Action.WITHDRAW:
                approval.status = Approval.Status.WITHDRAWN
                approval.finished_at = timezone.now()
                approval.current_approver = None
                approval.return_to_approver = None
            else:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "不支持的审批动作")

            approval.save(update_fields=["status", "current_approver", "return_to_approver", "finished_at"])
            ApprovalLog.objects.create(
                approval=approval,
                action=action,
                operator_id=operator_id,
                from_approver_id=previous_approver_id or operator_id,
                to_approver_id=target_user_id if action in [ApprovalLog.Action.TRANSFER, ApprovalLog.Action.ADD_APPROVER] else None,
                comment=comment,
                ip_address=ip_address,
                user_agent=user_agent[:1000],
            )

            if action == ApprovalLog.Action.TRANSFER:
                create_system_message(
                    receiver_id=target_user_id,
                    title=f"审批转交：{approval.source_title}",
                    content=comment,
                    level=SystemMessage.Level.NORMAL,
                    source_doc_type="approval",
                    source_doc_id=approval.id,
                    source_doc_no=approval.approval_no,
                )
            elif action == ApprovalLog.Action.ADD_APPROVER:
                create_system_message(
                    receiver_id=target_user_id,
                    title=f"审批加签：{approval.source_title}",
                    content=comment,
                    level=SystemMessage.Level.NORMAL,
                    source_doc_type="approval",
                    source_doc_id=approval.id,
                    source_doc_no=approval.approval_no,
                )
            elif action == ApprovalLog.Action.APPROVE and approval.current_approver_id:
                create_system_message(
                    receiver_id=approval.current_approver_id,
                    title=f"加签已同意：{approval.source_title}",
                    content=comment,
                    level=SystemMessage.Level.NORMAL,
                    source_doc_type="approval",
                    source_doc_id=approval.id,
                    source_doc_no=approval.approval_no,
                )
            elif action == ApprovalLog.Action.WITHDRAW and previous_approver_id:
                create_system_message(
                    receiver_id=previous_approver_id,
                    title=f"审批已撤回：{approval.source_title}",
                    content=comment,
                    level=SystemMessage.Level.NORMAL,
                    source_doc_type="approval",
                    source_doc_id=approval.id,
                    source_doc_no=approval.approval_no,
                )
            elif approval.submitted_by_id:
                action_label = "退回修改" if action == ApprovalLog.Action.RETURN_TO_EDIT else approval.get_status_display()
                create_system_message(
                    receiver_id=approval.submitted_by_id,
                    title=f"审批{action_label}：{approval.source_title}",
                    content=comment,
                    level=SystemMessage.Level.NORMAL,
                    source_doc_type="approval",
                    source_doc_id=approval.id,
                    source_doc_no=approval.approval_no,
                )

    except Approval.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "审批单不存在")

    return ServiceResult(True, message="审批动作已处理", data={"approval_id": approval.id, "status": approval.status})
