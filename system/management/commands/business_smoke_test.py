from __future__ import annotations

from decimal import Decimal
from secrets import token_hex

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bom.models import Bom, BomItem
from finance.models import (
    CustomerCreditBalance,
    CustomerCreditBalanceTransaction,
    CustomerReceipt,
    CustomerReceiptAllocation,
    CustomerReceiptReversal,
    SupplierCreditBalance,
    SupplierCreditBalanceTransaction,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from finance.services import (
    apply_customer_credit_balance,
    apply_supplier_credit_balance,
    confirm_customer_receipt,
    confirm_supplier_payment,
    reverse_customer_receipt,
    reverse_supplier_payment,
)
from inventory.models import Inventory, InventoryBatch, WarehouseLocation
from masterdata.models import Customer, CustomerProduct, Material, MaterialSupplierPrice, Supplier
from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
from purchase.services import (
    confirm_purchase_receipt,
    create_purchase_order_from_request,
    create_purchase_request_from_shortages,
)
from production.models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)
from production.services import confirm_material_requisition, confirm_production_receipt
from sales.models import SalesOrder, SalesOrderItem, SalesShipment, SalesShipmentItem, ShortageAlert
from sales.models import SampleLoan, SampleLoanItem, SampleLoanReturn, SampleLoanReturnItem
from sales.services import (
    confirm_sales_order,
    confirm_sales_shipment,
    confirm_sample_loan_out,
    confirm_sample_return,
    convert_sample_loan_item_to_sales_order,
)
from system.services import ServiceResult, next_document_no, process_pending_events


class SmokeRollback(Exception):
    pass


class Command(BaseCommand):
    help = "执行上线前业务冒烟测试；默认回滚临时数据，加 --commit 才保留"

    def add_arguments(self, parser):
        parser.add_argument("--operator", default="", help="执行人用户名；为空时使用第一个启用超级管理员或启用用户")
        parser.add_argument("--commit", action="store_true", help="保留冒烟测试数据，默认执行完成后回滚")

    def handle(self, *args, **options):
        operator = self._resolve_operator(options["operator"].strip())
        operator_id = operator.id if operator else None
        tag = f"{timezone.now():%Y%m%d%H%M%S}{token_hex(2).upper()}"
        checkpoints: list[dict] = []

        try:
            with transaction.atomic():
                checkpoints = _run_business_smoke(tag=tag, operator_id=operator_id)
                if not options["commit"]:
                    raise SmokeRollback
        except SmokeRollback:
            pass
        except Exception as exc:
            raise CommandError(f"业务冒烟测试失败：{exc}") from exc

        for checkpoint in checkpoints:
            self.stdout.write(self.style.SUCCESS(f"[OK] {checkpoint['name']}: {checkpoint['message']}"))

        mode = "已保留冒烟数据" if options["commit"] else "已回滚冒烟数据"
        operator_label = operator.username if operator else "-"
        self.stdout.write(self.style.SUCCESS(f"业务冒烟测试通过：tag={tag}, operator={operator_label}, {mode}"))

    def _resolve_operator(self, username: str):
        User = get_user_model()
        if username:
            user = User.objects.filter(username=username, is_active=True, is_deleted=False).first()
            if user is None:
                raise CommandError(f"执行人不存在或不可用：{username}")
            return user
        return (
            User.objects.filter(is_superuser=True, is_active=True, is_deleted=False).order_by("id").first()
            or User.objects.filter(is_active=True, is_deleted=False).order_by("id").first()
        )


def _run_business_smoke(tag: str, operator_id: int | None) -> list[dict]:
    fixture = _create_fixture(tag, operator_id)
    checkpoints = []
    checkpoints.append(_check_finished_stock_sales_flow(fixture, operator_id, tag))
    production_context = _check_shortage_to_purchase_and_kitting_flow(fixture, operator_id, tag)
    checkpoints.append(production_context["checkpoint"])
    checkpoints.append(_check_production_issue_and_receipt_flow(production_context, operator_id, tag))
    checkpoints.append(_check_sample_loan_return_and_sales_flow(fixture, operator_id, tag))
    checkpoints.append(_check_customer_receipt_finance_flow(fixture, operator_id, tag))
    checkpoints.append(_check_supplier_payment_finance_flow(fixture, operator_id, tag))
    return checkpoints


