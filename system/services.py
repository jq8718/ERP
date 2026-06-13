from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from system.display import code_label

from .models import AuditLog, BackgroundJob, DocumentSequence, PendingEvent


@dataclass
class ServiceResult:
    success: bool
    error_code: str | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    next_action: str | None = None


def next_document_no(prefix: str, sequence_date: date | None = None) -> str:
    sequence_date = sequence_date or timezone.localdate()
    if connection.in_atomic_block:
        return _next_document_no(prefix, sequence_date)
    with transaction.atomic():
        return _next_document_no(prefix, sequence_date)


def _next_document_no(prefix: str, sequence_date: date) -> str:
    sequence, _ = (
        DocumentSequence.objects.select_for_update()
        .get_or_create(prefix=prefix, sequence_date=sequence_date, defaults={"current_value": 0})
    )
    sequence.current_value += 1
    sequence.save(update_fields=["current_value"])
    return f"{prefix}{sequence_date:%Y%m%d}{sequence.current_value:04d}"


def enqueue_pending_event(event_type: str, idempotency_key: str, payload: dict[str, Any]) -> PendingEvent:
    event, _ = PendingEvent.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={"event_type": event_type, "payload": payload},
    )
    return event


def record_audit_log(
    action: str,
    source_doc_type: str,
    source_doc_id: int | None = None,
    source_doc_no: str = "",
    operator_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
    before_snapshot: dict | None = None,
    after_snapshot: dict | None = None,
) -> ServiceResult:
    log = AuditLog.objects.create(
        log_no=next_document_no("AUD"),
        operator_id=operator_id,
        action=action,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        source_doc_no=source_doc_no,
        ip_address=ip_address,
        user_agent=user_agent[:1000],
        before_snapshot=before_snapshot or {},
        after_snapshot=after_snapshot or {},
    )
    return ServiceResult(True, message="审计日志已记录", data={"audit_log_id": log.id, "log_no": log.log_no})


def record_audit_log_from_request(
    request,
    action: str,
    source_doc_type: str,
    source_doc_id: int | None = None,
    source_doc_no: str = "",
    before_snapshot: dict | None = None,
    after_snapshot: dict | None = None,
) -> ServiceResult:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip_address = forwarded_for.split(",")[0].strip() if forwarded_for else request.META.get("REMOTE_ADDR")
    return record_audit_log(
        action=action,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        source_doc_no=source_doc_no,
        operator_id=request.user.id if getattr(request, "user", None) and request.user.is_authenticated else None,
        ip_address=ip_address,
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
    )


def start_background_job(
    job_type: str,
    trigger_type: str = "manual",
    input_params: dict[str, Any] | None = None,
    job_no: str | None = None,
) -> ServiceResult:
    with transaction.atomic():
        _mark_stale_running_background_jobs(job_type)
        running_job = (
            BackgroundJob.objects.select_for_update()
            .filter(job_type=job_type, status=BackgroundJob.JobStatus.RUNNING)
            .order_by("started_at")
            .first()
        )
        if running_job:
            return ServiceResult(
                False,
                "SYSTEM_JOB_RUNNING",
                f"{code_label(job_type)}任务正在运行",
                data={"job_id": running_job.id, "job_no": running_job.job_no},
            )
        job = BackgroundJob.objects.create(
            job_no=job_no or next_document_no("JOB"),
            job_type=job_type,
            trigger_type=trigger_type,
            input_params=input_params or {},
            status=BackgroundJob.JobStatus.RUNNING,
            started_at=timezone.now(),
        )
    return ServiceResult(True, message="后台任务已开始", data={"job_id": job.id, "job_no": job.job_no})


def _background_job_running_timeout_minutes() -> int:
    return max(1, int(getattr(settings, "ERP_BACKGROUND_JOB_RUNNING_TIMEOUT_MINUTES", 120)))


