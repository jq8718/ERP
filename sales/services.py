from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.db.models import Sum

from bom.models import Bom, BomItem
from bom.services import UnitConversionMissing, required_component_qty_base
from inventory.exceptions import InventoryError
from inventory.models import Inventory, InventoryBatch, InventoryTransaction
from inventory.services import deduct_batch_inventory
from masterdata.models import CustomerProduct, Material
from system.models import PendingEvent
from system.services import ServiceResult, enqueue_pending_event, next_document_no

from .models import (
    CustomerReturn,
    CustomerReturnItem,
    SalesOrder,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
    SampleLoan,
    SampleLoanItem,
    SampleLoanReturn,
    SampleLoanReturnItem,
    ShortageAlert,
)


ZERO = Decimal("0")


def confirm_sales_order(
    sales_order_id: int,
    operator_id: int,
    approval_id: int | None = None,
    comment: str = "",
) -> ServiceResult:
    try:
        with transaction.atomic():
            sales_order = _get_locked_sales_order(sales_order_id)
            if sales_order is None:
                return ServiceResult(False, "DOC_NOT_FOUND", "销售订单不存在")
            if sales_order.status != SalesOrder.Status.PENDING_APPROVAL:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前销售订单状态不能审核确认")

            items = list(
                SalesOrderItem.objects.select_for_update()
                .filter(sales_order=sales_order)
                .select_related("finished_material")
                .order_by("line_no")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "销售订单没有明细行")

            line_results = []
            has_pending_bom = False
            has_shortage = False

            for item in items:
                result = _lock_bom_and_evaluate_item(item)
                line_results.append(result)
                if result["inventory_check_status"] == SalesOrderItem.InventoryCheckStatus.PENDING_BOM:
                    has_pending_bom = True
                if result["inventory_check_status"] == SalesOrderItem.InventoryCheckStatus.SHORTAGE:
                    has_shortage = True

            sales_order.status = _summarize_sales_order_status(has_pending_bom, has_shortage)
            sales_order.updated_by_id = operator_id
            sales_order.approved_by_id = operator_id
            sales_order.approved_at = timezone.now()
            sales_order.version += 1
            sales_order.save(update_fields=["status", "updated_by", "updated_at", "approved_by", "approved_at", "version"])

            _enqueue_sales_events(sales_order, line_results)
    except UnitConversionMissing as exc:
        return ServiceResult(False, "BOM_UNIT_CONVERSION_MISSING", f"BOM 单位换算缺失：{exc}")
    except ValueError as exc:
        return ServiceResult(False, "BOM_CALC_INVALID", str(exc))

    return ServiceResult(
        True,
        message="销售订单审核通过",
        data={
            "sales_order_id": sales_order.id,
            "sales_order_no": sales_order.sales_order_no,
            "status": sales_order.status,
            "line_results": line_results,
        },
        next_action="view_detail",
    )