def _create_fixture(tag: str, operator_id: int | None) -> dict:
    location = WarehouseLocation.objects.create(
        location_code=f"SMK-LOC-{tag}",
        location_name="冒烟测试库位",
    )
    customer = Customer.objects.create(
        customer_no=f"SMK-C-{tag}",
        customer_name="冒烟测试客户",
        sales_owner_id=operator_id,
    )
    supplier = Supplier.objects.create(
        supplier_no=f"SMK-S-{tag}",
        supplier_name="冒烟测试供应商",
    )
    stock_finished = Material.objects.create(
        material_code=f"SMK-FG-STOCK-{tag}",
        material_name="冒烟测试库存成品",
        material_type=Material.MaterialType.FINISHED,
        base_unit="pcs",
        qty_precision=0,
    )
    assembly_finished = Material.objects.create(
        material_code=f"SMK-FG-BOM-{tag}",
        material_name="冒烟测试 BOM 成品",
        material_type=Material.MaterialType.FINISHED,
        base_unit="pcs",
        qty_precision=0,
    )
    raw_material = Material.objects.create(
        material_code=f"SMK-RM-{tag}",
        material_name="冒烟测试原料",
        material_type=Material.MaterialType.RAW,
        base_unit="pcs",
        qty_precision=0,
        latest_purchase_price=Decimal("2.000000"),
    )
    stock_product = CustomerProduct.objects.create(
        customer=customer,
        customer_product_no=f"SMK-CP-STOCK-{tag}",
        customer_product_name="冒烟库存成品客户产品",
        finished_material=stock_finished,
        default_sale_price=Decimal("10.0000"),
    )
    assembly_product = CustomerProduct.objects.create(
        customer=customer,
        customer_product_no=f"SMK-CP-BOM-{tag}",
        customer_product_name="冒烟 BOM 成品客户产品",
        finished_material=assembly_finished,
        default_sale_price=Decimal("20.0000"),
    )
    MaterialSupplierPrice.objects.create(
        material=raw_material,
        supplier=supplier,
        purchase_price=Decimal("2.000000"),
        is_default=True,
    )
    stock_bom = Bom.objects.create(
        bom_no=f"SMK-BOM-STOCK-{tag}",
        finished_material=stock_finished,
        bom_version="V1",
        base_qty=Decimal("1.0000"),
        status=Bom.BomStatus.ENABLED,
        is_default=True,
        enabled_at=timezone.now(),
        created_by_id=operator_id,
        approved_by_id=operator_id,
        approved_at=timezone.now(),
    )
    BomItem.objects.create(
        bom=stock_bom,
        line_no=1,
        component_material=raw_material,
        usage_qty=Decimal("1.000000"),
        usage_unit="pcs",
        loss_rate=Decimal("0.000000"),
        is_required=True,
    )
    bom = Bom.objects.create(
        bom_no=f"SMK-BOM-{tag}",
        finished_material=assembly_finished,
        bom_version="V1",
        base_qty=Decimal("1.0000"),
        status=Bom.BomStatus.ENABLED,
        is_default=True,
        enabled_at=timezone.now(),
        created_by_id=operator_id,
        approved_by_id=operator_id,
        approved_at=timezone.now(),
    )
    BomItem.objects.create(
        bom=bom,
        line_no=1,
        component_material=raw_material,
        usage_qty=Decimal("2.000000"),
        usage_unit="pcs",
        loss_rate=Decimal("0.000000"),
        is_required=True,
    )

    finished_batch = InventoryBatch.objects.create(
        batch_no=f"SMK-BATCH-FG-{tag}",
        material=stock_finished,
        location=location,
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        received_at=timezone.now(),
        initial_qty=Decimal("5.0000"),
        remaining_qty=Decimal("5.0000"),
        cost_price=Decimal("4.000000"),
        batch_status=InventoryBatch.BatchStatus.IN_STOCK,
    )
    Inventory.objects.create(
        material=stock_finished,
        location=location,
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        qty=Decimal("5.0000"),
    )

    return {
        "location": location,
        "customer": customer,
        "supplier": supplier,
        "stock_finished": stock_finished,
        "assembly_finished": assembly_finished,
        "raw_material": raw_material,
        "stock_product": stock_product,
        "assembly_product": assembly_product,
        "finished_batch": finished_batch,
    }


