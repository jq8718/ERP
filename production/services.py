from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.exceptions import InventoryError
from inventory.models import Inventory, InventoryBatch, InventoryTransaction
from inventory.services import deduct_batch_inventory
from sales.models import SalesOrder, SalesOrderItem
from system.models import PendingEvent
from system.services import ServiceResult, enqueue_pending_event, next_document_no

from .models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)


ZERO = Decimal("0")


def confirm_material_requisition(
    requisition_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认领料需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"production_issue:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该领料请求已处理")

            requisition = (
                ProductionMaterialRequisition.objects.select_for_update()
                .select_related("production_order")
                .get(id=requisition_id)
            )
            if requisition.status != ProductionMaterialRequisition.Status.PENDING_CONFIRM:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前领料单状态不能确认出库")

            production_order = ProductionOrder.objects.select_for_update().get(id=requisition.production_order_id)
            items = list(
                ProductionMaterialRequisitionItem.objects.select_for_update()
                .filter(requisition=requisition)
                .select_related("material", "batch", "location")
                .order_by("material_id", "location_id", "batch_id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "领料单没有明细")

            item_results = []
            for item in items:
                if item.issued_qty <= 0:
                    continue
                if item.issued_qty > item.required_qty:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "实际领料数量不能超过需求数量")
                deduct_result = deduct_batch_inventory(
                    batch_id=item.batch_id,
                    material_id=item.material_id,
                    location_id=item.location_id,
                    qty=item.issued_qty,
                    mismatch_message="领料明细与批次物料或库位不一致",
                )
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.PRODUCTION_ISSUE,
                    material=item.material,
                    batch=item.batch,
                    location=item.location,
                    qty_delta=-item.issued_qty,
                    source_doc_type="production_material_requisition",
                    source_doc_id=requisition.id,
                    source_doc_no=requisition.requisition_no,
                    created_by_id=operator_id,
                )
                item_results.append(
                    {
                        "item_id": item.id,
                        "material_id": item.material_id,
                        "issued_qty": str(item.issued_qty),
                        "batch_remaining_qty": str(deduct_result["batch_remaining_qty"]),
                        "inventory_qty": str(deduct_result["inventory_qty"]),
                    }
                )

            requisition.status = ProductionMaterialRequisition.Status.ISSUED
            requisition.save(update_fields=["status"])

            production_order.status = ProductionOrder.Status.IN_PROGRESS
            production_order.updated_by_id = operator_id
            production_order.version += 1
            production_order.save(update_fields=["status", "updated_by", "updated_at", "version"])
            _mark_sales_item_in_production(production_order)

            event = enqueue_pending_event(
                "production_material_issued",
                event_key,
                {"requisition_id": requisition.id, "operator_id": operator_id},
            )
            event.payload = {"requisition_id": requisition.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except ProductionMaterialRequisition.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "生产领料单不存在")
    except InventoryError as exc:
        return ServiceResult(False, exc.error_code, exc.message, exc.data)

    return ServiceResult(
        True,
        message="生产领料已确认",
        data={"requisition_id": requisition.id, "status": requisition.status, "items": item_results},
        next_action="view_detail",
    )


