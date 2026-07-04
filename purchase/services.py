from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from inventory.exceptions import InventoryError
from inventory.models import Inventory, InventoryBatch, InventoryTransaction
from inventory.services import deduct_batch_inventory
from sales.models import ShortageAlert
from system.models import PendingEvent
from system.services import ServiceResult, enqueue_pending_event, next_document_no

from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseRequest,
    PurchaseRequestItem,
    SupplierReturn,
    SupplierReturnItem,
)


def confirm_purchase_receipt(
    purchase_receipt_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认入库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"purchase_received:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该入库请求已处理")

            receipt = (
                PurchaseReceipt.objects.select_for_update()
                .select_related("purchase_order", "supplier")
                .get(id=purchase_receipt_id)
            )
            if receipt.status not in [PurchaseReceipt.Status.PENDING_RECEIVE, PurchaseReceipt.Status.PARTIAL_RECEIVED]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前进货单状态不能确认入库")

            items = list(
                PurchaseReceiptItem.objects.select_for_update()
                .filter(purchase_receipt=receipt)
                .select_related("material", "location", "purchase_order_item")
                .order_by("material_id", "location_id", "id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "进货单没有明细")

            touched_sales_order_item_ids: set[int] = set()
            item_results = []
            for item in items:
                if item.accepted_qty <= 0:
                    continue
                batch = item.batch or _create_purchase_batch(item)
                item.batch = batch
                item.save(update_fields=["batch"])

                inventory = _increase_inventory(item.material_id, item.location_id, item.accepted_qty)
                transaction_no = next_document_no("IT")
                InventoryTransaction.objects.create(
                    transaction_no=transaction_no,
                    transaction_type=InventoryTransaction.TransactionType.PURCHASE_IN,
                    material=item.material,
                    batch=batch,
                    location=item.location,
                    qty_delta=item.accepted_qty,
                    source_doc_type="purchase_receipt",
                    source_doc_id=receipt.id,
                    source_doc_no=receipt.purchase_receipt_no,
                    created_by_id=operator_id,
                )

                order_item = PurchaseOrderItem.objects.select_for_update().get(id=item.purchase_order_item_id)
                order_item.received_qty += item.accepted_qty
                order_item.line_status = _purchase_order_item_status(order_item)
                order_item.save(update_fields=["received_qty", "line_status"])

                touched_sales_order_item_ids.update(_source_sales_order_item_ids_for_material(item.material_id))
                item_results.append(
                    {
                        "item_id": item.id,
                        "material_id": item.material_id,
                        "accepted_qty": str(item.accepted_qty),
                        "batch_id": batch.id,
                        "inventory_qty": str(inventory.qty),
                    }
                )

            receipt.status = _purchase_receipt_status(receipt)
            receipt.save(update_fields=["status"])
            _refresh_purchase_order_status(receipt.purchase_order)

            existing_event = enqueue_pending_event(
                "purchase_received",
                event_key,
                {"purchase_receipt_id": receipt.id, "operator_id": operator_id},
            )
            existing_event.payload = {
                "purchase_receipt_id": receipt.id,
                "operator_id": operator_id,
                "confirmed": True,
                "sales_order_item_ids": sorted(touched_sales_order_item_ids),
            }
            existing_event.save(update_fields=["payload", "updated_at"])

    except PurchaseReceipt.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "进货单不存在")

    return ServiceResult(
        True,
        message="进货入库已确认",
        data={
            "purchase_receipt_id": purchase_receipt_id,
            "status": receipt.status,
            "items": item_results,
            "recheck_sales_order_item_ids": sorted(touched_sales_order_item_ids),
        },
        next_action="view_detail",
    )


def create_purchase_request_from_shortages(
    shortage_alert_ids: list[int],
    operator_id: int,
    merge_mode: str = "by_material",
    idempotency_key: str = "",
) -> ServiceResult:
    if not shortage_alert_ids:
        return ServiceResult(False, "DOC_NOT_FOUND", "没有选择欠料提醒")

    with transaction.atomic():
        shortages = list(
            ShortageAlert.objects.select_for_update()
            .filter(
                id__in=shortage_alert_ids,
                status=ShortageAlert.Status.UNPROCESSED,
                shortage_qty__gt=0,
            )
            .select_related("material", "sales_order_item")
            .order_by("material_id", "id")
        )
        if not shortages:
            return ServiceResult(False, "STATE_INVALID_TRANSITION", "没有可生成采购需求的欠料提醒")

        request = PurchaseRequest.objects.create(
            purchase_request_no=next_document_no("PR"),
            source_type=PurchaseRequest.SourceType.SHORTAGE,
            status=PurchaseRequest.Status.DRAFT,
            requested_by_id=operator_id,
            remark=f"由 {len(shortages)} 条欠料提醒生成",
        )

        groups = _group_shortages(shortages, merge_mode)
        created_items = []
        for line_no, (_, group_rows) in enumerate(groups.items(), start=1):
            request_qty = sum((row.shortage_qty for row in group_rows), Decimal("0"))
            first = group_rows[0]
            request_item = PurchaseRequestItem.objects.create(
                purchase_request=request,
                line_no=line_no,
                material=first.material,
                request_qty=request_qty,
                needed_date=first.sales_order.delivery_date,
                source_shortage_alert=first if len(group_rows) == 1 else None,
                source_sales_order_item=first.sales_order_item if len(group_rows) == 1 else None,
            )
            created_items.append({"item_id": request_item.id, "material_id": first.material_id, "request_qty": str(request_qty)})

        for shortage in shortages:
            shortage.status = ShortageAlert.Status.PURCHASE_REQUESTED
            shortage.purchase_request = request
            shortage.save(update_fields=["status", "purchase_request"])

        if idempotency_key:
            enqueue_pending_event(
                "purchase_request_created",
                f"purchase_request_created:{idempotency_key}",
                {"purchase_request_id": request.id, "shortage_alert_ids": shortage_alert_ids},
            )

    return ServiceResult(
        True,
        message="采购需求已生成",
        data={
            "purchase_request_id": request.id,
            "purchase_request_no": request.purchase_request_no,
            "items": created_items,
        },
        next_action="view_purchase_request",
    )


def confirm_supplier_return_shipment(
    supplier_return_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认供应商退货出库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"supplier_return_out:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该供应商退货出库请求已处理")

            supplier_return = (
                SupplierReturn.objects.select_for_update()
                .select_related("supplier")
                .get(id=supplier_return_id)
            )
            if supplier_return.status != SupplierReturn.Status.CONFIRMED:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有已确认供应商退货单可以出库")

            items = list(
                SupplierReturnItem.objects.select_for_update()
                .filter(supplier_return=supplier_return)
                .select_related("material")
                .order_by("material_id", "location_id", "batch_id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "供应商退货单没有明细")

            item_results = []
            for item in items:
                if item.return_qty <= 0:
                    continue
                if not item.batch_id or not item.location_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "供应商退货明细必须选择批次和库位")
                deduct_result = deduct_batch_inventory(
                    batch_id=item.batch_id,
                    material_id=item.material_id,
                    location_id=item.location_id,
                    qty=item.return_qty,
                    mismatch_message="供应商退货明细与批次物料或库位不一致",
                )
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.SUPPLIER_RETURN_OUT,
                    material=item.material,
                    batch=item.batch,
                    location=item.location,
                    qty_delta=-item.return_qty,
                    source_doc_type="supplier_return",
                    source_doc_id=supplier_return.id,
                    source_doc_no=supplier_return.supplier_return_no,
                    created_by_id=operator_id,
                )
                item_results.append(
                    {
                        "supplier_return_item_id": item.id,
                        "material_id": item.material_id,
                        "return_qty": str(item.return_qty),
                        "batch_remaining_qty": str(deduct_result["batch_remaining_qty"]),
                        "inventory_qty": str(deduct_result["inventory_qty"]),
                    }
                )

            supplier_return.status = SupplierReturn.Status.SHIPPED
            supplier_return.save(update_fields=["status"])
            event = enqueue_pending_event(
                "supplier_return_out",
                event_key,
                {"supplier_return_id": supplier_return.id, "operator_id": operator_id},
            )
            event.payload = {"supplier_return_id": supplier_return.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except SupplierReturn.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "供应商退货单不存在")
    except InventoryError as exc:
        return ServiceResult(False, exc.error_code, exc.message, exc.data)

    return ServiceResult(
        True,
        message="供应商退货出库已确认",
        data={"supplier_return_id": supplier_return.id, "status": supplier_return.status, "items": item_results},
        next_action="view_detail",
    )


def create_purchase_order_from_request(
    purchase_request_id: int,
    supplier_id: int,
    operator_id: int,
    idempotency_key: str = "",
) -> ServiceResult:
    from masterdata.models import Supplier

    if not Supplier.objects.filter(id=supplier_id, status=Supplier.SupplierStatus.ACTIVE).exists():
        return ServiceResult(False, "DOC_NOT_FOUND", "供应商不存在或未启用")

    try:
        with transaction.atomic():
            event_key = f"purchase_order_created:{idempotency_key}" if idempotency_key else ""
            if event_key and PendingEvent.objects.filter(idempotency_key=event_key).exists():
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该采购单生成请求已处理")

            purchase_request = PurchaseRequest.objects.select_for_update().get(id=purchase_request_id)
            if purchase_request.status in [PurchaseRequest.Status.CLOSED, PurchaseRequest.Status.VOIDED]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "已关闭或已作废采购需求不能生成采购单")

            request_items = list(
                PurchaseRequestItem.objects.select_for_update()
                .filter(
                    purchase_request=purchase_request,
                    line_status__in=[
                        PurchaseRequestItem.LineStatus.OPEN,
                        PurchaseRequestItem.LineStatus.PARTIAL_ORDERED,
                    ],
                )
                .select_related("material")
                .order_by("line_no", "id")
            )
            if not request_items:
                return ServiceResult(False, "DOC_NOT_FOUND", "采购需求没有可下单明细")

            order = PurchaseOrder.objects.create(
                purchase_order_no=next_document_no("PO"),
                supplier_id=supplier_id,
                status=PurchaseOrder.Status.APPROVED,
                order_date=timezone.localdate(),
                created_by_id=operator_id,
                remark=f"由采购需求 {purchase_request.purchase_request_no} 生成",
            )

            created_items = []
            for line_no, request_item in enumerate(request_items, start=1):
                unit_price = _default_purchase_price(request_item.material_id, supplier_id)
                line_amount = _money(request_item.request_qty * unit_price)
                order_item = PurchaseOrderItem.objects.create(
                    purchase_order=order,
                    purchase_request_item=request_item,
                    line_no=line_no,
                    material=request_item.material,
                    order_qty=request_item.request_qty,
                    unit_price=unit_price,
                    line_amount=line_amount,
                    needed_date=request_item.needed_date or purchase_request.needed_date,
                )
                request_item.line_status = PurchaseRequestItem.LineStatus.ORDERED
                request_item.save(update_fields=["line_status"])
                created_items.append({"item_id": order_item.id, "material_id": request_item.material_id})

            _refresh_purchase_request_status(purchase_request)
            _refresh_purchase_order_total(order)

            if event_key:
                enqueue_pending_event(
                    "purchase_order_created",
                    event_key,
                    {
                        "purchase_request_id": purchase_request.id,
                        "purchase_order_id": order.id,
                        "operator_id": operator_id,
                    },
                )
    except PurchaseRequest.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "采购需求不存在")

    return ServiceResult(
        True,
        message="采购单已生成",
        data={"purchase_order_id": order.id, "purchase_order_no": order.purchase_order_no, "items": created_items},
        next_action="view_purchase_order",
    )