def _check_finished_stock_sales_flow(fixture: dict, operator_id: int | None, tag: str) -> dict:
    sales_order = SalesOrder.objects.create(
        sales_order_no=f"SMK-SO-STOCK-{tag}",
        customer=fixture["customer"],
        order_date=timezone.localdate(),
        status=SalesOrder.Status.PENDING_APPROVAL,
        total_amount=Decimal("20.00"),
        created_by_id=operator_id,
    )
    sales_item = SalesOrderItem.objects.create(
        sales_order=sales_order,
        line_no=1,
        customer_product=fixture["stock_product"],
        finished_material=fixture["stock_finished"],
        order_qty=Decimal("2.0000"),
        unit_price=Decimal("10.0000"),
        line_amount=Decimal("20.00"),
        line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
    )

    _assert_result(confirm_sales_order(sales_order.id, operator_id), "销售订单库存检查失败")
    sales_item.refresh_from_db()
    if sales_item.inventory_check_status != SalesOrderItem.InventoryCheckStatus.SUFFICIENT:
        raise CommandError("库存成品订单未识别为库存充足")

    shipment = SalesShipment.objects.create(
        shipment_no=f"SMK-SH-{tag}",
        sales_order=sales_order,
        customer=fixture["customer"],
        shipment_date=timezone.localdate(),
        status=SalesShipment.Status.PENDING_CONFIRM,
        created_by_id=operator_id,
    )
    SalesShipmentItem.objects.create(
        shipment=shipment,
        sales_order_item=sales_item,
        material=fixture["stock_finished"],
        shipment_qty=Decimal("2.0000"),
        batch=fixture["finished_batch"],
        location=fixture["location"],
        cost_price=Decimal("4.000000"),
    )

    _assert_result(confirm_sales_shipment(shipment.id, operator_id, f"smoke-shipment-{tag}"), "销售出库确认失败")
    fixture["finished_batch"].refresh_from_db()
    sales_order.refresh_from_db()
    if fixture["finished_batch"].remaining_qty != Decimal("3.0000"):
        raise CommandError("销售出库后成品批次剩余数量不正确")
    if sales_order.status != SalesOrder.Status.SHIPPED:
        raise CommandError("销售出库后销售订单未更新为已发货")

    return {"name": "库存成品销售出库", "message": "销售确认、出库扣减和订单状态更新通过"}