def recheck_sales_order_inventory(
    sales_order_item_ids: list[int],
    trigger: str,
    operator_id: int | None = None,
) -> ServiceResult:
    try:
        with transaction.atomic():
            items = list(
                SalesOrderItem.objects.select_for_update()
                .filter(id__in=sales_order_item_ids)
                # locked_bom is nullable. Joining it in a SELECT ... FOR UPDATE query
                # breaks on PostgreSQL when pending-BOM rows have no locked BOM yet.
                .select_related("sales_order", "finished_material")
                .order_by("id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "没有找到需要重检的销售订单明细")

            line_results = []
            affected_orders: dict[int, SalesOrder] = {}
            for item in items:
                result = _evaluate_item_with_locked_or_enabled_bom(item)
                line_results.append(result)
                affected_orders[item.sales_order_id] = item.sales_order

            for sales_order in SalesOrder.objects.select_for_update().filter(id__in=affected_orders.keys()):
                _refresh_sales_order_header_status(sales_order, operator_id)

            for result in line_results:
                if result["inventory_check_status"] == SalesOrderItem.InventoryCheckStatus.KITTED:
                    enqueue_pending_event(
                        "shortage_kitted",
                        f"shortage_kitted:{result['item_id']}:{trigger}",
                        {"sales_order_item_id": result["item_id"], "trigger": trigger},
                    )
    except UnitConversionMissing as exc:
        return ServiceResult(False, "BOM_UNIT_CONVERSION_MISSING", f"BOM 单位换算缺失：{exc}")
    except ValueError as exc:
        return ServiceResult(False, "BOM_CALC_INVALID", str(exc))

    return ServiceResult(
        True,
        message="库存齐套状态已重新检查",
        data={"line_results": line_results},
        next_action="refresh",
    )


def confirm_sales_shipment(
    shipment_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认销售出库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"sales_shipped:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该销售出库请求已处理")

            shipment = (
                SalesShipment.objects.select_for_update()
                .select_related("sales_order", "customer")
                .get(id=shipment_id)
            )
            if shipment.status != SalesShipment.Status.PENDING_CONFIRM:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前销售出库单状态不能确认出库")

            shipment_items = list(
                SalesShipmentItem.objects.select_for_update()
                .filter(shipment=shipment)
                .select_related("sales_order_item", "material", "batch", "location")
                .order_by("material_id", "location_id", "batch_id")
            )
            if not shipment_items:
                return ServiceResult(False, "DOC_NOT_FOUND", "销售出库单没有明细")

            item_results = []
            touched_sales_item_ids = set()
            for shipment_item in shipment_items:
                if shipment_item.shipment_qty <= 0:
                    continue
                sales_item = SalesOrderItem.objects.select_for_update().get(id=shipment_item.sales_order_item_id)
                unshipped_qty = sales_item.order_qty - sales_item.shipped_qty
                if shipment_item.shipment_qty > unshipped_qty:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "本次发货数量不能超过未发货数量")
                if shipment_item.material_id != sales_item.finished_material_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "出库物料必须与销售订单明细成品一致")

                deduct_result = deduct_batch_inventory(
                    batch_id=shipment_item.batch_id,
                    material_id=shipment_item.material_id,
                    location_id=shipment_item.location_id,
                    qty=shipment_item.shipment_qty,
                    mismatch_message="出库明细与批次物料或库位不一致",
                )
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.SALES_OUT,
                    material=shipment_item.material,
                    batch=shipment_item.batch,
                    location=shipment_item.location,
                    qty_delta=-shipment_item.shipment_qty,
                    source_doc_type="sales_shipment",
                    source_doc_id=shipment.id,
                    source_doc_no=shipment.shipment_no,
                    created_by_id=operator_id,
                )

                sales_item.shipped_qty += shipment_item.shipment_qty
                sales_item.line_status = (
                    SalesOrderItem.LineStatus.SHIPPED
                    if sales_item.shipped_qty >= sales_item.order_qty
                    else SalesOrderItem.LineStatus.CONFIRMED
                )
                sales_item.save(update_fields=["shipped_qty", "line_status"])
                touched_sales_item_ids.add(sales_item.id)
                item_results.append(
                    {
                        "shipment_item_id": shipment_item.id,
                        "sales_order_item_id": sales_item.id,
                        "shipment_qty": str(shipment_item.shipment_qty),
                        "shipped_qty": str(sales_item.shipped_qty),
                        "batch_remaining_qty": str(deduct_result["batch_remaining_qty"]),
                        "inventory_qty": str(deduct_result["inventory_qty"]),
                    }
                )

            shipment.status = SalesShipment.Status.SHIPPED
            shipment.save(update_fields=["status"])
            _refresh_sales_order_after_shipment(shipment.sales_order, operator_id)

            event = enqueue_pending_event(
                "sales_shipped",
                event_key,
                {"shipment_id": shipment.id, "operator_id": operator_id},
            )
            event.payload = {
                "shipment_id": shipment.id,
                "operator_id": operator_id,
                "confirmed": True,
                "sales_order_item_ids": sorted(touched_sales_item_ids),
            }
            event.save(update_fields=["payload", "updated_at"])

    except SalesShipment.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "销售出库单不存在")
    except InventoryError as exc:
        return ServiceResult(False, exc.error_code, exc.message, exc.data)

    return ServiceResult(
        True,
        message="销售出库已确认",
        data={"shipment_id": shipment.id, "status": shipment.status, "items": item_results},
        next_action="view_detail",
    )