def _create_purchase_batch(item: PurchaseReceiptItem) -> InventoryBatch:
    batch = InventoryBatch.objects.create(
        batch_no=next_document_no("BA"),
        material=item.material,
        location=item.location,
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        received_at=timezone.now(),
        initial_qty=item.accepted_qty,
        remaining_qty=item.accepted_qty,
        cost_price=item.unit_price,
        batch_status=InventoryBatch.BatchStatus.IN_STOCK,
    )
    return batch


def _increase_inventory(material_id: int, location_id: int, qty: Decimal) -> Inventory:
    inventory, _ = (
        Inventory.objects.select_for_update()
        .get_or_create(
            material_id=material_id,
            location_id=location_id,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            defaults={"qty": Decimal("0")},
        )
    )
    inventory.qty += qty
    if inventory.qty < 0:
        raise ValueError("inventory qty cannot be negative")
    inventory.save(update_fields=["qty", "updated_at"])
    return inventory


def _purchase_order_item_status(order_item: PurchaseOrderItem) -> str:
    if order_item.received_qty <= 0:
        return PurchaseOrderItem.LineStatus.OPEN
    if order_item.received_qty < order_item.order_qty:
        return PurchaseOrderItem.LineStatus.PARTIAL_RECEIVED
    return PurchaseOrderItem.LineStatus.RECEIVED