def _check_shortage_to_purchase_and_kitting_flow(fixture: dict, operator_id: int | None, tag: str) -> dict:
    sales_order = SalesOrder.objects.create(
        sales_order_no=f"SMK-SO-BOM-{tag}",
        customer=fixture["customer"],
        order_date=timezone.localdate(),
        delivery_date=timezone.localdate(),
        status=SalesOrder.Status.PENDING_APPROVAL,
        total_amount=Decimal("60.00"),
        created_by_id=operator_id,
    )
    sales_item = SalesOrderItem.objects.create(
        sales_order=sales_order,
        line_no=1,
        customer_product=fixture["assembly_product"],
        finished_material=fixture["assembly_finished"],
        order_qty=Decimal("3.0000"),
        unit_price=Decimal("20.0000"),
        line_amount=Decimal("60.00"),
        line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
    )

    _assert_result(confirm_sales_order(sales_order.id, operator_id), "BOM 销售订单确认失败")
    sales_item.refresh_from_db()
    if sales_item.inventory_check_status != SalesOrderItem.InventoryCheckStatus.SHORTAGE:
        raise CommandError("BOM 成品订单未生成欠料状态")

    shortage = ShortageAlert.objects.get(sales_order_item=sales_item, material=fixture["raw_material"])
    if shortage.shortage_qty != Decimal("6.0000"):
        raise CommandError(f"欠料数量不正确：{shortage.shortage_qty}")

    request_result = create_purchase_request_from_shortages(
        [shortage.id],
        operator_id=operator_id,
        idempotency_key=f"smoke-pr-{tag}",
    )
    _assert_result(request_result, "欠料生成采购需求失败")
    purchase_request_id = request_result.data["purchase_request_id"]

    order_result = create_purchase_order_from_request(
        purchase_request_id,
        fixture["supplier"].id,
        operator_id=operator_id,
        idempotency_key=f"smoke-po-{tag}",
    )
    _assert_result(order_result, "采购需求生成采购单失败")
    purchase_order = PurchaseOrder.objects.get(id=order_result.data["purchase_order_id"])
    purchase_order_item = purchase_order.items.get(material=fixture["raw_material"])

    receipt = PurchaseReceipt.objects.create(
        purchase_receipt_no=f"SMK-PI-{tag}",
        purchase_order=purchase_order,
        supplier=fixture["supplier"],
        receipt_date=timezone.localdate(),
        status=PurchaseReceipt.Status.PENDING_RECEIVE,
        created_by_id=operator_id,
    )
    PurchaseReceiptItem.objects.create(
        purchase_receipt=receipt,
        purchase_order_item=purchase_order_item,
        material=fixture["raw_material"],
        received_qty=Decimal("6.0000"),
        accepted_qty=Decimal("6.0000"),
        rejected_qty=Decimal("0.0000"),
        unit_price=Decimal("2.000000"),
        location=fixture["location"],
    )

    _assert_result(confirm_purchase_receipt(receipt.id, operator_id, f"smoke-receipt-{tag}"), "采购入库确认失败")
    sales_item.refresh_from_db()
    shortage.refresh_from_db()
    if sales_item.inventory_check_status != SalesOrderItem.InventoryCheckStatus.SHORTAGE:
        raise CommandError("采购入库确认后销售订单明细应等待事务后事件重检")
    if shortage.status != ShortageAlert.Status.PURCHASE_REQUESTED:
        raise CommandError("采购入库确认后欠料提醒应等待事务后事件重检")

    _assert_result(process_pending_events(event_type="purchase_received"), "采购入库后事务事件处理失败")
    sales_item.refresh_from_db()
    shortage.refresh_from_db()
    if sales_item.inventory_check_status != SalesOrderItem.InventoryCheckStatus.KITTED:
        raise CommandError("采购入库事件处理后销售订单明细未更新为已齐套")
    if shortage.status != ShortageAlert.Status.KITTED:
        raise CommandError("采购入库事件处理后欠料提醒未更新为已齐套")

    return {
        "fixture": fixture,
        "sales_order": sales_order,
        "sales_item": sales_item,
        "purchase_order": purchase_order,
        "purchase_order_item": purchase_order_item,
        "raw_batch": receipt.items.get(material=fixture["raw_material"]).batch,
        "checkpoint": {"name": "欠料采购入库齐套", "message": "欠料、采购需求、采购单、入库和齐套重检通过"},
    }