def confirm_sample_loan_out(
    sample_loan_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认借样出库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"sample_out:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该借样出库请求已处理")

            sample_loan = (
                SampleLoan.objects.select_for_update()
                .select_related("customer")
                .get(id=sample_loan_id)
            )
            if sample_loan.status != SampleLoan.Status.PENDING_APPROVAL:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有待审核借样单可以确认出库")

            items = list(
                SampleLoanItem.objects.select_for_update()
                .filter(sample_loan=sample_loan)
                .select_related("material")
                .order_by("material_id", "location_id", "batch_id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "借样单没有明细")

            item_results = []
            for item in items:
                if item.loan_qty <= ZERO:
                    continue
                if not item.batch_id or not item.location_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "借样出库明细必须选择批次和库位")
                deduct_result = deduct_batch_inventory(
                    batch_id=item.batch_id,
                    material_id=item.material_id,
                    location_id=item.location_id,
                    qty=item.loan_qty,
                    mismatch_message="借样明细与批次物料或库位不一致",
                )
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.SAMPLE_OUT,
                    material=item.material,
                    batch=item.batch,
                    location=item.location,
                    qty_delta=-item.loan_qty,
                    source_doc_type="sample_loan",
                    source_doc_id=sample_loan.id,
                    source_doc_no=sample_loan.sample_loan_no,
                    created_by_id=operator_id,
                )
                item.line_status = SampleLoanItem.LineStatus.OUT
                item.save(update_fields=["line_status"])
                item_results.append(
                    {
                        "sample_loan_item_id": item.id,
                        "material_id": item.material_id,
                        "loan_qty": str(item.loan_qty),
                        "batch_remaining_qty": str(deduct_result["batch_remaining_qty"]),
                        "inventory_qty": str(deduct_result["inventory_qty"]),
                    }
                )

            sample_loan.status = SampleLoan.Status.OUT
            sample_loan.save(update_fields=["status"])
            event = enqueue_pending_event(
                "sample_out",
                event_key,
                {"sample_loan_id": sample_loan.id, "operator_id": operator_id},
            )
            event.payload = {"sample_loan_id": sample_loan.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except SampleLoan.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "借样单不存在")
    except InventoryError as exc:
        return ServiceResult(False, exc.error_code, exc.message, exc.data)

    return ServiceResult(
        True,
        message="借样出库已确认",
        data={"sample_loan_id": sample_loan.id, "status": sample_loan.status, "items": item_results},
        next_action="view_detail",
    )


def convert_sample_loan_item_to_sales_order(
    sample_loan_item_id: int,
    convert_qty: Decimal,
    unit_price: Decimal,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "借样转销售需要幂等键")
    if convert_qty <= ZERO or unit_price < ZERO:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "转销售数量和单价必须正确")

    try:
        with transaction.atomic():
            event_key = f"sample_to_sales:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该借样转销售请求已处理")

            loan_item = (
                SampleLoanItem.objects.select_for_update()
                .select_related("sample_loan", "sample_loan__customer", "material")
                .get(id=sample_loan_item_id)
            )
            sample_loan = SampleLoan.objects.select_for_update().get(id=loan_item.sample_loan_id)
            if sample_loan.status not in [
                SampleLoan.Status.OUT,
                SampleLoan.Status.PART_RETURNED,
                SampleLoan.Status.PART_SOLD,
            ]:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前借样状态不能转销售")

            available_qty = loan_item.loan_qty - loan_item.returned_qty - loan_item.sold_qty
            if convert_qty > available_qty:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "转销售数量不能超过未归还未转销售数量")
            if not loan_item.batch_id or not loan_item.location_id:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "借样明细缺少出库批次或库位，不能转销售")

            customer_product = (
                CustomerProduct.objects.filter(
                    customer=sample_loan.customer,
                    finished_material=loan_item.material,
                    status=CustomerProduct.ProductStatus.ACTIVE,
                )
                .order_by("customer_product_no", "id")
                .first()
            )

            sales_order = SalesOrder.objects.create(
                sales_order_no=next_document_no("SO"),
                customer=sample_loan.customer,
                order_date=timezone.localdate(),
                delivery_date=sample_loan.expected_return_date,
                status=SalesOrder.Status.PENDING_APPROVAL,
                total_amount=(convert_qty * unit_price).quantize(Decimal("0.01")),
                created_by_id=operator_id,
                updated_by_id=operator_id,
                remark=f"由借样单 {sample_loan.sample_loan_no} 转销售生成",
            )
            sales_item = SalesOrderItem.objects.create(
                sales_order=sales_order,
                line_no=1,
                customer_product=customer_product,
                finished_material=loan_item.material,
                customer_model_remark=f"由借样单 {sample_loan.sample_loan_no} 转销售",
                order_qty=convert_qty,
                unit_price=unit_price,
                line_amount=(convert_qty * unit_price).quantize(Decimal("0.01")),
                line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
                inventory_check_status=SalesOrderItem.InventoryCheckStatus.SUFFICIENT,
                shipped_qty=convert_qty,
            )

            loan_item.sold_qty += convert_qty
            loan_item.line_status = _sample_loan_item_status(loan_item)
            loan_item.save(update_fields=["sold_qty", "line_status"])
            _refresh_sample_loan_status(sample_loan)

            InventoryTransaction.objects.create(
                transaction_no=next_document_no("IT"),
                transaction_type=InventoryTransaction.TransactionType.SAMPLE_TO_SALES,
                material=loan_item.material,
                batch=loan_item.batch,
                location=loan_item.location,
                qty_delta=ZERO,
                source_doc_type="sample_loan",
                source_doc_id=sample_loan.id,
                source_doc_no=sample_loan.sample_loan_no,
                created_by_id=operator_id,
            )
            event = enqueue_pending_event(
                "sample_to_sales",
                event_key,
                {
                    "sample_loan_id": sample_loan.id,
                    "sample_loan_item_id": loan_item.id,
                    "sales_order_id": sales_order.id,
                    "operator_id": operator_id,
                },
            )
            event.payload = {
                "sample_loan_id": sample_loan.id,
                "sample_loan_item_id": loan_item.id,
                "sales_order_id": sales_order.id,
                "operator_id": operator_id,
                "confirmed": True,
            }
            event.save(update_fields=["payload", "updated_at"])

    except SampleLoanItem.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "借样明细不存在")

    return ServiceResult(
        True,
        message="借样已转销售",
        data={
            "sample_loan_id": sample_loan.id,
            "sample_loan_item_id": loan_item.id,
            "sales_order_id": sales_order.id,
            "sales_order_item_id": sales_item.id,
        },
        next_action="view_sales_order",
    )