def _purchase_receipt_status(receipt: PurchaseReceipt) -> str:
    total_accepted = sum((item.accepted_qty for item in receipt.items.all()), Decimal("0"))
    if total_accepted <= 0:
        return PurchaseReceipt.Status.PENDING_RECEIVE
    return PurchaseReceipt.Status.RECEIVED


def _refresh_purchase_order_status(purchase_order: PurchaseOrder) -> None:
    items = list(PurchaseOrderItem.objects.select_for_update().filter(purchase_order=purchase_order))
    if not items:
        return
    if all(item.line_status == PurchaseOrderItem.LineStatus.RECEIVED for item in items):
        purchase_order.status = PurchaseOrder.Status.RECEIVED
    elif any(item.received_qty > 0 for item in items):
        purchase_order.status = PurchaseOrder.Status.PARTIAL_RECEIVED
    purchase_order.save(update_fields=["status"])


def _source_sales_order_item_ids_for_material(material_id: int) -> set[int]:
    return set(
        ShortageAlert.objects.filter(
            material_id=material_id,
            status__in=[
                ShortageAlert.Status.UNPROCESSED,
                ShortageAlert.Status.PURCHASE_REQUESTED,
                ShortageAlert.Status.PARTIAL_RECEIVED,
            ],
        ).values_list("sales_order_item_id", flat=True)
    )