def _check_production_issue_and_receipt_flow(context: dict, operator_id: int | None, tag: str) -> dict:
    fixture = context["fixture"]
    sales_item = context["sales_item"]
    raw_batch = context["raw_batch"]

    production_order = ProductionOrder.objects.create(
        production_order_no=f"SMK-MO-{tag}",
        sales_order_item=sales_item,
        finished_material=fixture["assembly_finished"],
        production_qty=Decimal("3.0000"),
        locked_bom=sales_item.locked_bom,
        locked_bom_version=sales_item.locked_bom_version,
        status=ProductionOrder.Status.PENDING,
        created_by_id=operator_id,
    )
    requisition = ProductionMaterialRequisition.objects.create(
        requisition_no=f"SMK-MR-{tag}",
        production_order=production_order,
        requisition_date=timezone.localdate(),
        status=ProductionMaterialRequisition.Status.PENDING_CONFIRM,
        created_by_id=operator_id,
    )
    ProductionMaterialRequisitionItem.objects.create(
        requisition=requisition,
        production_order=production_order,
        line_no=1,
        material=fixture["raw_material"],
        required_qty=Decimal("6.0000"),
        issued_qty=Decimal("6.0000"),
        batch=raw_batch,
        location=fixture["location"],
    )

    _assert_result(confirm_material_requisition(requisition.id, operator_id, f"smoke-issue-{tag}"), "生产领料确认失败")
    raw_batch.refresh_from_db()
    production_order.refresh_from_db()
    sales_item.refresh_from_db()
    if raw_batch.remaining_qty != Decimal("0.0000"):
        raise CommandError(f"生产领料后原料批次剩余数量不正确：{raw_batch.remaining_qty}")
    if production_order.status != ProductionOrder.Status.IN_PROGRESS:
        raise CommandError("生产领料后生产指令未进入生产中")
    if sales_item.line_status != SalesOrderItem.LineStatus.IN_PRODUCTION:
        raise CommandError("生产领料后销售订单明细未进入生产中")

    receipt = ProductionReceipt.objects.create(
        production_receipt_no=f"SMK-PRC-{tag}",
        production_order=production_order,
        receipt_date=timezone.localdate(),
        status=ProductionReceipt.Status.PENDING_CONFIRM,
        created_by_id=operator_id,
    )
    ProductionReceiptItem.objects.create(
        production_receipt=receipt,
        production_order=production_order,
        line_no=1,
        finished_material=fixture["assembly_finished"],
        receipt_qty=Decimal("3.0000"),
        location=fixture["location"],
        quality_status=ProductionReceiptItem.QualityStatus.QUALIFIED,
    )

    _assert_result(confirm_production_receipt(receipt.id, operator_id, f"smoke-production-receipt-{tag}"), "生产入库确认失败")
    production_order.refresh_from_db()
    sales_item.refresh_from_db()
    finished_inventory = Inventory.objects.get(
        material=fixture["assembly_finished"],
        location=fixture["location"],
        inventory_type=InventoryBatch.InventoryType.AVAILABLE,
    )
    if production_order.status != ProductionOrder.Status.COMPLETED:
        raise CommandError("生产入库后生产指令未完成")
    if finished_inventory.qty != Decimal("3.0000"):
        raise CommandError(f"生产入库后成品库存不正确：{finished_inventory.qty}")
    if sales_item.inventory_check_status != SalesOrderItem.InventoryCheckStatus.SUFFICIENT:
        raise CommandError("生产入库后销售订单明细未更新为库存充足")

    return {"name": "生产领料与生产入库", "message": "原料扣减、生产状态、成品入库和销售可发货状态通过"}