def confirm_customer_return_receipt(
    customer_return_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认客户退货入库需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"customer_return_in:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该客户退货入库请求已处理")

            customer_return = (
                CustomerReturn.objects.select_for_update()
                .select_related("customer")
                .get(id=customer_return_id)
            )
            if customer_return.status != CustomerReturn.Status.CONFIRMED:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "只有已确认客户退货单可以入库")

            items = list(
                CustomerReturnItem.objects.select_for_update()
                .filter(customer_return=customer_return)
                .select_related("material")
                .order_by("material_id", "location_id", "id")
            )
            if not items:
                return ServiceResult(False, "DOC_NOT_FOUND", "客户退货单没有明细")

            item_results = []
            for item in items:
                if item.return_qty <= ZERO:
                    continue
                if not item.location_id:
                    return ServiceResult(False, "STATE_INVALID_TRANSITION", "客户退货明细必须选择入库库位")
                inventory_type = item.inventory_type or InventoryBatch.InventoryType.AVAILABLE
                batch = InventoryBatch.objects.create(
                    batch_no=next_document_no("CRB"),
                    material=item.material,
                    location=item.location,
                    inventory_type=inventory_type,
                    received_at=timezone.now(),
                    initial_qty=item.return_qty,
                    remaining_qty=item.return_qty,
                    batch_status=InventoryBatch.BatchStatus.IN_STOCK,
                )
                inventory = _increase_inventory(item.material_id, item.location_id, item.return_qty, inventory_type)
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.CUSTOMER_RETURN_IN,
                    material=item.material,
                    batch=batch,
                    location=item.location,
                    qty_delta=item.return_qty,
                    source_doc_type="customer_return",
                    source_doc_id=customer_return.id,
                    source_doc_no=customer_return.return_no,
                    created_by_id=operator_id,
                )
                item_results.append(
                    {
                        "customer_return_item_id": item.id,
                        "material_id": item.material_id,
                        "return_qty": str(item.return_qty),
                        "batch_id": batch.id,
                        "inventory_qty": str(inventory.qty),
                    }
                )

            customer_return.status = CustomerReturn.Status.RECEIVED
            customer_return.save(update_fields=["status"])
            event = enqueue_pending_event(
                "customer_return_in",
                event_key,
                {"customer_return_id": customer_return.id, "operator_id": operator_id},
            )
            event.payload = {"customer_return_id": customer_return.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except CustomerReturn.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "客户退货单不存在")

    return ServiceResult(
        True,
        message="客户退货入库已确认",
        data={"customer_return_id": customer_return.id, "status": customer_return.status, "items": item_results},
        next_action="view_detail",
    )