def _mark_stale_running_background_jobs(job_type: str) -> int:
    stale_before = timezone.now() - timedelta(minutes=_background_job_running_timeout_minutes())
    stale_jobs = list(
        BackgroundJob.objects.select_for_update()
        .filter(
            job_type=job_type,
            status=BackgroundJob.JobStatus.RUNNING,
            started_at__lte=stale_before,
        )
        .order_by("started_at")
    )
    for job in stale_jobs:
        job.status = BackgroundJob.JobStatus.FAILED
        job.finished_at = timezone.now()
        job.error_message = (
            f"后台任务运行超过 {_background_job_running_timeout_minutes()} 分钟，"
            "已在启动新任务前自动标记失败"
        )
        job.result_summary = {**job.result_summary, "auto_failed_by_timeout": True}
        job.save(update_fields=["status", "finished_at", "error_message", "result_summary"])
        transaction.on_commit(lambda job_id=job.id: _notify_background_job_failure(BackgroundJob.objects.get(id=job_id)))
    return len(stale_jobs)


def finish_background_job(job_id: int, result_summary: dict[str, Any] | None = None) -> BackgroundJob:
    job = BackgroundJob.objects.get(id=job_id)
    job.status = BackgroundJob.JobStatus.SUCCESS
    job.finished_at = timezone.now()
    job.result_summary = result_summary or {}
    job.error_message = ""
    job.save(update_fields=["status", "finished_at", "result_summary", "error_message"])
    return job


def fail_background_job(job_id: int, error_message: str, result_summary: dict[str, Any] | None = None) -> BackgroundJob:
    job = BackgroundJob.objects.get(id=job_id)
    job.status = BackgroundJob.JobStatus.FAILED
    job.finished_at = timezone.now()
    job.error_message = error_message
    job.result_summary = result_summary or {}
    job.save(update_fields=["status", "finished_at", "error_message", "result_summary"])
    _notify_background_job_failure(job)
    return job


def process_pending_events(event_type: str | None = None, limit: int = 100) -> ServiceResult:
    max_retries = _pending_event_max_retries()
    now = timezone.now()
    queryset = PendingEvent.objects.filter(_processable_event_filter(now, max_retries))
    if event_type:
        queryset = queryset.filter(event_type=event_type)

    processed = 0
    failed = 0
    retry_scheduled = 0
    max_retry_exceeded = 0
    event_ids = list(queryset.order_by("created_at").values_list("id", flat=True)[:limit])
    for event_id in event_ids:
        event = _claim_pending_event(event_id, max_retries=max_retries)
        if event is None:
            continue
        try:
            handler_result = _dispatch_pending_event(event)
            event.status = PendingEvent.EventStatus.SUCCESS
            event.last_error = ""
            event.next_retry_at = None
            event.payload = {**event.payload, "handler_result": handler_result}
            event.save(update_fields=["status", "last_error", "next_retry_at", "payload", "updated_at"])
            processed += 1
        except Exception as exc:
            event.retry_count = event.retry_count + 1
            event.last_error = str(exc)
            if event.retry_count >= max_retries:
                event.status = PendingEvent.EventStatus.FAILED
                event.next_retry_at = None
                max_retry_exceeded += 1
            else:
                event.status = PendingEvent.EventStatus.FAILED
                event.next_retry_at = _next_pending_event_retry_at(event.retry_count)
                retry_scheduled += 1
            event.save(update_fields=["status", "retry_count", "next_retry_at", "last_error", "updated_at"])
            failed += 1

    data = {
        "processed": processed,
        "failed": failed,
        "retry_scheduled": retry_scheduled,
        "max_retry_exceeded": max_retry_exceeded,
    }
    if failed:
        return ServiceResult(
            False,
            "SYSTEM_EVENT_PROCESS_FAILED",
            f"事务后事件处理完成，但 {failed} 个事件失败，其中 {retry_scheduled} 个已安排重试",
            data=data,
        )
    return ServiceResult(True, message="事务后事件处理完成", data=data)


def _pending_event_max_retries() -> int:
    return max(1, int(getattr(settings, "ERP_PENDING_EVENT_MAX_RETRIES", 3)))