def _group_shortages(shortages: list[ShortageAlert], merge_mode: str) -> dict[tuple, list[ShortageAlert]]:
    groups: dict[tuple, list[ShortageAlert]] = defaultdict(list)
    for shortage in shortages:
        if merge_mode == "separate_by_order":
            key = (shortage.material_id, shortage.sales_order_id)
        else:
            key = (shortage.material_id,)
        groups[key].append(shortage)
    return groups


def _default_purchase_price(material_id: int, supplier_id: int) -> Decimal:
    from masterdata.models import Material, MaterialSupplierPrice

    price = (
        MaterialSupplierPrice.objects.filter(
            material_id=material_id,
            supplier_id=supplier_id,
            status=MaterialSupplierPrice.PriceStatus.ACTIVE,
        )
        .order_by("-is_default", "-effective_from", "-id")
        .first()
    )
    if price:
        return price.purchase_price
    material = Material.objects.get(id=material_id)
    return material.latest_purchase_price or Decimal("0")


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def _refresh_purchase_order_total(order: PurchaseOrder) -> None:
    order.total_amount = _money(sum((item.line_amount for item in order.items.all()), Decimal("0")))
    order.save(update_fields=["total_amount"])


def _refresh_purchase_request_status(purchase_request: PurchaseRequest) -> None:
    items = list(PurchaseRequestItem.objects.filter(purchase_request=purchase_request))
    if not items:
        return
    if all(item.line_status == PurchaseRequestItem.LineStatus.ORDERED for item in items):
        purchase_request.status = PurchaseRequest.Status.CLOSED
    elif any(
        item.line_status in [PurchaseRequestItem.LineStatus.ORDERED, PurchaseRequestItem.LineStatus.PARTIAL_ORDERED]
        for item in items
    ):
        purchase_request.status = PurchaseRequest.Status.APPROVED
    purchase_request.save(update_fields=["status"])