def confirm_sample_return(
    sample_return_id: int,
    operator_id: int,
    idempotency_key: str,
) -> ServiceResult:
    if not idempotency_key:
        return ServiceResult(False, "STATE_INVALID_TRANSITION", "确认借样归还需要幂等键")

    try:
        with transaction.atomic():
            event_key = f"sample_returned:{idempotency_key}"
            existing_event = PendingEvent.objects.filter(idempotency_key=event_key).first()
            if existing_event and existing_event.payload.get("confirmed"):
                return ServiceResult(False, "STATE_ALREADY_PROCESSED", "该借样归还请求已处理")

            sample_return = (
                SampleLoanReturn.objects.select_for_update()
                .select_related("sample_loan", "customer")
                .get(id=sample_return_id)
            )
            if sample_return.status != SampleLoanReturn.Status.PENDING_CONFIRM:
                return ServiceResult(False, "STATE_INVALID_TRANSITION", "当前借样归还单状态不能确认入库")

            sample_loan = SampleLoan.objects.select_for_update().get(id=sample_return.sample_loan_id)
            return_items = list(
                SampleLoanReturnItem.objects.select_for_update()
                .filter(sample_return=sample_return)
                .select_related("sample_loan_item", "material", "location")
                .order_by("material_id", "location_id", "id")
            )
            if not return_items:
                return ServiceResult(False, "DOC_NOT_FOUND", "借样归还单没有明细")

            item_results = []
            for return_item in return_items:
                loan_item = SampleLoanItem.objects.select_for_update().get(id=return_item.sample_loan_item_id)
                available_to_return = loan_item.loan_qty - loan_item.returned_qty - loan_item.sold_qty
                if return_item.return_qty <= ZERO or return_item.return_qty > available_to_return:
                    return ServiceResult(False, "SAMPLE_RETURN_QTY_OVER", "归还数量不能超过未归还且未转销售数量")

                inventory_type = _sample_return_inventory_type(return_item.sample_condition)
                batch = _create_sample_return_batch(return_item, inventory_type)
                inventory = _increase_inventory(return_item.material_id, return_item.location_id, return_item.return_qty, inventory_type)
                InventoryTransaction.objects.create(
                    transaction_no=next_document_no("IT"),
                    transaction_type=InventoryTransaction.TransactionType.SAMPLE_RETURN_IN,
                    material=return_item.material,
                    batch=batch,
                    location=return_item.location,
                    qty_delta=return_item.return_qty,
                    source_doc_type="sample_loan_return",
                    source_doc_id=sample_return.id,
                    source_doc_no=sample_return.sample_return_no,
                    created_by_id=operator_id,
                )

                loan_item.returned_qty += return_item.return_qty
                loan_item.line_status = _sample_loan_item_status(loan_item)
                loan_item.save(update_fields=["returned_qty", "line_status"])
                item_results.append(
                    {
                        "item_id": return_item.id,
                        "sample_loan_item_id": loan_item.id,
                        "return_qty": str(return_item.return_qty),
                        "batch_id": batch.id,
                        "inventory_type": inventory_type,
                        "inventory_qty": str(inventory.qty),
                    }
                )

            sample_return.status = SampleLoanReturn.Status.RECEIVED
            sample_return.save(update_fields=["status"])
            _refresh_sample_loan_status(sample_loan)

            event = enqueue_pending_event(
                "sample_returned",
                event_key,
                {"sample_return_id": sample_return.id, "operator_id": operator_id},
            )
            event.payload = {"sample_return_id": sample_return.id, "operator_id": operator_id, "confirmed": True}
            event.save(update_fields=["payload", "updated_at"])

    except SampleLoanReturn.DoesNotExist:
        return ServiceResult(False, "DOC_NOT_FOUND", "借样归还单不存在")

    return ServiceResult(
        True,
        message="借样归还已确认",
        data={
            "sample_return_id": sample_return.id,
            "sample_loan_id": sample_loan.id,
            "sample_status": sample_loan.status,
            "overdue_status": sample_loan.overdue_status,
            "items": item_results,
        },
        next_action="view_detail",
    )