def _check_sample_loan_return_and_sales_flow(fixture: dict, operator_id: int | None, tag: str) -> dict:
    sample_loan = SampleLoan.objects.create(
        sample_loan_no=f"SMK-SL-{tag}",
        customer=fixture["customer"],
        loan_date=timezone.localdate(),
        expected_return_date=timezone.localdate(),
        status=SampleLoan.Status.PENDING_APPROVAL,
        created_by_id=operator_id,
    )
    loan_item = SampleLoanItem.objects.create(
        sample_loan=sample_loan,
        line_no=1,
        material=fixture["stock_finished"],
        loan_qty=Decimal("2.0000"),
        batch=fixture["finished_batch"],
        location=fixture["location"],
        line_status=SampleLoanItem.LineStatus.OUT,
    )

    _assert_result(confirm_sample_loan_out(sample_loan.id, operator_id, f"smoke-sample-out-{tag}"), "借样出库确认失败")
    sample_loan.refresh_from_db()
    fixture["finished_batch"].refresh_from_db()
    if sample_loan.status != SampleLoan.Status.OUT:
        raise CommandError("借样出库后借样单状态不正确")
    if fixture["finished_batch"].remaining_qty != Decimal("1.0000"):
        raise CommandError(f"借样出库后原成品批次剩余数量不正确：{fixture['finished_batch'].remaining_qty}")

    sample_return = SampleLoanReturn.objects.create(
        sample_return_no=f"SMK-SR-{tag}",
        sample_loan=sample_loan,
        customer=fixture["customer"],
        return_date=timezone.localdate(),
        status=SampleLoanReturn.Status.PENDING_CONFIRM,
    )
    SampleLoanReturnItem.objects.create(
        sample_return=sample_return,
        sample_loan=sample_loan,
        sample_loan_item=loan_item,
        material=fixture["stock_finished"],
        return_qty=Decimal("1.0000"),
        location=fixture["location"],
        sample_condition=SampleLoanReturnItem.SampleCondition.GOOD,
    )

    _assert_result(confirm_sample_return(sample_return.id, operator_id, f"smoke-sample-return-{tag}"), "借样归还确认失败")
    sample_return.refresh_from_db()
    sample_loan.refresh_from_db()
    loan_item.refresh_from_db()
    if sample_return.status != SampleLoanReturn.Status.RECEIVED:
        raise CommandError("借样归还单确认后状态不正确")
    if sample_loan.status != SampleLoan.Status.PART_RETURNED or loan_item.returned_qty != Decimal("1.0000"):
        raise CommandError("借样部分归还后借样状态或归还数量不正确")

    convert_result = convert_sample_loan_item_to_sales_order(
        loan_item.id,
        Decimal("1.0000"),
        Decimal("10.0000"),
        operator_id,
        f"smoke-sample-sales-{tag}",
    )
    _assert_result(convert_result, "借样转销售失败")
    sample_loan.refresh_from_db()
    loan_item.refresh_from_db()
    sales_order = SalesOrder.objects.get(id=convert_result.data["sales_order_id"])
    sales_item = sales_order.items.get()
    if sample_loan.status != SampleLoan.Status.PART_SOLD or sample_loan.overdue_status != SampleLoan.OverdueStatus.CLOSED:
        raise CommandError("借样转销售后借样单状态或逾期状态不正确")
    if loan_item.sold_qty != Decimal("1.0000") or loan_item.line_status != SampleLoanItem.LineStatus.PART_SOLD:
        raise CommandError("借样转销售后借样明细状态不正确")
    if sales_order.status != SalesOrder.Status.PENDING_APPROVAL or sales_item.shipped_qty != Decimal("1.0000"):
        raise CommandError("借样转销售生成的销售订单状态或已发货数量不正确")

    return {"name": "借样出库归还与转销售", "message": "借样出库扣减、部分归还入库、剩余转销售和逾期关闭通过"}