def _pending_event_retry_base_minutes() -> int:
    return max(1, int(getattr(settings, "ERP_PENDING_EVENT_RETRY_BASE_MINUTES", 5)))


def _pending_event_running_timeout_minutes() -> int:
    return max(1, int(getattr(settings, "ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES", 30)))


def _processable_event_filter(now, max_retries: int) -> Q:
    due_failed = Q(
        status=PendingEvent.EventStatus.FAILED,
        retry_count__lt=max_retries,
    ) & (Q(next_retry_at__lte=now) | Q(next_retry_at__isnull=True))
    stale_running = Q(
        status=PendingEvent.EventStatus.RUNNING,
        updated_at__lte=now - timedelta(minutes=_pending_event_running_timeout_minutes()),
        retry_count__lt=max_retries,
    )
    return Q(status=PendingEvent.EventStatus.PENDING) | due_failed | stale_running


def _next_pending_event_retry_at(retry_count: int):
    retry_delay_minutes = _pending_event_retry_base_minutes() * (2 ** max(0, retry_count - 1))
    return timezone.now() + timedelta(minutes=retry_delay_minutes)


def _notify_background_job_failure(job: BackgroundJob) -> None:
    from accounts.permissions import PermissionCode
    from notifications.models import SystemMessage
    from notifications.services import create_system_message

    User = get_user_model()
    receivers = (
        User.objects.filter(
            Q(is_superuser=True)
            | Q(
                roles__status="active",
                roles__permissions__permission_code=PermissionCode.ADMIN_PERMISSION_MANAGE,
            ),
            is_active=True,
            is_deleted=False,
            status=User.AccountStatus.ACTIVE,
        )
        .distinct()
        .order_by("id")
    )
    title = f"后台任务失败：{code_label(job.job_type)}"
    content = f"任务单号：{job.job_no}\n失败原因：{job.error_message or '未提供错误信息'}"
    for receiver in receivers:
        if SystemMessage.objects.filter(
            receiver=receiver,
            source_doc_type="background_job",
            source_doc_id=job.id,
            title=title,
        ).exists():
            continue
        create_system_message(
            receiver_id=receiver.id,
            title=title,
            content=content,
            level=SystemMessage.Level.URGENT,
            source_doc_type="background_job",
            source_doc_id=job.id,
            source_doc_no=job.job_no,
            action_url="/background-jobs/?status=failed",
            suggested_action="查看后台任务失败详情",
        )


def _claim_pending_event(event_id: int, max_retries: int) -> PendingEvent | None:
    with transaction.atomic():
        now = timezone.now()
        event = PendingEvent.objects.select_for_update().filter(
            id=event_id,
        ).filter(_processable_event_filter(now, max_retries)).first()
        if event is None:
            return None
        event.status = PendingEvent.EventStatus.RUNNING
        event.next_retry_at = None
        event.save(update_fields=["status", "next_retry_at", "updated_at"])
        return event


def _dispatch_pending_event(event: PendingEvent) -> dict[str, Any]:
    handlers = {
        "purchase_received": _handle_purchase_received,
        "shortage_created": _handle_shortage_created,
        "shortage_kitted": _handle_shortage_kitted,
        "sales_order_confirmed": _handle_sales_order_confirmed,
        "sales_shipped": _handle_sales_shipped,
        "sample_out": _handle_sample_out,
        "sample_returned": _handle_sample_returned,
        "sample_to_sales": _handle_sample_to_sales,
        "customer_return_in": _handle_customer_return_in,
        "supplier_return_out": _handle_supplier_return_out,
        "location_transfer": _handle_location_transfer,
        "production_material_issued": _handle_production_material_issued,
        "production_received": _handle_production_received,
        "stock_count_adjusted": _handle_stock_count_adjusted,
        "payment_confirmed": _handle_payment_event,
        "payment_reversed": _handle_payment_event,
        "purchase_request_created": _handle_purchase_request_created,
        "purchase_order_created": _handle_purchase_order_created,
    }
    handler = handlers.get(event.event_type, _handle_generic_event)
    return handler(event)