def _get_locked_sales_order(sales_order_id: int) -> SalesOrder | None:
    try:
        return SalesOrder.objects.select_for_update().get(id=sales_order_id)
    except SalesOrder.DoesNotExist:
        return None


def _lock_bom_and_evaluate_item(item: SalesOrderItem) -> dict:
    enabled_bom = (
        Bom.objects.select_for_update()
        .filter(finished_material=item.finished_material, status=Bom.BomStatus.ENABLED)
        .order_by("-is_default", "-enabled_at", "-id")
        .first()
    )
    if enabled_bom is None:
        item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.PENDING_BOM
        item.line_status = SalesOrderItem.LineStatus.CONFIRMED
        item.save(update_fields=["inventory_check_status", "line_status"])
        _close_item_shortages(item, "BOM 未启用，等待重新检查")
        return _line_result(item, SalesOrderItem.InventoryCheckStatus.PENDING_BOM, shortages=[])

    item.locked_bom = enabled_bom
    item.locked_bom_version = enabled_bom.bom_version
    return _evaluate_item_with_locked_or_enabled_bom(item)


def _evaluate_item_with_locked_or_enabled_bom(item: SalesOrderItem) -> dict:
    if item.locked_bom_id is None:
        return _lock_bom_and_evaluate_item(item)

    finished_available_qty = _available_qty(item.finished_material)
    if finished_available_qty >= item.order_qty:
        item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.SUFFICIENT
        item.line_status = SalesOrderItem.LineStatus.CONFIRMED
        item.save(update_fields=["inventory_check_status", "line_status", "locked_bom", "locked_bom_version"])
        _close_item_shortages(item, "成品库存充足")
        return _line_result(item, SalesOrderItem.InventoryCheckStatus.SUFFICIENT, finished_available_qty, [])

    shortages = _calculate_bom_shortages(item)
    required_shortages = [row for row in shortages if row["is_required"] and row["shortage_qty"] > ZERO]

    if required_shortages:
        item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.SHORTAGE
        item.line_status = SalesOrderItem.LineStatus.CONFIRMED
        item.save(update_fields=["inventory_check_status", "line_status", "locked_bom", "locked_bom_version"])
        _sync_shortage_alerts(item, shortages)
        return _line_result(item, SalesOrderItem.InventoryCheckStatus.SHORTAGE, finished_available_qty, shortages)

    item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.KITTED
    item.line_status = SalesOrderItem.LineStatus.CONFIRMED
    item.save(update_fields=["inventory_check_status", "line_status", "locked_bom", "locked_bom_version"])
    _mark_item_shortages_kitted(item)
    return _line_result(item, SalesOrderItem.InventoryCheckStatus.KITTED, finished_available_qty, shortages)


def _calculate_bom_shortages(item: SalesOrderItem) -> list[dict]:
    rows = []
    bom_items = (
        BomItem.objects.filter(bom=item.locked_bom)
        .select_related("bom", "component_material")
        .order_by("line_no")
    )
    for bom_item in bom_items:
        material = bom_item.component_material
        required_qty = required_component_qty_base(bom_item, item.order_qty)
        available_qty = _available_qty(material)
        shortage_qty = max(ZERO, required_qty - available_qty)
        rows.append(
            {
                "material_id": material.id,
                "material_code": material.material_code,
                "material_name": material.material_name,
                "required_qty": required_qty,
                "available_qty": available_qty,
                "shortage_qty": shortage_qty,
                "is_required": bom_item.is_required,
            }
        )
    return rows


def _available_qty(material: Material) -> Decimal:
    result = InventoryBatch.objects.filter(
        material=material,
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        remaining_qty__gt=0,
    ).aggregate(total=Sum("remaining_qty"))
    return result["total"] or ZERO


def _increase_inventory(material_id: int, location_id: int, qty: Decimal, inventory_type: str) -> Inventory:
    inventory, _ = (
        Inventory.objects.select_for_update()
        .get_or_create(
            material_id=material_id,
            location_id=location_id,
            inventory_type=inventory_type,
            defaults={"qty": ZERO},
        )
    )
    inventory.qty += qty
    inventory.save(update_fields=["qty", "updated_at"])
    return inventory


