from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from system.models import PendingEvent
from system.services import ServiceResult, enqueue_pending_event, next_document_no

from .exceptions import InventoryError
from .models import Inventory, InventoryBatch, InventoryTransaction, LocationTransfer, StockCount, StockCountItem


ZERO = Decimal("0")


def confirm_location_transfer(
    transfer_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认移库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"location_transfer:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该移库请求已处理")

            transfer = (
                LocationTransfer.objects.select_for_update()
                .select_related("material", "batch", "from_location", "to_location")
                .get(id=transfer_id)
            )
            if transfer.status != LocationTransfer.TransferStatus.DRAFT:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有草稿移库单可以确认")
            if transfer.from_location_id == transfer.to_location_id:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "目标库位不能与原库位相同")

            batch = InventoryBatch.objects.select_for_update().get(id=transfer.batch_id)
            if batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK:
                return ServiceResult(False, "STOCK_BATCH_LOCKED", "库存批次不可用")
            if batch.material_id != transfer.material_id or batch.location_id != transfer.from_location_id:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "移库单与批次物料或原库位不一致")
            if batch.remaining_qty < transfer.transfer_qty:
                return ServiceResult(False, "STOCK_NOT_ENOUGH", "批次库存不足")

            from_inventory = _inventory_for_update(transfer.material_id, transfer.from_location_id, batch.inventory_type)
            to_inventory = _inventory_for_update(transfer.material_id, transfer.to_location_id, batch.inventory_type)
            if from_inventory.qty < transfer.transfer_qty:
                return ServiceResult(False, "STOCK_NOT_ENOUGH", "原库位库存不足")

            batch.remaining_qty -= transfer.transfer_qty
            if batch.remaining_qty == ZERO:
                batch.batch_status = InventoryBatch.BatchStatus.USED_UP
                batch.save(update_fields=["remaining_qty", "batch_status"])
            else:
                batch.save(update_fields=["remaining_qty"])

            target_batch = InventoryBatch.objects.create(
                batch_no=next_document_no("LTB"),
                material=transfer.material,
                location=transfer.to_location,
                inventory_type=batch.inventory_type,
                received_at=timezone.now(),
                initial_qty=transfer.transfer_qty,
                remaining_qty=transfer.transfer_qty,
                cost_price=batch.cost_price,
                batch_status=InventoryBatch.BatchStatus.IN_STOCK,
            )

            from_inventory.qty -= transfer.transfer_qty
            to_inventory.qty += transfer.transfer_qty
            if from_inventory.qty < ZERO:
                raise InventoryError("STOCK_NEGATIVE_NOT_ALLOWED", "库存不能为负", {"material_id": transfer.material_id})
            from_inventory.save(update_fields=["qty", "updated_at"])
            to_inventory.save(update_fields=["qty", "updated_at"])

            InventoryTransaction.objects.create(
                transaction_no=next_document_no("IT"),
                transaction_type=InventoryTransaction.TransactionType.LOCATION_TRANSFER,
                material=transfer.material,
                batch=target_batch,
                location=transfer.to_location,
                qty_delta=transfer.transfer_qty,
                source_doc_type="location_transfer",
                source_doc_id=transfer.id,
                source_doc_no=transfer.transfer_no,
                created_by_id=operator_id,
            )

            transfer.status = LocationTransfer.TransferStatus.CONFIRMED
            transfer.save(update_fields=["status"])
            event = enqueue_pending_event(
                "location_transfer",
                event_key,
                {"transfer_id": transfer.id, "operator_id": operator_id},
            )
            event.payload = {"transfer_id": transfer.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except LocationTransfer.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "移库单不存在")
    except InventoryError as exc:
        return ServiceResult(False, exc.error_code, exc.message, exc.data)

    return ServiceResult(
        True,
        message="库位移库已确认",
        data={
            "transfer_id": transfer.id,
            "status": transfer.status,
            "source_batch_id": batch.id,
            "target_batch_id": target_batch.id,
            "from_inventory_qty": str(from_inventory.qty),
            "to_inventory_qty": str(to_inventory.qty),
        },
        next_action="view_detail",
    )