def _check_customer_receipt_finance_flow(fixture: dict, operator_id: int | None, tag: str) -> dict:
    sales_order = SalesOrder.objects.create(
        sales_order_no=f"SMK-SO-FIN-{tag}",
        customer=fixture["customer"],
        order_date=timezone.localdate(),
        status=SalesOrder.Status.SHIPPED,
        total_amount=Decimal("100.00"),
        created_by_id=operator_id,
    )
    SalesOrderItem.objects.create(
        sales_order=sales_order,
        line_no=1,
        customer_product=fixture["stock_product"],
        finished_material=fixture["stock_finished"],
        order_qty=Decimal("10.0000"),
        unit_price=Decimal("10.0000"),
        line_amount=Decimal("100.00"),
        line_status=SalesOrderItem.LineStatus.SHIPPED,
    )
    receipt = CustomerReceipt.objects.create(
        receipt_no=f"SMK-RC-{tag}",
        customer=fixture["customer"],
        receipt_date=timezone.localdate(),
        receipt_amount=Decimal("120.00"),
        status=CustomerReceipt.Status.PENDING_APPROVAL,
        created_by_id=operator_id,
    )

    _assert_result(
        confirm_customer_receipt(
            receipt.id,
            [{"sales_order_id": sales_order.id, "allocated_amount": "100.00"}],
            operator_id,
            f"smoke-customer-receipt-{tag}",
        ),
        "客户收款核销确认失败",
    )
    receipt.refresh_from_db()
    balance = CustomerCreditBalance.objects.get(source_doc_type="customer_receipt", source_doc_id=receipt.id)
    allocation = CustomerReceiptAllocation.objects.get(customer_receipt=receipt, sales_order=sales_order)
    if receipt.status != CustomerReceipt.Status.CONFIRMED or receipt.unallocated_amount != Decimal("20.00"):
        raise CommandError("客户收款确认后的状态或未分配金额不正确")
    if allocation.allocated_amount != Decimal("100.00"):
        raise CommandError("客户收款核销金额不正确")
    if balance.remaining_amount != Decimal("20.00") or balance.status != CustomerCreditBalance.Status.PENDING:
        raise CommandError("客户超收待处理余额生成不正确")

    _assert_result(
        apply_customer_credit_balance(
            balance.id,
            CustomerCreditBalanceTransaction.ActionType.CLOSE,
            Decimal("20.00"),
            operator_id,
            reason="冒烟测试关闭超收余额",
            idempotency_key=f"smoke-customer-balance-{tag}",
        ),
        "客户待处理余额处理失败",
    )
    balance.refresh_from_db()
    if balance.remaining_amount != Decimal("0.00") or balance.status != CustomerCreditBalance.Status.CLOSED:
        raise CommandError("客户待处理余额关闭后状态不正确")

    _assert_result(
        reverse_customer_receipt(
            receipt.id,
            Decimal("100.00"),
            "冒烟测试红冲客户收款核销",
            operator_id,
            f"smoke-customer-reversal-{tag}",
        ),
        "客户收款红冲失败",
    )
    receipt.refresh_from_db()
    reversal = CustomerReceiptReversal.objects.get(source_receipt=receipt)
    reverse_allocation = CustomerReceiptAllocation.objects.get(customer_receipt=receipt, source_reversal=reversal)
    if receipt.status != CustomerReceipt.Status.PART_REVERSED:
        raise CommandError("客户收款红冲后状态不正确")
    if reversal.reversal_amount != Decimal("100.00") or reverse_allocation.allocated_amount != Decimal("-100.00"):
        raise CommandError("客户收款红冲金额或反向核销金额不正确")

    return {"name": "客户收款核销与红冲", "message": "收款核销、超收余额处理和红冲反向核销通过"}