def _create_sample_return_batch(return_item: SampleLoanReturnItem, inventory_type: str) -> InventoryBatch:
    return InventoryBatch.objects.create(
        batch_no=next_document_no("BA"),
        material=return_item.material,
        location=return_item.location,
        inventory_type=inventory_type,
        received_at=timezone.now(),
        initial_qty=return_item.return_qty,
        remaining_qty=return_item.return_qty,
        batch_status=InventoryBatch.BatchStatus.IN_STOCK,
    )


def _sample_return_inventory_type(sample_condition: str) -> str:
    if sample_condition == SampleLoanReturnItem.SampleCondition.GOOD:
        return InventoryBatch.InventoryType.AVAILABLE
    return InventoryBatch.InventoryType.PENDING


def _sample_loan_item_status(loan_item: SampleLoanItem) -> str:
    closed_qty = loan_item.returned_qty + loan_item.sold_qty
    if closed_qty >= loan_item.loan_qty:
        if loan_item.sold_qty > ZERO and loan_item.returned_qty > ZERO:
            return SampleLoanItem.LineStatus.PART_SOLD
        if loan_item.sold_qty >= loan_item.loan_qty:
            return SampleLoanItem.LineStatus.SOLD
        return SampleLoanItem.LineStatus.RETURNED
    if loan_item.returned_qty > ZERO:
        return SampleLoanItem.LineStatus.PART_RETURNED
    if loan_item.sold_qty > ZERO:
        return SampleLoanItem.LineStatus.PART_SOLD
    return SampleLoanItem.LineStatus.OUT


def _refresh_sample_loan_status(sample_loan: SampleLoan) -> None:
    items = list(SampleLoanItem.objects.select_for_update().filter(sample_loan=sample_loan))
    if not items:
        return
    all_closed = all(item.returned_qty + item.sold_qty >= item.loan_qty for item in items)
    all_returned = all(item.returned_qty >= item.loan_qty for item in items)
    all_sold = all(item.sold_qty >= item.loan_qty for item in items)
    any_returned = any(item.returned_qty > ZERO for item in items)
    any_sold = any(item.sold_qty > ZERO for item in items)

    if all_returned:
        sample_loan.status = SampleLoan.Status.RETURNED
    elif all_sold:
        sample_loan.status = SampleLoan.Status.SOLD
    elif all_closed and any_returned and any_sold:
        sample_loan.status = SampleLoan.Status.PART_SOLD
    elif any_sold:
        sample_loan.status = SampleLoan.Status.PART_SOLD
    elif any_returned:
        sample_loan.status = SampleLoan.Status.PART_RETURNED

    if all_closed:
        sample_loan.is_overdue = False
        sample_loan.overdue_days = 0
        sample_loan.overdue_status = SampleLoan.OverdueStatus.CLOSED
    sample_loan.save(update_fields=["status", "is_overdue", "overdue_days", "overdue_status"])


def _sync_shortage_alerts(item: SalesOrderItem, shortages: list[dict]) -> None:
    active_alerts = {
        alert.material_id: alert
        for alert in ShortageAlert.objects.select_for_update().filter(
            sales_order_item=item,
            status__in=[
                ShortageAlert.Status.UNPROCESSED,
                ShortageAlert.Status.PURCHASE_REQUESTED,
                ShortageAlert.Status.PARTIAL_RECEIVED,
            ],
        )
    }
    shortage_material_ids = set()

    for row in shortages:
        if row["shortage_qty"] <= ZERO:
            continue
        shortage_material_ids.add(row["material_id"])
        alert = active_alerts.get(row["material_id"])
        if alert is None:
            ShortageAlert.objects.create(
                shortage_no=next_document_no("SA"),
                sales_order=item.sales_order,
                sales_order_item=item,
                material_id=row["material_id"],
                required_qty=row["required_qty"],
                available_qty=row["available_qty"],
                shortage_qty=row["shortage_qty"],
                is_required=row["is_required"],
                status=ShortageAlert.Status.UNPROCESSED,
            )
            continue

        if alert.status == ShortageAlert.Status.PURCHASE_REQUESTED:
            alert.status = ShortageAlert.Status.PARTIAL_RECEIVED
        alert.required_qty = row["required_qty"]
        alert.available_qty = row["available_qty"]
        alert.shortage_qty = row["shortage_qty"]
        alert.is_required = row["is_required"]
        alert.save(update_fields=["required_qty", "available_qty", "shortage_qty", "is_required", "status"])

    for material_id, alert in active_alerts.items():
        if material_id not in shortage_material_ids:
            alert.status = ShortageAlert.Status.KITTED
            alert.shortage_qty = ZERO
            alert.save(update_fields=["status", "shortage_qty"])