def create_stock_count_from_batches(
    operator_id: int,
    scope_type: str = "batch",
    scope_value: str = "",
    location_id: int | None = None,
) -> ServiceResult:
    snapshot_at = timezone.now()
    with transaction.atomic():
        stock_count = StockCount.objects.create(
            stock_count_no=next_document_no("SC"),
            scope_type=scope_type or "batch",
            scope_value=scope_value,
            snapshot_at=snapshot_at,
            status=StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT,
            created_by_id=operator_id,
        )

        batches = InventoryBatch.objects.select_for_update().filter(
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
            remaining_qty__gt=0,
        )
        if location_id:
            batches = batches.filter(location_id=location_id)
        if scope_value:
            batches = batches.filter(material__material_code__icontains=scope_value)
        batches = batches.select_related("material", "location").order_by("material_id", "location_id", "received_at", "batch_no")

        created_items = []
        for batch in batches:
            item = StockCountItem.objects.create(
                stock_count=stock_count,
                material=batch.material,
                batch=batch,
                location=batch.location,
                book_qty=batch.remaining_qty,
                counted_qty=batch.remaining_qty,
                difference_qty=ZERO,
            )
            created_items.append(item.id)

    return ServiceResult(
        True,
        message="盘点单已创建",
        data={"stock_count_id": stock_count.id, "item_count": len(created_items)},
        next_action="view_detail",
    )


def confirm_stock_count_adjustment(
    stock_count_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认盘点调整需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"stock_count_adjusted:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该盘点调整请求已处理")

            stock_count = StockCount.objects.select_for_update().get(id=stock_count_id)
            if stock_count.status != StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前盘点单状态不能调整库存")

            items = list(
                StockCountItem.objects.select_for_update()
                .filter(stock_count=stock_count)
                .select_related("material", "batch", "location")
                .order_by("material_id", "location_id", "batch_id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "盘点单没有明细")

            adjusted_items = []
            total_gain_qty = ZERO
            total_loss_qty = ZERO

            for item in items:
                if item.counted_qty is None:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "盘点明细必须填写实盘数量")

                difference_qty = item.counted_qty - item.book_qty
                item.difference_qty = difference_qty
                item.save(update_fields=["difference_qty"])
                if difference_qty == ZERO:
                    continue

                if difference_qty > ZERO:
                    batch, inventory_qty = _increase_for_stock_count(item, difference_qty)
                    total_gain_qty += difference_qty
                else:
                    batch, inventory_qty = _decrease_for_stock_count(item, -difference_qty)
                    total_loss_qty += -difference_qty

                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.STOCK_ADJUSTMENT,
                    material=item.material,
                    batch=batch,
                    location=item.location,
                    qty_delta=difference_qty,
                    source_doc_type="stock_count",
                    source_doc_id=stock_count.id,
                    source_doc_no=stock_count.stock_count_no,
                    created_by_id=operator_id,
                )
                adjusted_items.append(
                    {
                        "item_id": item.id,
                        "material_id": item.material_id,
                        "difference_qty": str(difference_qty),
                        "batch_id": batch.id,
                        "inventory_qty": str(inventory_qty),
                    }
                )

            stock_count.status = StockCount.CountStatus.ADJUSTED
            stock_count.save(update_fields=["status"])
            event = enqueue_pending_event(
                "stock_count_adjusted",
                event_key,
                {"stock_count_id": stock_count.id, "operator_id": operator_id},
            )
            event.payload = {"stock_count_id": stock_count.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except StockCount.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "盘点单不存在")
    except InventoryError as exc:
        return ServiceResult(False, exc.error_code, exc.message, exc.data)

    return ServiceResult(
        True,
        message="盘点调整已确认",
        data={
            "stock_count_id": stock_count.id,
            "status": stock_count.status,
            "adjusted_items": len(adjusted_items),
            "total_gain_qty": str(total_gain_qty),
            "total_loss_qty": str(total_loss_qty),
            "items": adjusted_items,
        },
        next_action="view_detail",
    )


def _increase_for_stock_count(item: StockCountItem, qty: Decimal) -> tuple[InventoryBatch, Decimal]:
    if item.batch_id:
        batch = InventoryBatch.objects.select_for_update().get(id=item.batch_id)
        if batch.batch_status not in [InventoryBatch.BatchStatus.IN_STOCK, InventoryBatch.BatchStatus.USED_UP]:
            raise InventoryError("STOCK_BATCH_LOCKED", "盘点批次不可调整", {"batch_id": batch.id})
        batch.remaining_qty += qty
        if batch.batch_status == InventoryBatch.BatchStatus.USED_UP and batch.remaining_qty > ZERO:
            batch.batch_status = InventoryBatch.BatchStatus.IN_STOCK
        batch.save(update_fields=["remaining_qty", "batch_status"])
    else:
        batch = InventoryBatch.objects.create(
            batch_no=next_document_no("SCB"),
            material=item.material,
            location=item.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=qty,
            remaining_qty=qty,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )

    inventory = _inventory_for_update(item.material_id, item.location_id, batch.inventory_type)
    inventory.qty += qty
    inventory.save(update_fields=["qty", "updated_at"])
    return batch, inventory.qty