def confirm_production_receipt(
    production_receipt_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认生产入库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"production_received:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该生产入库请求已处理")

            receipt = (
                ProductionReceipt.objects.select_for_update()
                .select_related("production_order")
                .get(id=production_receipt_id)
            )
            if receipt.status != ProductionReceipt.Status.PENDING_CONFIRM:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前生产入库单状态不能确认入库")

            production_order = ProductionOrder.objects.select_for_update().get(id=receipt.production_order_id)
            items = list(
                ProductionReceiptItem.objects.select_for_update()
                .filter(production_receipt=receipt)
                .select_related("finished_material", "location")
                .order_by("finished_material_id", "location_id", "id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "生产入库单没有明细")

            receipt_qty = sum((item.receipt_qty for item in items if item.receipt_qty > 0), ZERO)
            if production_order.received_qty + receipt_qty > production_order.production_qty:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "入库数量不能超过生产指令数量")

            item_results = []
            for item in items:
                if item.receipt_qty <= 0:
                    continue
                batch = item.batch or _create_production_batch(item)
                item.batch = batch
                item.batch_no = batch.batch_no
                item.save(update_fields=["batch", "batch_no"])

                inventory = _increase_inventory(item.finished_material_id, item.location_id, item.receipt_qty)
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.PRODUCTION_RECEIPT,
                    material=item.finished_material,
                    batch=batch,
                    location=item.location,
                    qty_delta=item.receipt_qty,
                    source_doc_type="production_receipt",
                    source_doc_id=receipt.id,
                    source_doc_no=receipt.production_receipt_no,
                    created_by_id=operator_id,
                )
                item_results.append(
                    {
                        "item_id": item.id,
                        "finished_material_id": item.finished_material_id,
                        "receipt_qty": str(item.receipt_qty),
                        "batch_id": batch.id,
                        "inventory_qty": str(inventory.qty),
                    }
                )

            receipt.status = ProductionReceipt.Status.RECEIVED
            receipt.save(update_fields=["status"])

            production_order.received_qty += receipt_qty
            production_order.status = (
                ProductionOrder.Status.COMPLETED
                if production_order.received_qty >= production_order.production_qty
                else ProductionOrder.Status.IN_PROGRESS
            )
            production_order.updated_by_id = operator_id
            production_order.version += 1
            production_order.save(update_fields=["received_qty", "status", "updated_by", "updated_at", "version"])
            _refresh_sales_item_after_production_receipt(production_order, operator_id)

            event = enqueue_pending_event(
                "production_received",
                event_key,
                {"production_receipt_id": receipt.id, "operator_id": operator_id},
            )
            event.payload = {"production_receipt_id": receipt.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except ProductionReceipt.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "生产入库单不存在")

    return ServiceResult(
        True,
        message="生产入库已确认",
        data={
            "production_receipt_id": receipt.id,
            "status": receipt.status,
            "production_order_status": production_order.status,
            "items": item_results,
        },
        next_action="view_detail",
    )


def _increase_inventory(material_id: int, location_id: int, qty: Decimal) -> Inventory:
    inventory, _ = (
        Inventory.objects.select_for_update()
        .get_or_create(
            material_id=material_id,
            location_id=location_id,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            defaults={"qty": ZERO},
        )
    )
    inventory.qty += qty
    inventory.save(update_fields=["qty", "updated_at"])
    return inventory


def _create_production_batch(item: ProductionReceiptItem) -> InventoryBatch:
    batch_no = item.batch_no or next_document_no("BA")
    return InventoryBatch.objects.create(
        batch_no=batch_no,
        material=item.finished_material,
        location=item.location,
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        received_at=timezone.now(),
        initial_qty=item.receipt_qty,
        remaining_qty=item.receipt_qty,
        batch_status=InventoryBatch.BatchStatus.IN_STOCK,
    )


def _mark_sales_item_in_production(production_order: ProductionOrder) -> None:
    if not production_order.sales_order_item_id:
        return
    item = SalesOrderItem.objects.select_for_update().select_related("sales_order").get(id=production_order.sales_order_item_id)
    item.line_status = SalesOrderItem.LineStatus.IN_PRODUCTION
    item.save(update_fields=["line_status"])
    sales_order = item.sales_order
    sales_order.status = SalesOrder.Status.IN_PRODUCTION
    sales_order.save(update_fields=["status"])


def _refresh_sales_item_after_production_receipt(production_order: ProductionOrder, operator_id: int) -> None:
    if not production_order.sales_order_item_id:
        return
    item = SalesOrderItem.objects.select_for_update().select_related("sales_order", "finished_material").get(
        id=production_order.sales_order_item_id
    )
    available_qty = _available_finished_qty(item.finished_material_id)
    required_to_ship = max(ZERO, item.order_qty - item.shipped_qty)
    if available_qty >= required_to_ship:
        item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.SUFFICIENT
        item.line_status = SalesOrderItem.LineStatus.CONFIRMED
    else:
        item.line_status = SalesOrderItem.LineStatus.IN_PRODUCTION
    item.save(update_fields=["inventory_check_status", "line_status"])

    sales_order = item.sales_order
    if sales_order.items.filter(line_status=SalesOrderItem.LineStatus.IN_PRODUCTION).exists():
        sales_order.status = SalesOrder.Status.IN_PRODUCTION
    else:
        sales_order.status = SalesOrder.Status.CONFIRMED
    sales_order.updated_by_id = operator_id
    sales_order.version += 1
    sales_order.save(update_fields=["status", "updated_by", "updated_at", "version"])


def _available_finished_qty(material_id: int) -> Decimal:
    result = InventoryBatch.objects.filter(
        material_id=material_id,
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        remaining_qty__gt=0,
    ).aggregate(total=Sum("remaining_qty"))
    return result["total"] or ZERO