def _handle_purchase_received(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from notifications.services import create_system_message
    from sales.services import recheck_sales_order_inventory

    sales_order_item_ids = event.payload.get("sales_order_item_ids") or []
    operator_id = event.payload.get("operator_id")
    if sales_order_item_ids:
        result = recheck_sales_order_inventory(
            sales_order_item_ids,
            trigger=f"pending_event:{event.id}",
            operator_id=operator_id,
        )
        if not result.success:
            raise RuntimeError(result.message or result.error_code or "采购入库后欠料重检失败")
    else:
        result = ServiceResult(True, message="没有需要重检的销售订单明细", data={"line_results": []})

    _notify_operator(
        event,
        title="采购入库后处理完成",
        content=result.message,
        level=SystemMessage.Level.INFO,
        suggested_action="查看欠料和采购入库结果",
    )
    return {"rechecked_sales_order_item_ids": sales_order_item_ids, "result": result.data}


def _handle_shortage_created(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SalesOrderItem

    item_id = event.payload.get("sales_order_item_id")
    item = SalesOrderItem.objects.select_related("sales_order").filter(id=item_id).first()
    if item and item.sales_order.created_by_id:
        _create_message(
            receiver_id=item.sales_order.created_by_id,
            title="销售订单存在欠料",
            content=f"{item.sales_order.sales_order_no} 第 {item.line_no} 行需要采购或等待齐套",
            level=SystemMessage.Level.URGENT,
            source_doc_type="sales_order",
            source_doc_id=item.sales_order_id,
            source_doc_no=item.sales_order.sales_order_no,
            suggested_action="查看欠料提醒",
        )
    return {"sales_order_item_id": item_id}


def _handle_shortage_kitted(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SalesOrderItem

    item_id = event.payload.get("sales_order_item_id")
    item = SalesOrderItem.objects.select_related("sales_order").filter(id=item_id).first()
    if item and item.sales_order.created_by_id:
        _create_message(
            receiver_id=item.sales_order.created_by_id,
            title="销售订单已齐套",
            content=f"{item.sales_order.sales_order_no} 第 {item.line_no} 行物料已齐套，可以安排生产",
            level=SystemMessage.Level.NORMAL,
            source_doc_type="sales_order",
            source_doc_id=item.sales_order_id,
            source_doc_no=item.sales_order.sales_order_no,
            suggested_action="创建生产指令单",
        )
    return {"sales_order_item_id": item_id}


def _handle_sales_order_confirmed(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SalesOrder

    order_id = event.payload.get("sales_order_id")
    order = SalesOrder.objects.filter(id=order_id).first()
    if order and order.created_by_id:
        _create_message(
            receiver_id=order.created_by_id,
            title="销售订单已确认",
            content=f"{order.sales_order_no} 已完成审核确认",
            level=SystemMessage.Level.INFO,
            source_doc_type="sales_order",
            source_doc_id=order.id,
            source_doc_no=order.sales_order_no,
        )
    return {"sales_order_id": order_id}


def _handle_sales_shipped(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SalesShipment

    shipment_id = event.payload.get("shipment_id")
    shipment = SalesShipment.objects.select_related("sales_order").filter(id=shipment_id).first()
    if shipment and shipment.sales_order.created_by_id:
        _create_message(
            receiver_id=shipment.sales_order.created_by_id,
            title="销售出库已完成",
            content=f"{shipment.shipment_no} 已确认出库",
            level=SystemMessage.Level.INFO,
            source_doc_type="sales_shipment",
            source_doc_id=shipment.id,
            source_doc_no=shipment.shipment_no,
        )
    return {"shipment_id": shipment_id}


def _handle_sample_out(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SampleLoan

    sample_loan_id = event.payload.get("sample_loan_id")
    sample_loan = SampleLoan.objects.filter(id=sample_loan_id).first()
    if sample_loan:
        _notify_operator(
            event,
            title="借样出库已完成",
            content=f"{sample_loan.sample_loan_no} 已确认出库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看借样单",
            source_doc_type="sample_loan",
            source_doc_id=sample_loan.id,
            source_doc_no=sample_loan.sample_loan_no,
        )
    return {"sample_loan_id": sample_loan_id}


def _handle_sample_returned(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SampleLoanReturn

    sample_return_id = event.payload.get("sample_return_id")
    sample_return = SampleLoanReturn.objects.filter(id=sample_return_id).first()
    if sample_return:
        _notify_operator(
            event,
            title="借样归还已完成",
            content=f"{sample_return.sample_return_no} 已确认归还入库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看借样归还单",
            source_doc_type="sample_loan_return",
            source_doc_id=sample_return.id,
            source_doc_no=sample_return.sample_return_no,
        )
    return {"sample_return_id": sample_return_id}


def _handle_sample_to_sales(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import SalesOrder, SampleLoan

    sample_loan_id = event.payload.get("sample_loan_id")
    sales_order_id = event.payload.get("sales_order_id")
    sample_loan = SampleLoan.objects.filter(id=sample_loan_id).first()
    sales_order = SalesOrder.objects.filter(id=sales_order_id).first()
    if sales_order:
        content = f"已生成销售订单 {sales_order.sales_order_no}"
        if sample_loan:
            content = f"{sample_loan.sample_loan_no} 已转销售，{content}"
        _notify_operator(
            event,
            title="借样转销售已完成",
            content=content,
            level=SystemMessage.Level.INFO,
            suggested_action="查看销售订单",
            source_doc_type="sales_order",
            source_doc_id=sales_order.id,
            source_doc_no=sales_order.sales_order_no,
        )
    return {"sample_loan_id": sample_loan_id, "sales_order_id": sales_order_id}


def _handle_customer_return_in(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from sales.models import CustomerReturn

    customer_return_id = event.payload.get("customer_return_id")
    customer_return = CustomerReturn.objects.filter(id=customer_return_id).first()
    if customer_return:
        _notify_operator(
            event,
            title="客户退货入库已完成",
            content=f"{customer_return.return_no} 已确认入库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看客户退货单",
            source_doc_type="customer_return",
            source_doc_id=customer_return.id,
            source_doc_no=customer_return.return_no,
        )
    return {"customer_return_id": customer_return_id}


def _handle_supplier_return_out(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from purchase.models import SupplierReturn

    supplier_return_id = event.payload.get("supplier_return_id")
    supplier_return = SupplierReturn.objects.filter(id=supplier_return_id).first()
    if supplier_return:
        _notify_operator(
            event,
            title="供应商退货出库已完成",
            content=f"{supplier_return.supplier_return_no} 已确认出库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看供应商退货单",
            source_doc_type="supplier_return",
            source_doc_id=supplier_return.id,
            source_doc_no=supplier_return.supplier_return_no,
        )
    return {"supplier_return_id": supplier_return_id}


def _handle_location_transfer(event: PendingEvent) -> dict[str, Any]:
    from inventory.models import LocationTransfer
    from notifications.models import SystemMessage

    transfer_id = event.payload.get("transfer_id")
    transfer = LocationTransfer.objects.filter(id=transfer_id).first()
    if transfer:
        _notify_operator(
            event,
            title="库位移库已完成",
            content=f"{transfer.transfer_no} 已确认移库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看库位移库单",
            source_doc_type="location_transfer",
            source_doc_id=transfer.id,
            source_doc_no=transfer.transfer_no,
        )
    return {"transfer_id": transfer_id}



def _handle_production_material_issued(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from production.models import ProductionMaterialRequisition

    requisition_id = event.payload.get("requisition_id")
    requisition = ProductionMaterialRequisition.objects.filter(id=requisition_id).first()
    if requisition:
        _notify_operator(
            event,
            title="生产领料已完成",
            content=f"{requisition.requisition_no} 已确认出库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看生产领料单",
            source_doc_type="production_material_requisition",
            source_doc_id=requisition.id,
            source_doc_no=requisition.requisition_no,
        )
    return {"requisition_id": requisition_id}


def _handle_production_received(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from production.models import ProductionReceipt

    production_receipt_id = event.payload.get("production_receipt_id")
    production_receipt = ProductionReceipt.objects.filter(id=production_receipt_id).first()
    if production_receipt:
        _notify_operator(
            event,
            title="生产入库已完成",
            content=f"{production_receipt.production_receipt_no} 已确认入库",
            level=SystemMessage.Level.INFO,
            suggested_action="查看生产入库单",
            source_doc_type="production_receipt",
            source_doc_id=production_receipt.id,
            source_doc_no=production_receipt.production_receipt_no,
        )
    return {"production_receipt_id": production_receipt_id}


def _handle_stock_count_adjusted(event: PendingEvent) -> dict[str, Any]:
    from inventory.models import StockCount
    from notifications.models import SystemMessage

    stock_count_id = event.payload.get("stock_count_id")
    stock_count = StockCount.objects.filter(id=stock_count_id).first()
    if stock_count:
        _notify_operator(
            event,
            title="盘点调整已完成",
            content=f"{stock_count.stock_count_no} 已完成库存调整",
            level=SystemMessage.Level.INFO,
            suggested_action="查看盘点单",
            source_doc_type="stock_count",
            source_doc_id=stock_count.id,
            source_doc_no=stock_count.stock_count_no,
        )
    return {"stock_count_id": stock_count_id}


def _handle_payment_event(event: PendingEvent) -> dict[str, Any]:
    from finance.models import CustomerReceipt, CustomerReceiptReversal, SupplierPayment, SupplierPaymentReversal
    from notifications.models import SystemMessage

    party = event.payload.get("party")
    if event.event_type == "payment_confirmed" and party == "customer":
        receipt_id = event.payload.get("receipt_id")
        receipt = CustomerReceipt.objects.filter(id=receipt_id).first()
        if receipt:
            _notify_operator(
                event,
                title="客户收款已确认",
                content=f"{receipt.receipt_no} 已确认并完成核销处理",
                level=SystemMessage.Level.INFO,
                suggested_action="查看客户收款单",
                source_doc_type="customer_receipt",
                source_doc_id=receipt.id,
                source_doc_no=receipt.receipt_no,
            )
        return {"party": party, "receipt_id": receipt_id}

    if event.event_type == "payment_confirmed" and party == "supplier":
        payment_id = event.payload.get("payment_id")
        payment = SupplierPayment.objects.filter(id=payment_id).first()
        if payment:
            _notify_operator(
                event,
                title="供应商付款已确认",
                content=f"{payment.payment_no} 已确认并完成核销处理",
                level=SystemMessage.Level.INFO,
                suggested_action="查看供应商付款单",
                source_doc_type="supplier_payment",
                source_doc_id=payment.id,
                source_doc_no=payment.payment_no,
            )
        return {"party": party, "payment_id": payment_id}

    if event.event_type == "payment_reversed" and party == "customer":
        receipt_id = event.payload.get("receipt_id")
        reversal_id = event.payload.get("reversal_id")
        reversal = CustomerReceiptReversal.objects.select_related("source_receipt").filter(id=reversal_id).first()
        if reversal:
            _notify_operator(
                event,
                title="客户收款红冲已完成",
                content=f"{reversal.source_receipt.receipt_no} 已红冲，红冲单 {reversal.reversal_no}",
                level=SystemMessage.Level.INFO,
                suggested_action="查看客户收款红冲单",
                source_doc_type="customer_receipt_reversal",
                source_doc_id=reversal.id,
                source_doc_no=reversal.reversal_no,
            )
        return {"party": party, "receipt_id": receipt_id, "reversal_id": reversal_id}

    if event.event_type == "payment_reversed" and party == "supplier":
        payment_id = event.payload.get("payment_id")
        reversal_id = event.payload.get("reversal_id")
        reversal = SupplierPaymentReversal.objects.select_related("source_payment").filter(id=reversal_id).first()
        if reversal:
            _notify_operator(
                event,
                title="供应商付款红冲已完成",
                content=f"{reversal.source_payment.payment_no} 已红冲，红冲单 {reversal.reversal_no}",
                level=SystemMessage.Level.INFO,
                suggested_action="查看供应商付款红冲单",
                source_doc_type="supplier_payment_reversal",
                source_doc_id=reversal.id,
                source_doc_no=reversal.reversal_no,
            )
        return {"party": party, "payment_id": payment_id, "reversal_id": reversal_id}

    return _notify_operator(event, "收付款事件已处理", "收付款确认或红冲事件已处理")


def _handle_purchase_request_created(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from purchase.models import PurchaseRequest

    purchase_request_id = event.payload.get("purchase_request_id")
    purchase_request = PurchaseRequest.objects.filter(id=purchase_request_id).first()
    receiver_id = event.payload.get("operator_id")
    if purchase_request and (receiver_id or purchase_request.requested_by_id):
        _create_message(
            receiver_id=receiver_id or purchase_request.requested_by_id,
            title="采购需求已生成",
            content=f"{purchase_request.purchase_request_no} 已由欠料提醒生成",
            level=SystemMessage.Level.INFO,
            source_doc_type="purchase_request",
            source_doc_id=purchase_request.id,
            source_doc_no=purchase_request.purchase_request_no,
            suggested_action="查看采购需求",
        )
    return {"purchase_request_id": purchase_request_id}


def _handle_purchase_order_created(event: PendingEvent) -> dict[str, Any]:
    from notifications.models import SystemMessage
    from purchase.models import PurchaseOrder, PurchaseRequest

    purchase_request_id = event.payload.get("purchase_request_id")
    purchase_order_id = event.payload.get("purchase_order_id")
    purchase_order = PurchaseOrder.objects.filter(id=purchase_order_id).first()
    purchase_request = PurchaseRequest.objects.filter(id=purchase_request_id).first()
    if purchase_order:
        content = f"{purchase_order.purchase_order_no} 已生成"
        if purchase_request:
            content = f"由采购需求 {purchase_request.purchase_request_no} 生成，{content}"
        _notify_operator(
            event,
            title="采购单已生成",
            content=content,
            level=SystemMessage.Level.INFO,
            suggested_action="查看采购单",
            source_doc_type="purchase_order",
            source_doc_id=purchase_order.id,
            source_doc_no=purchase_order.purchase_order_no,
        )
    return {"purchase_request_id": purchase_request_id, "purchase_order_id": purchase_order_id}


def _handle_generic_event(event: PendingEvent) -> dict[str, Any]:
    return _notify_operator(event, f"事件已处理：{code_label(event.event_type)}", str(event.payload))


def _notify_operator(
    event: PendingEvent,
    title: str,
    content: str,
    level: str | None = None,
    suggested_action: str = "",
    source_doc_type: str = "pending_event",
    source_doc_id: int | None = None,
    source_doc_no: str = "",
) -> dict[str, Any]:
    from notifications.models import SystemMessage

    receiver_id = event.payload.get("operator_id") or event.payload.get("receiver_id")
    if not receiver_id:
        return {"message_created": False}
    _create_message(
        receiver_id=receiver_id,
        title=title,
        content=content,
        level=level or SystemMessage.Level.INFO,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id if source_doc_id is not None else event.id,
        source_doc_no=source_doc_no or event.idempotency_key,
        suggested_action=suggested_action,
    )
    return {"message_created": True, "receiver_id": receiver_id}


def _create_message(
    receiver_id: int,
    title: str,
    content: str,
    level: str,
    source_doc_type: str,
    source_doc_id: int | None,
    source_doc_no: str,
    suggested_action: str = "",
) -> None:
    from notifications.services import create_system_message

    create_system_message(
        receiver_id=receiver_id,
        title=title,
        content=content,
        level=level,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        source_doc_no=source_doc_no,
        suggested_action=suggested_action,
    )