def _decrease_for_stock_count(item: StockCountItem, qty: Decimal) -> tuple[InventoryBatch, Decimal]:
    if not item.batch_id:
        raise InventoryError("STATE_INVALID_TRANSITION", "盘亏调整必须指定库存批次", {"item_id": item.id})

    batch = InventoryBatch.objects.select_for_update().get(id=item.batch_id)
    if batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK:
        raise InventoryError("STOCK_BATCH_LOCKED", "库存批次不可用", {"batch_id": batch.id})
    if batch.remaining_qty < qty:
        raise InventoryError(
            "STOCK_NOT_ENOUGH",
            "批次库存不足，不能盘亏调整",
            {"batch_id": batch.id, "required_qty": str(qty), "available_qty": str(batch.remaining_qty)},
        )

    inventory = _inventory_for_update(item.material_id, item.location_id, batch.inventory_type)
    if inventory.qty < qty:
        raise InventoryError(
            "STOCK_NOT_ENOUGH",
            "当前库存不足，不能盘亏调整",
            {"material_id": item.material_id, "required_qty": str(qty), "available_qty": str(inventory.qty)},
        )

    batch.remaining_qty -= qty
    if batch.remaining_qty == ZERO:
        batch.batch_status = InventoryBatch.BatchStatus.USED_UP
        batch.save(update_fields=["remaining_qty", "batch_status"])
    else:
        batch.save(update_fields=["remaining_qty"])

    inventory.qty -= qty
    if inventory.qty < ZERO:
        raise InventoryError("STOCK_NEGATIVE_NOT_ALLOWED", "库存不能为负", {"material_id": item.material_id})
    inventory.save(update_fields=["qty", "updated_at"])
    return batch, inventory.qty


def deduct_batch_inventory(
    batch_id: int,
    material_id: int,
    location_id: int,
    qty: Decimal,
    mismatch_message: str = "出库明细与批次物料或库位不一致",
) -> dict:
    if not transaction.get_connection().in_atomic_block:
        with transaction.atomic():
            return _deduct_batch_inventory_locked(batch_id, material_id, location_id, qty, mismatch_message)
    return _deduct_batch_inventory_locked(batch_id, material_id, location_id, qty, mismatch_message)


def _deduct_batch_inventory_locked(
    batch_id: int,
    material_id: int,
    location_id: int,
    qty: Decimal,
    mismatch_message: str,
) -> dict:
    batch = InventoryBatch.objects.select_for_update().get(id=batch_id)
    if batch.batch_status != InventoryBatch.BatchStatus.IN_STOCK:
        raise InventoryError("STOCK_BATCH_LOCKED", "库存批次不可用", {"batch_id": batch_id})
    if batch.material_id != material_id or batch.location_id != location_id:
        raise InventoryError("STATE_INVALID_TRANSITION", mismatch_message, {"batch_id": batch_id})
    if batch.remaining_qty < qty:
        raise InventoryError(
            "STOCK_NOT_ENOUGH",
            "批次库存不足",
            {"batch_id": batch_id, "required_qty": str(qty), "available_qty": str(batch.remaining_qty)},
        )

    inventory = Inventory.objects.select_for_update().get(
        material_id=material_id,
        location_id=location_id,
        inventory_type=batch.inventory_type,
    )
    if inventory.qty < qty:
        raise InventoryError(
            "STOCK_NOT_ENOUGH",
            "当前库存不足",
            {"material_id": material_id, "required_qty": str(qty), "available_qty": str(inventory.qty)},
        )

    batch.remaining_qty -= qty
    if batch.remaining_qty == ZERO:
        batch.batch_status = InventoryBatch.BatchStatus.USED_UP
        batch.save(update_fields=["remaining_qty", "batch_status"])
    else:
        batch.save(update_fields=["remaining_qty"])

    inventory.qty -= qty
    if inventory.qty < ZERO:
        raise InventoryError("STOCK_NEGATIVE_NOT_ALLOWED", "库存不能为负", {"material_id": material_id})
    inventory.save(update_fields=["qty", "updated_at"])
    return {"batch_remaining_qty": batch.remaining_qty, "inventory_qty": inventory.qty}


def _inventory_for_update(material_id: int, location_id: int, inventory_type: str) -> Inventory:
    inventory, _ = (
        Inventory.objects.select_for_update()
        .get_or_create(
            material_id=material_id,
            location_id=location_id,
            inventory_type=inventory_type,
            defaults={"qty": ZERO},
        )
    )
    return inventory