def _check_supplier_payment_finance_flow(fixture: dict, operator_id: int | None, tag: str) -> dict:
    purchase_order = PurchaseOrder.objects.create(
        purchase_order_no=f"SMK-PO-FIN-{tag}",
        supplier=fixture["supplier"],
        status=PurchaseOrder.Status.RECEIVED,
        order_date=timezone.localdate(),
        total_amount=Decimal("100.00"),
        created_by_id=operator_id,
    )
    purchase_order_item = PurchaseOrderItem.objects.create(
        purchase_order=purchase_order,
        line_no=1,
        material=fixture["raw_material"],
        order_qty=Decimal("10.0000"),
        received_qty=Decimal("10.0000"),
        unit_price=Decimal("10.000000"),
        line_amount=Decimal("100.00"),
        line_status=PurchaseOrderItem.LineStatus.RECEIVED,
    )
    purchase_receipt = PurchaseReceipt.objects.create(
        purchase_receipt_no=f"SMK-GR-FIN-{tag}",
        purchase_order=purchase_order,
        supplier=fixture["supplier"],
        receipt_date=timezone.localdate(),
        status=PurchaseReceipt.Status.RECEIVED,
        created_by_id=operator_id,
    )
    PurchaseReceiptItem.objects.create(
        purchase_receipt=purchase_receipt,
        purchase_order_item=purchase_order_item,
        material=fixture["raw_material"],
        received_qty=Decimal("10.0000"),
        accepted_qty=Decimal("10.0000"),
        rejected_qty=Decimal("0.0000"),
        unit_price=Decimal("10.000000"),
        location=fixture["location"],
    )
    payment = SupplierPayment.objects.create(
        payment_no=f"SMK-PY-{tag}",
        supplier=fixture["supplier"],
        payment_date=timezone.localdate(),
        payment_amount=Decimal("120.00"),
        status=SupplierPayment.Status.PENDING_APPROVAL,
        created_by_id=operator_id,
    )

    _assert_result(
        confirm_supplier_payment(
            payment.id,
            [{"purchase_receipt_id": purchase_receipt.id, "allocated_amount": "100.00"}],
            operator_id,
            f"smoke-supplier-payment-{tag}",
        ),
        "供应商付款核销确认失败",
    )
    payment.refresh_from_db()
    balance = SupplierCreditBalance.objects.get(source_doc_type="supplier_payment", source_doc_id=payment.id)
    allocation = SupplierPaymentAllocation.objects.get(supplier_payment=payment, purchase_receipt=purchase_receipt)
    if payment.status != SupplierPayment.Status.CONFIRMED or payment.unallocated_amount != Decimal("20.00"):
        raise CommandError("供应商付款确认后的状态或未分配金额不正确")
    if allocation.allocated_amount != Decimal("100.00"):
        raise CommandError("供应商付款核销金额不正确")
    if balance.remaining_amount != Decimal("20.00") or balance.status != SupplierCreditBalance.Status.PENDING:
        raise CommandError("供应商超付待处理余额生成不正确")

    _assert_result(
        apply_supplier_credit_balance(
            balance.id,
            SupplierCreditBalanceTransaction.ActionType.CLOSE,
            Decimal("20.00"),
            operator_id,
            reason="冒烟测试关闭超付余额",
            idempotency_key=f"smoke-supplier-balance-{tag}",
        ),
        "供应商待处理余额处理失败",
    )
    balance.refresh_from_db()
    if balance.remaining_amount != Decimal("0.00") or balance.status != SupplierCreditBalance.Status.CLOSED:
        raise CommandError("供应商待处理余额关闭后状态不正确")

    _assert_result(
        reverse_supplier_payment(
            payment.id,
            Decimal("100.00"),
            "冒烟测试红冲供应商付款核销",
            operator_id,
            f"smoke-supplier-reversal-{tag}",
        ),
        "供应商付款红冲失败",
    )
    payment.refresh_from_db()
    reversal = SupplierPaymentReversal.objects.get(source_payment=payment)
    reverse_allocation = SupplierPaymentAllocation.objects.get(supplier_payment=payment, source_reversal=reversal)
    if payment.status != SupplierPayment.Status.PART_REVERSED:
        raise CommandError("供应商付款红冲后状态不正确")
    if reversal.reversal_amount != Decimal("100.00") or reverse_allocation.allocated_amount != Decimal("-100.00"):
        raise CommandError("供应商付款红冲金额或反向核销金额不正确")

    return {"name": "供应商付款核销与红冲", "message": "付款核销、超付余额处理和红冲反向核销通过"}


def _assert_result(result: ServiceResult, message: str) -> None:
    if not result.success:
        raise CommandError(f"{message}：{result.error_code or ''} {result.message}".strip())