def _mark_item_shortages_kitted(item: SalesOrderItem) -> None:
    ShortageAlert.objects.select_for_update().filter(
        sales_order_item=item,
        status__in=[
            ShortageAlert.Status.UNPROCESSED,
            ShortageAlert.Status.PURCHASE_REQUESTED,
            ShortageAlert.Status.PARTIAL_RECEIVED,
        ],
    ).update(status=ShortageAlert.Status.KITTED, shortage_qty=ZERO)


def _close_item_shortages(item: SalesOrderItem, reason: str) -> None:
    ShortageAlert.objects.select_for_update().filter(
        sales_order_item=item,
        status__in=[
            ShortageAlert.Status.UNPROCESSED,
            ShortageAlert.Status.PURCHASE_REQUESTED,
            ShortageAlert.Status.PARTIAL_RECEIVED,
        ],
    ).update(status=ShortageAlert.Status.CLOSED, shortage_qty=ZERO, closed_reason=reason)


def _refresh_sales_order_header_status(sales_order: SalesOrder, operator_id: int | None) -> None:
    statuses = list(sales_order.items.values_list("inventory_check_status", flat=True))
    sales_order.status = _summarize_sales_order_status(
        has_pending_bom=SalesOrderItem.InventoryCheckStatus.PENDING_BOM in statuses,
        has_shortage=SalesOrderItem.InventoryCheckStatus.SHORTAGE in statuses,
    )
    if operator_id:
        sales_order.updated_by_id = operator_id
    sales_order.version += 1
    sales_order.save(update_fields=["status", "updated_by", "updated_at", "version"])


def _refresh_sales_order_after_shipment(sales_order: SalesOrder, operator_id: int | None) -> None:
    items = list(SalesOrderItem.objects.select_for_update().filter(sales_order=sales_order))
    if all(item.shipped_qty >= item.order_qty for item in items):
        sales_order.status = SalesOrder.Status.SHIPPED
    elif any(item.shipped_qty > ZERO for item in items):
        sales_order.status = SalesOrder.Status.CONFIRMED
    if operator_id:
        sales_order.updated_by_id = operator_id
    sales_order.version += 1
    sales_order.save(update_fields=["status", "updated_by", "updated_at", "version"])


def _summarize_sales_order_status(has_pending_bom: bool, has_shortage: bool) -> str:
    if has_pending_bom:
        return SalesOrder.Status.PENDING_BOM
    if has_shortage:
        return SalesOrder.Status.CONFIRMED
    return SalesOrder.Status.CONFIRMED


def _line_result(
    item: SalesOrderItem,
    inventory_check_status: str,
    finished_available_qty: Decimal | None = None,
    shortages: list[dict] | None = None,
) -> dict:
    return {
        "item_id": item.id,
        "line_no": item.line_no,
        "inventory_check_status": inventory_check_status,
        "locked_bom_id": item.locked_bom_id,
        "locked_bom_version": item.locked_bom_version,
        "finished_available_qty": str(finished_available_qty or ZERO),
        "shortages": [_stringify_shortage(row) for row in (shortages or [])],
    }


def _stringify_shortage(row: dict) -> dict:
    return {
        **row,
        "required_qty": str(row["required_qty"]),
        "available_qty": str(row["available_qty"]),
        "shortage_qty": str(row["shortage_qty"]),
    }


def _enqueue_sales_events(sales_order: SalesOrder, line_results: list[dict]) -> None:
    enqueue_pending_event(
        "sales_order_confirmed",
        f"sales_order_confirmed:{sales_order.id}",
        {"sales_order_id": sales_order.id},
    )
    for result in line_results:
        if result["inventory_check_status"] == SalesOrderItem.InventoryCheckStatus.SHORTAGE:
            enqueue_pending_event(
                "shortage_created",
                f"shortage_created:{result['item_id']}",
                {"sales_order_item_id": result["item_id"]},
            )
