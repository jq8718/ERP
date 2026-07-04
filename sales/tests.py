from datetime import date
from decimal import Decimal
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from bom.models import Bom, BomItem
from files.models import Attachment, ExportLog, ImportJob, PrintLog
from inventory.models import Inventory, InventoryBatch, InventoryTransaction, WarehouseLocation
from masterdata.models import Customer, CustomerAddress, CustomerProduct, Material
from purchase.models import PurchaseRequest, PurchaseRequestItem
from purchase.services import create_purchase_request_from_shortages
from sales.models import (
    SalesOrder,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
    SampleLoan,
    SampleLoanItem,
    SampleLoanReturn,
    SampleLoanReturnItem,
    ShortageAlert,
    CustomerReturn,
    CustomerReturnItem,
)
from sales.services import (
    confirm_customer_return_receipt,
    confirm_sales_order,
    confirm_sales_shipment,
    confirm_sample_loan_out,
    confirm_sample_return,
    convert_sample_loan_item_to_sales_order,
)
from system.models import AuditLog, PendingEvent


class SalesServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tester", password="x")
        self.customer = Customer.objects.create(
            customer_no="C001",
            customer_name="测试客户",
            sales_owner=self.user,
        )
        self.location = WarehouseLocation.objects.create(location_code="A01", location_name="A01")
        self.finished = Material.objects.create(
            material_code="FG001",
            material_name="成品 1",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
            qty_precision=0,
        )
        self.raw = Material.objects.create(
            material_code="RM001",
            material_name="原料 1",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            qty_precision=0,
        )
        self.customer_product = CustomerProduct.objects.create(
            customer=self.customer,
            customer_product_no="CP001",
            customer_product_name="客户产品 1",
            finished_material=self.finished,
            default_sale_price=Decimal("10.0000"),
        )
        self.bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            status=Bom.BomStatus.ENABLED,
            enabled_at=timezone.now(),
        )
        BomItem.objects.create(
            bom=self.bom,
            line_no=1,
            component_material=self.raw,
            usage_qty=Decimal("2.000000"),
            usage_unit="pcs",
            loss_rate=Decimal("0"),
            is_required=True,
        )

    def _grant_permission(self, permission_code: str):
        permission_types = {
            PermissionCode.SALES_VIEW_ALL: Permission.PermissionType.DATA_SCOPE,
            PermissionCode.FINANCE_VIEW_AMOUNT: Permission.PermissionType.FIELD,
            PermissionCode.ATTACHMENT_VIEW_SENSITIVE: Permission.PermissionType.FIELD,
            PermissionCode.SALES_PROCESS: Permission.PermissionType.ACTION,
        }
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_types.get(permission_code, Permission.PermissionType.ACTION),
            },
        )
        role = Role.objects.create(role_code=f"sales-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def _sales_order(self, qty=Decimal("10")):
        order = SalesOrder.objects.create(
            sales_order_no="SO001",
            customer=self.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.PENDING_APPROVAL,
            total_amount=Decimal("100.00"),
        )
        item = SalesOrderItem.objects.create(
            sales_order=order,
            line_no=1,
            customer_product=self.customer_product,
            finished_material=self.finished,
            order_qty=qty,
            unit_price=Decimal("10.0000"),
            line_amount=Decimal("100.00"),
            line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
        )
        return order, item

    def _customer_return(self, qty=Decimal("2.0000")):
        order, sales_item = self._sales_order()
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["status"])
        sales_item.shipped_qty = sales_item.order_qty
        sales_item.line_status = SalesOrderItem.LineStatus.SHIPPED
        sales_item.save(update_fields=["shipped_qty", "line_status"])
        customer_return = CustomerReturn.objects.create(
            return_no="RT001",
            customer=self.customer,
            sales_order=order,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.CONFIRMED,
            return_amount=(qty * Decimal("10.0000")).quantize(Decimal("0.01")),
            remark="客户退货",
        )
        CustomerReturnItem.objects.create(
            customer_return=customer_return,
            sales_order_item=sales_item,
            material=self.finished,
            return_qty=qty,
            unit_price=Decimal("10.0000"),
            return_amount=(qty * Decimal("10.0000")).quantize(Decimal("0.01")),
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            return_reason="客户退回",
        )
        return customer_return

    def _batch(self, material, qty):
        batch = InventoryBatch.objects.create(
            batch_no=f"B{material.material_code}{qty}",
            material=material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=qty,
            remaining_qty=qty,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.update_or_create(
            material=material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            defaults={"qty": qty},
        )
        return batch

    def _sample_return(self, qty=Decimal("2.0000")):
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
            is_overdue=True,
            overdue_days=3,
            overdue_status=SampleLoan.OverdueStatus.OVERDUE,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=qty,
            line_status=SampleLoanItem.LineStatus.OUT,
        )
        sample_return = SampleLoanReturn.objects.create(
            sample_return_no="SR001",
            sample_loan=loan,
            customer=self.customer,
            return_date=timezone.localdate(),
            status=SampleLoanReturn.Status.PENDING_CONFIRM,
        )
        SampleLoanReturnItem.objects.create(
            sample_return=sample_return,
            sample_loan=loan,
            sample_loan_item=loan_item,
            material=self.finished,
            return_qty=qty,
            location=self.location,
            sample_condition=SampleLoanReturnItem.SampleCondition.GOOD,
        )
        return loan, loan_item, sample_return

    def test_confirm_sales_order_marks_line_sufficient_when_finished_stock_exists(self):
        self._batch(self.finished, Decimal("10"))
        order, item = self._sales_order()

        result = confirm_sales_order(order.id, self.user.id)

        self.assertTrue(result.success)
        item.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.CONFIRMED)
        self.assertEqual(order.approved_by_id, self.user.id)
        self.assertIsNotNone(order.approved_at)
        self.assertEqual(item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SUFFICIENT)
        self.assertEqual(item.locked_bom, self.bom)
        self.assertFalse(ShortageAlert.objects.exists())

    def test_confirm_sales_order_creates_shortage_alert_for_required_component(self):
        self._batch(self.raw, Decimal("5"))
        order, item = self._sales_order()

        result = confirm_sales_order(order.id, self.user.id)

        self.assertTrue(result.success)
        item.refresh_from_db()
        self.assertEqual(item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SHORTAGE)
        alert = ShortageAlert.objects.get()
        self.assertEqual(alert.material, self.raw)
        self.assertEqual(alert.required_qty, Decimal("20.0000"))
        self.assertEqual(alert.available_qty, Decimal("5.0000"))
        self.assertEqual(alert.shortage_qty, Decimal("15.0000"))
        self.assertTrue(alert.is_required)
        self.assertTrue(PendingEvent.objects.filter(event_type="shortage_created").exists())

    def test_confirm_sales_order_uses_bom_base_qty_and_loss_rate_for_shortage(self):
        self.bom.base_qty = Decimal("2.0000")
        self.bom.save(update_fields=["base_qty"])
        bom_item = self.bom.items.get()
        bom_item.usage_qty = Decimal("2.000000")
        bom_item.loss_rate = Decimal("0.100000")
        bom_item.save(update_fields=["usage_qty", "loss_rate"])
        self._batch(self.raw, Decimal("5"))
        order, item = self._sales_order(qty=Decimal("10"))

        result = confirm_sales_order(order.id, self.user.id)

        self.assertTrue(result.success)
        alert = ShortageAlert.objects.get()
        self.assertEqual(alert.required_qty, Decimal("11.0000"))
        self.assertEqual(alert.available_qty, Decimal("5.0000"))
        self.assertEqual(alert.shortage_qty, Decimal("6.0000"))

    def test_confirm_sales_order_locks_default_enabled_bom(self):
        other_bom = Bom.objects.create(
            bom_no="BOM002",
            finished_material=self.finished,
            bom_version="V2",
            status=Bom.BomStatus.ENABLED,
            is_default=True,
            enabled_at=timezone.now(),
        )
        self.bom.is_default = False
        self.bom.save(update_fields=["is_default"])
        BomItem.objects.create(
            bom=other_bom,
            line_no=1,
            component_material=self.raw,
            usage_qty=Decimal("1.000000"),
            usage_unit="pcs",
            loss_rate=Decimal("0"),
            is_required=True,
        )
        self._batch(self.raw, Decimal("10"))
        order, item = self._sales_order()

        result = confirm_sales_order(order.id, self.user.id)

        self.assertTrue(result.success)
        item.refresh_from_db()
        self.assertEqual(item.locked_bom, other_bom)
        self.assertEqual(item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.KITTED)

    def test_create_purchase_request_from_shortages_merges_by_material(self):
        self._batch(self.raw, Decimal("5"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        alert = ShortageAlert.objects.get()

        result = create_purchase_request_from_shortages(
            [alert.id],
            operator_id=self.user.id,
            merge_mode="by_material",
            idempotency_key="idem-1",
        )

        self.assertTrue(result.success)
        request = PurchaseRequest.objects.get()
        request_item = PurchaseRequestItem.objects.get()
        alert.refresh_from_db()
        self.assertEqual(request.source_type, PurchaseRequest.SourceType.SHORTAGE)
        self.assertEqual(request_item.material, self.raw)
        self.assertEqual(request_item.request_qty, Decimal("15.0000"))
        self.assertEqual(alert.status, ShortageAlert.Status.PURCHASE_REQUESTED)
        self.assertEqual(alert.purchase_request, request)
        self.assertEqual(alert.purchase_request_id, request.id)

    def test_confirm_sales_shipment_deducts_finished_inventory_and_marks_order_shipped(self):
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS001",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
        )

        result = confirm_sales_shipment(shipment.id, self.user.id, "ship-1")

        self.assertTrue(result.success)
        batch.refresh_from_db()
        item.refresh_from_db()
        order.refresh_from_db()
        shipment.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        self.assertEqual(batch.remaining_qty, Decimal("0.0000"))
        self.assertEqual(batch.batch_status, InventoryBatch.BatchStatus.USED_UP)
        self.assertEqual(inventory.qty, Decimal("0.0000"))
        self.assertEqual(item.shipped_qty, Decimal("10.0000"))
        self.assertEqual(item.line_status, SalesOrderItem.LineStatus.SHIPPED)
        self.assertEqual(order.status, SalesOrder.Status.SHIPPED)
        self.assertEqual(shipment.status, SalesShipment.Status.SHIPPED)
        self.assertEqual(InventoryTransaction.objects.get().transaction_type, InventoryTransaction.TransactionType.SALES_OUT)
        self.assertTrue(PendingEvent.objects.filter(event_type="sales_shipped").exists())

    def test_confirm_sample_return_increases_inventory_and_closes_overdue(self):
        loan, loan_item, sample_return = self._sample_return()

        result = confirm_sample_return(sample_return.id, self.user.id, "sample-return-1")

        self.assertTrue(result.success)
        loan.refresh_from_db()
        loan_item.refresh_from_db()
        sample_return.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        transaction = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.SAMPLE_RETURN_IN)
        self.assertEqual(sample_return.status, SampleLoanReturn.Status.RECEIVED)
        self.assertEqual(loan.status, SampleLoan.Status.RETURNED)
        self.assertEqual(loan.overdue_status, SampleLoan.OverdueStatus.CLOSED)
        self.assertFalse(loan.is_overdue)
        self.assertEqual(loan_item.returned_qty, Decimal("2.0000"))
        self.assertEqual(loan_item.line_status, SampleLoanItem.LineStatus.RETURNED)
        self.assertEqual(inventory.qty, Decimal("2.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("2.0000"))
        self.assertTrue(PendingEvent.objects.filter(event_type="sample_returned").exists())

    def test_confirm_sample_loan_out_deducts_inventory_and_marks_out(self):
        batch = self._batch(self.finished, Decimal("5.0000"))
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.PENDING_APPROVAL,
        )
        SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("2.0000"),
            batch=batch,
            location=self.location,
        )

        result = confirm_sample_loan_out(loan.id, self.user.id, "sample-out-1")

        self.assertTrue(result.success)
        loan.refresh_from_db()
        batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        transaction = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.SAMPLE_OUT)
        self.assertEqual(loan.status, SampleLoan.Status.OUT)
        self.assertEqual(batch.remaining_qty, Decimal("3.0000"))
        self.assertEqual(inventory.qty, Decimal("3.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("-2.0000"))
        self.assertTrue(PendingEvent.objects.filter(event_type="sample_out").exists())

    def test_confirm_customer_return_receipt_increases_inventory(self):
        customer_return = self._customer_return()

        result = confirm_customer_return_receipt(customer_return.id, self.user.id, "customer-return-1")

        self.assertTrue(result.success)
        customer_return.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        batch = InventoryBatch.objects.get(material=self.finished, location=self.location)
        transaction = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.CUSTOMER_RETURN_IN)
        self.assertEqual(customer_return.status, CustomerReturn.Status.RECEIVED)
        self.assertEqual(inventory.qty, Decimal("2.0000"))
        self.assertEqual(batch.remaining_qty, Decimal("2.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("2.0000"))
        self.assertTrue(PendingEvent.objects.filter(event_type="customer_return_in").exists())

    def test_confirm_sample_loan_out_requires_batch_and_location(self):
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.PENDING_APPROVAL,
        )
        SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("2.0000"),
        )

        result = confirm_sample_loan_out(loan.id, self.user.id, "sample-out-no-batch")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "STATE_INVALID_TRANSITION")

    def test_convert_sample_loan_item_to_sales_order_creates_pending_order(self):
        batch = self._batch(self.finished, Decimal("5.0000"))
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("5.0000"),
            returned_qty=Decimal("1.0000"),
            sold_qty=Decimal("0"),
            batch=batch,
            location=self.location,
            line_status=SampleLoanItem.LineStatus.OUT,
        )

        result = convert_sample_loan_item_to_sales_order(
            loan_item.id,
            Decimal("2.0000"),
            Decimal("12.5000"),
            self.user.id,
            "sample-to-sales-1",
        )

        self.assertTrue(result.success)
        order = SalesOrder.objects.get(id=result.data["sales_order_id"])
        order_item = order.items.get()
        loan.refresh_from_db()
        loan_item.refresh_from_db()
        transaction = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.SAMPLE_TO_SALES)
        self.assertEqual(order.status, SalesOrder.Status.PENDING_APPROVAL)
        self.assertEqual(order.customer, self.customer)
        self.assertEqual(order.total_amount, Decimal("25.00"))
        self.assertEqual(order_item.customer_product, self.customer_product)
        self.assertEqual(order_item.order_qty, Decimal("2.0000"))
        self.assertEqual(order_item.shipped_qty, Decimal("2.0000"))
        self.assertEqual(loan_item.sold_qty, Decimal("2.0000"))
        self.assertEqual(loan.status, SampleLoan.Status.PART_SOLD)
        self.assertEqual(transaction.qty_delta, Decimal("0.0000"))

    def test_convert_sample_loan_item_to_sales_order_rejects_over_quantity(self):
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("2.0000"),
            returned_qty=Decimal("1.0000"),
            line_status=SampleLoanItem.LineStatus.OUT,
        )

        result = convert_sample_loan_item_to_sales_order(
            loan_item.id,
            Decimal("2.0000"),
            Decimal("12.5000"),
            self.user.id,
            "sample-to-sales-over",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "STATE_INVALID_TRANSITION")


class SalesOrderViewTests(SalesServiceTests):
    def setUp(self):
        super().setUp()
        self._grant_permission(PermissionCode.SALES_VIEW)

    def test_sales_order_list_filters_to_owned_customer_without_view_all(self):
        other_user = get_user_model().objects.create_user(username="other-sales", password="x")
        owned_customer = Customer.objects.create(customer_no="C-OWN", customer_name="我的客户", sales_owner=self.user)
        other_customer = Customer.objects.create(customer_no="C-OTHER", customer_name="别人的客户", sales_owner=other_user)
        SalesOrder.objects.create(
            sales_order_no="SO-OWN",
            customer=owned_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("10.00"),
        )
        SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("20.00"),
        )
        self.client.force_login(self.user)

        response = self.client.get("/sales/orders/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SO-OWN")
        self.assertNotContains(response, "SO-OTHER")

    def test_sales_order_list_view_all_permission_shows_all_orders(self):
        other_user = get_user_model().objects.create_user(username="other-sales", password="x")
        owned_customer = Customer.objects.create(customer_no="C-OWN", customer_name="我的客户", sales_owner=self.user)
        other_customer = Customer.objects.create(customer_no="C-OTHER", customer_name="别人的客户", sales_owner=other_user)
        SalesOrder.objects.create(
            sales_order_no="SO-OWN",
            customer=owned_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("10.00"),
        )
        SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("20.00"),
        )
        self._grant_permission(PermissionCode.SALES_VIEW_ALL)
        self.client.force_login(self.user)

        response = self.client.get("/sales/orders/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SO-OWN")
        self.assertContains(response, "SO-OTHER")

    def test_sales_order_detail_denies_unowned_order(self):
        other_user = get_user_model().objects.create_user(username="other-sales", password="x")
        other_customer = Customer.objects.create(customer_no="C-OTHER", customer_name="别人的客户", sales_owner=other_user)
        order = SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("20.00"),
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertEqual(response.status_code, 404)

    def test_create_sales_order_draft_from_form(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)

        response = self.client.post(
            "/sales/orders/new/",
            {
                "customer": self.customer.id,
                "customer_contract_no": "HT-FORM",
                "settlement_method": "月结",
                "order_date": timezone.localdate().isoformat(),
                "delivery_date": "",
                "remark": "页面创建",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-customer_product": self.customer_product.id,
                "items-0-order_qty": "3",
                "items-0-unit_price": "12.5",
                "items-1-customer_product": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-2-customer_product": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "action": "draft",
            },
        )

        order = SalesOrder.objects.order_by("-id").first()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/orders/{order.id}/")
        self.assertEqual(order.status, SalesOrder.Status.DRAFT)
        self.assertEqual(order.customer_contract_no, "HT-FORM")
        self.assertEqual(order.settlement_method, "月结")
        self.assertEqual(order.total_amount, Decimal("37.50"))
        self.assertEqual(order.items.get().line_status, SalesOrderItem.LineStatus.DRAFT)

    def test_sales_order_form_uses_human_readable_address_and_product_options(self):
        CustomerAddress.objects.create(
            customer=self.customer,
            address_type=CustomerAddress.AddressType.SHIPPING,
            receiver_name="王五",
            receiver_phone_encrypted="13900000000",
            address_encrypted="深圳市测试路 1 号",
            is_default=True,
        )
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)

        response = self.client.get("/sales/orders/new/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "测试客户 - 收货地址 - 王五 - 深圳市测试路 1 号（默认）")
        self.assertContains(response, "测试客户 - CP001 - 客户产品 1 - 成品:FG001 成品 1")
        self.assertNotContains(response, "CustomerAddress object")
        self.assertNotContains(response, "CustomerProduct object")

    def test_create_sales_order_submit_requires_sales_process_permission(self):
        self.client.force_login(self.user)

        get_response = self.client.get("/sales/orders/new/")
        post_response = self.client.post(
            "/sales/orders/new/",
            {
                "customer": self.customer.id,
                "order_date": timezone.localdate().isoformat(),
                "delivery_date": "",
                "remark": "无权限提交",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-customer_product": self.customer_product.id,
                "items-0-order_qty": "3",
                "items-0-unit_price": "12.5",
                "items-1-customer_product": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-2-customer_product": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "action": "submit",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertNotContains(get_response, "保存并提交审核")
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(SalesOrder.objects.exists())

    def test_sales_order_amounts_mask_without_finance_permission(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        item.unit_price = Decimal("7.1234")
        item.line_amount = Decimal("71.23")
        item.save(update_fields=["unit_price", "line_amount"])
        order.total_amount = Decimal("71.23")
        order.save(update_fields=["total_amount"])

        list_response = self.client.get("/sales/orders/")
        detail_response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertContains(list_response, "******")
        self.assertNotContains(list_response, "71.23")
        self.assertContains(detail_response, "******")
        self.assertNotContains(detail_response, "7.1234")

    def test_sales_order_amounts_visible_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        item.unit_price = Decimal("7.1234")
        item.line_amount = Decimal("71.23")
        item.save(update_fields=["unit_price", "line_amount"])
        order.total_amount = Decimal("71.23")
        order.save(update_fields=["total_amount"])

        response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertContains(response, "71.23")
        self.assertContains(response, "7.1234")

    def test_sales_order_print_masks_amount_and_records_log(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        item.unit_price = Decimal("7.1234")
        item.line_amount = Decimal("71.23")
        item.save(update_fields=["unit_price", "line_amount"])
        order.total_amount = Decimal("71.23")
        order.save(update_fields=["total_amount"])

        detail_response = self.client.get(f"/sales/orders/{order.id}/")
        print_response = self.client.get(f"/sales/orders/{order.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "销售订单")
        self.assertContains(print_response, order.sales_order_no)
        self.assertContains(print_response, "******")
        self.assertNotContains(print_response, "7.1234")
        print_log = PrintLog.objects.get(source_doc_type="sales_order", source_doc_id=order.id)
        self.assertEqual(print_log.template_type, "sales_order")
        self.assertEqual(print_log.source_doc_no, order.sales_order_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_sales_order_print_shows_amount_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        item.unit_price = Decimal("7.1234")
        item.line_amount = Decimal("71.23")
        item.save(update_fields=["unit_price", "line_amount"])
        order.total_amount = Decimal("71.23")
        order.save(update_fields=["total_amount"])

        response = self.client.get(f"/sales/orders/{order.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "71.23")
        self.assertContains(response, "7.1234")

    def test_sales_order_print_denies_unowned_order(self):
        other_user = get_user_model().objects.create_user(username="print-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-PRINT-OTHER", customer_name="别人的客户", sales_owner=other_user)
        order = SalesOrder.objects.create(
            sales_order_no="SO-PRINT-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("20.00"),
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/sales/orders/{order.id}/print/")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PrintLog.objects.exists())

    def test_sales_order_export_masks_amount_and_respects_sales_scope(self):
        other_user = get_user_model().objects.create_user(username="export-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-EXPORT-OTHER", customer_name="别人的客户", sales_owner=other_user)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.sales_order_no = "SO-EXPORT-MINE"
        order.total_amount = Decimal("71.23")
        order.save(update_fields=["sales_order_no", "total_amount"])
        SalesOrder.objects.create(
            sales_order_no="SO-EXPORT-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("88.00"),
        )

        list_response = self.client.get("/sales/orders/")
        response = self.client.get("/sales/orders/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("订单号,客户,订单日期,交期,状态,金额", content)
        self.assertIn("SO-EXPORT-MINE", content)
        self.assertNotIn("SO-EXPORT-OTHER", content)
        self.assertIn("******", content)
        self.assertNotIn("71.23", content)
        export_log = ExportLog.objects.get(module="sales_orders")
        self.assertEqual(export_log.row_count, 1)

    def test_sales_order_export_shows_amount_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.total_amount = Decimal("71.23")
        order.save(update_fields=["total_amount"])

        response = self.client.get("/sales/orders/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("71.23", content)

    def test_sales_order_list_filter_and_export_share_query(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.sales_order_no = "SO-FILTER-KEEP"
        order.status = SalesOrder.Status.CONFIRMED
        order.save(update_fields=["sales_order_no", "status"])
        SalesOrder.objects.create(
            sales_order_no="SO-FILTER-HIDE",
            customer=self.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("88.00"),
        )

        list_response = self.client.get("/sales/orders/?q=KEEP&status=confirmed")
        export_response = self.client.get("/sales/orders/export/?q=KEEP&status=confirmed")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "SO-FILTER-KEEP")
        self.assertNotContains(list_response, "SO-FILTER-HIDE")
        self.assertContains(list_response, "/sales/orders/export/?q=KEEP&amp;status=confirmed")
        self.assertIn("SO-FILTER-KEEP", content)
        self.assertNotIn("SO-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="sales_orders")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "confirmed")

    def test_sales_order_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/sales/orders/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("销售订单号,客户编号,客户地址 ID,订单日期", content)
        self.assertIn("SO-INIT-001", content)

    def test_sales_order_import_creates_draft_order_with_multiple_lines(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        second_finished = Material.objects.create(
            material_code="FG002",
            material_name="成品 2",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        second_product = CustomerProduct.objects.create(
            customer=self.customer,
            customer_product_no="CP002",
            customer_product_name="客户产品 2",
            finished_material=second_finished,
            default_sale_price=Decimal("6.0000"),
        )
        upload = SimpleUploadedFile(
            "sales_orders.csv",
            (
                "销售订单号,客户编号,客户地址 ID,订单日期,交期,客户产品编号,订单数量,单价,备注\n"
                "SO-IMP-001,C001,,2026-06-10,2026-06-20,CP001,3,12.5000,导入订单\n"
                "SO-IMP-001,C001,,2026-06-10,2026-06-20,CP002,2,6.0000,\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sales/orders/")
        order = SalesOrder.objects.get(sales_order_no="SO-IMP-001")
        self.assertEqual(order.status, SalesOrder.Status.DRAFT)
        self.assertEqual(order.customer, self.customer)
        self.assertEqual(order.total_amount, Decimal("49.50"))
        self.assertEqual(order.created_by, self.user)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(list(order.items.order_by("line_no").values_list("customer_product__customer_product_no", flat=True)), ["CP001", "CP002"])
        self.assertEqual(order.items.get(customer_product=second_product).line_amount, Decimal("12.00"))
        job = ImportJob.objects.get(template_type="sales_orders")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_sales_order_import_without_amount_permission_uses_default_price(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        self.customer_product.default_sale_price = Decimal("8.5000")
        self.customer_product.save(update_fields=["default_sale_price"])
        upload = SimpleUploadedFile(
            "sales_orders.csv",
            (
                "销售订单号,客户编号,客户地址 ID,订单日期,交期,客户产品编号,订单数量,单价,备注\n"
                "SO-IMP-NO-AMOUNT,C001,,2026-06-10,,CP001,4,99.9999,无金额权限\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        order = SalesOrder.objects.get(sales_order_no="SO-IMP-NO-AMOUNT")
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("8.5000"))
        self.assertEqual(order.total_amount, Decimal("34.00"))

    def test_sales_order_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "sales_orders.csv",
            (
                "销售订单号,客户编号,客户地址 ID,订单日期,交期,客户产品编号,订单数量,单价,备注\n"
                "SO-BAD,C001,,bad-date,2026-06-01,CP001,-1,-5,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "订单日期格式错误")
        self.assertContains(response, "订单数量必须大于 0")
        job = ImportJob.objects.get(template_type="sales_orders")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)
        self.assertFalse(SalesOrder.objects.filter(sales_order_no="SO-BAD").exists())

    def test_sales_order_import_requires_sales_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/sales/orders/import-template/")
        import_response = self.client.get("/sales/orders/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_sample_loan_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/sales/sample-loans/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("借样单号,客户编号,借样日期,预计归还日期", content)
        self.assertIn("SL-INIT-001", content)

    def test_sample_loan_import_creates_pending_loan_with_multiple_lines(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        second_finished = Material.objects.create(
            material_code="FG002",
            material_name="成品 2",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        location = WarehouseLocation.objects.create(location_code="SL-A01", location_name="样品库位")
        batch = InventoryBatch.objects.create(
            batch_no="SL-B001",
            material=self.finished,
            location=location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("10.0000"),
            remaining_qty=Decimal("10.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        upload = SimpleUploadedFile(
            "sample_loans.csv",
            (
                "借样单号,客户编号,借样日期,预计归还日期,物料编码,借样数量,批次号,库位编码,明细预计归还日期,备注\n"
                "SL-IMP-001,C001,2026-06-10,2026-06-20,FG001,2,SL-B001,SL-A01,2026-06-20,导入借样\n"
                "SL-IMP-001,C001,2026-06-10,2026-06-20,FG002,1,,,2026-06-22,\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/sample-loans/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sales/sample-loans/")
        loan = SampleLoan.objects.get(sample_loan_no="SL-IMP-001")
        self.assertEqual(loan.status, SampleLoan.Status.PENDING_APPROVAL)
        self.assertEqual(loan.customer, self.customer)
        self.assertEqual(loan.created_by, self.user)
        self.assertEqual(loan.items.count(), 2)
        first_item = loan.items.get(material=self.finished)
        second_item = loan.items.get(material=second_finished)
        self.assertEqual(first_item.loan_qty, Decimal("2"))
        self.assertEqual(first_item.batch, batch)
        self.assertEqual(first_item.location, location)
        self.assertEqual(second_item.expected_return_date.isoformat(), "2026-06-22")
        batch.refresh_from_db()
        self.assertEqual(batch.remaining_qty, Decimal("10.0000"))
        job = ImportJob.objects.get(template_type="sample_loans")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_sample_loan_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "sample_loans.csv",
            (
                "借样单号,客户编号,借样日期,预计归还日期,物料编码,借样数量,批次号,库位编码,明细预计归还日期,备注\n"
                "SL-BAD,C001,bad-date,2026-06-01,RM-MISSING,-1,B-MISSING,L-MISSING,bad-line,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/sample-loans/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "借出日期格式错误")
        self.assertContains(response, "样品物料不存在、未启用或不是成品")
        self.assertContains(response, "借出数量必须大于 0")
        self.assertContains(response, "批次不存在、未在库或无可用库存")
        self.assertContains(response, "库位不存在或未启用")
        job = ImportJob.objects.get(template_type="sample_loans")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertFalse(SampleLoan.objects.filter(sample_loan_no="SL-BAD").exists())

    def test_sample_loan_import_requires_sales_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/sales/sample-loans/import-template/")
        import_response = self.client.get("/sales/sample-loans/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_customer_return_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/sales/returns/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("客户退货单号,客户编号,销售订单号,退货日期", content)
        self.assertIn("RT-INIT-001", content)

    def test_customer_return_import_creates_draft_return_with_source_order_line(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.sales_order_no = "SO-RETURN-IMPORT"
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["sales_order_no", "status"])
        item.shipped_qty = Decimal("5.0000")
        item.line_status = SalesOrderItem.LineStatus.SHIPPED
        item.save(update_fields=["shipped_qty", "line_status"])
        location = WarehouseLocation.objects.create(location_code="RT-A01", location_name="退货库位")
        upload = SimpleUploadedFile(
            "customer_returns.csv",
            (
                "客户退货单号,客户编号,销售订单号,退货日期,销售订单行号,物料编码,退货数量,单价,库位编码,库存类型,退货原因,备注\n"
                "RT-IMP-001,C001,SO-RETURN-IMPORT,2026-06-10,1,FG001,2,9.5000,RT-A01,available,客户退回,导入退货\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/returns/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sales/returns/")
        customer_return = CustomerReturn.objects.get(return_no="RT-IMP-001")
        return_item = customer_return.items.get()
        self.assertEqual(customer_return.status, CustomerReturn.Status.DRAFT)
        self.assertEqual(customer_return.customer, self.customer)
        self.assertEqual(customer_return.sales_order, order)
        self.assertEqual(customer_return.return_amount, Decimal("19.00"))
        self.assertEqual(return_item.sales_order_item, item)
        self.assertEqual(return_item.material, self.finished)
        self.assertEqual(return_item.location, location)
        self.assertEqual(return_item.unit_price, Decimal("9.5000"))
        self.assertFalse(Inventory.objects.exists())
        job = ImportJob.objects.get(template_type="customer_returns")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_customer_return_import_without_amount_permission_uses_source_price(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.sales_order_no = "SO-RETURN-NO-AMOUNT"
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["sales_order_no", "status"])
        item.shipped_qty = Decimal("4.0000")
        item.unit_price = Decimal("7.2500")
        item.line_status = SalesOrderItem.LineStatus.SHIPPED
        item.save(update_fields=["shipped_qty", "unit_price", "line_status"])
        upload = SimpleUploadedFile(
            "customer_returns.csv",
            (
                "客户退货单号,客户编号,销售订单号,退货日期,销售订单行号,物料编码,退货数量,单价,库位编码,库存类型,退货原因,备注\n"
                "RT-IMP-NO-AMOUNT,C001,SO-RETURN-NO-AMOUNT,2026-06-10,1,FG001,2,99.9900,,available,客户退回,无金额权限\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/returns/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        customer_return = CustomerReturn.objects.get(return_no="RT-IMP-NO-AMOUNT")
        return_item = customer_return.items.get()
        self.assertEqual(return_item.unit_price, Decimal("7.2500"))
        self.assertEqual(customer_return.return_amount, Decimal("14.50"))

    def test_customer_return_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "customer_returns.csv",
            (
                "客户退货单号,客户编号,销售订单号,退货日期,销售订单行号,物料编码,退货数量,单价,库位编码,库存类型,退货原因,备注\n"
                "RT-BAD,C001,SO-MISSING,bad-date,1,RM-MISSING,-1,-5,L-MISSING,bad-type,错误,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/returns/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "来源销售订单不存在或未发货完成")
        self.assertContains(response, "退货日期格式错误")
        self.assertContains(response, "退货数量必须大于 0")
        self.assertContains(response, "退货单价不能小于 0")
        self.assertContains(response, "库位不存在或未启用")
        self.assertContains(response, "库存类型不在允许范围内")
        job = ImportJob.objects.get(template_type="customer_returns")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertFalse(CustomerReturn.objects.filter(return_no="RT-BAD").exists())

    def test_customer_return_import_requires_sales_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/sales/returns/import-template/")
        import_response = self.client.get("/sales/returns/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_sales_shipment_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/sales/shipments/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("销售出库单号,销售订单号,出库日期", content)
        self.assertIn("SS-INIT-001", content)

    def test_sales_shipment_import_creates_pending_shipment_without_deducting_inventory(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        batch = self._batch(self.finished, Decimal("10.0000"))
        batch.cost_price = Decimal("3.123456")
        batch.save(update_fields=["cost_price"])
        order, item = self._sales_order()
        order.sales_order_no = "SO-SHIP-IMPORT"
        order.save(update_fields=["sales_order_no"])
        confirm_sales_order(order.id, self.user.id)
        upload = SimpleUploadedFile(
            "sales_shipments.csv",
            (
                "销售出库单号,销售订单号,出库日期,销售订单行号,物料编码,出库数量,批次号,库位编码,备注\n"
                f"SS-IMP-001,SO-SHIP-IMPORT,2026-06-10,1,FG001,4,{batch.batch_no},{self.location.location_code},导入出库\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/shipments/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sales/shipments/")
        shipment = SalesShipment.objects.get(shipment_no="SS-IMP-001")
        shipment_item = shipment.items.get()
        self.assertEqual(shipment.status, SalesShipment.Status.PENDING_CONFIRM)
        self.assertEqual(shipment.sales_order, order)
        self.assertEqual(shipment.customer, self.customer)
        self.assertEqual(shipment.created_by, self.user)
        self.assertEqual(shipment_item.sales_order_item, item)
        self.assertEqual(shipment_item.shipment_qty, Decimal("4"))
        self.assertEqual(shipment_item.batch, batch)
        self.assertEqual(shipment_item.location, self.location)
        self.assertEqual(shipment_item.cost_price, Decimal("3.123456"))
        batch.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(batch.remaining_qty, Decimal("10.0000"))
        self.assertEqual(item.shipped_qty, Decimal("0"))
        job = ImportJob.objects.get(template_type="sales_shipments")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_sales_shipment_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.sales_order_no = "SO-SHIP-BAD"
        order.status = SalesOrder.Status.CONFIRMED
        order.save(update_fields=["sales_order_no", "status"])
        item.line_status = SalesOrderItem.LineStatus.CONFIRMED
        item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.SUFFICIENT
        item.save(update_fields=["line_status", "inventory_check_status"])
        upload = SimpleUploadedFile(
            "sales_shipments.csv",
            (
                "销售出库单号,销售订单号,出库日期,销售订单行号,物料编码,出库数量,批次号,库位编码,备注\n"
                "SS-BAD,SO-SHIP-BAD,bad-date,1,FG001,20,B-MISSING,L-MISSING,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/shipments/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "出库日期格式错误")
        self.assertContains(response, "出库数量不能超过销售订单行未发货数量")
        self.assertContains(response, "批次不存在、不是可用库存或未在库")
        self.assertContains(response, "库位不存在或未启用")
        job = ImportJob.objects.get(template_type="sales_shipments")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertFalse(SalesShipment.objects.filter(shipment_no="SS-BAD").exists())

    def test_sales_shipment_import_requires_sales_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/sales/shipments/import-template/")
        import_response = self.client.get("/sales/shipments/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_sales_shipment_import_respects_sales_data_scope(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        other_user = get_user_model().objects.create_user(username="ship-import-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-SHIP-IMPORT-OTHER", customer_name="别人的客户", sales_owner=other_user)
        other_product = CustomerProduct.objects.create(
            customer=other_customer,
            customer_product_no="CP-SHIP-IMPORT-OTHER",
            customer_product_name="别人的产品",
            finished_material=self.finished,
            default_sale_price=Decimal("10.0000"),
        )
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-SHIP-SCOPE",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.CONFIRMED,
            total_amount=Decimal("10.00"),
            created_by=other_user,
        )
        SalesOrderItem.objects.create(
            sales_order=other_order,
            line_no=1,
            customer_product=other_product,
            finished_material=self.finished,
            order_qty=Decimal("1.0000"),
            unit_price=Decimal("10.0000"),
            line_amount=Decimal("10.00"),
            line_status=SalesOrderItem.LineStatus.CONFIRMED,
            inventory_check_status=SalesOrderItem.InventoryCheckStatus.SUFFICIENT,
        )
        batch = self._batch(self.finished, Decimal("3.0000"))
        upload = SimpleUploadedFile(
            "sales_shipments.csv",
            (
                "销售出库单号,销售订单号,出库日期,销售订单行号,物料编码,出库数量,批次号,库位编码,备注\n"
                f"SS-SCOPE,SO-SHIP-SCOPE,2026-06-10,1,FG001,1,{batch.batch_no},{self.location.location_code},越权导入\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/sales/shipments/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "来源销售订单不在当前用户数据范围内")
        self.assertFalse(SalesShipment.objects.filter(shipment_no="SS-SCOPE").exists())

    def test_shortage_alert_export_respects_sales_scope_and_logs(self):
        other_user = get_user_model().objects.create_user(username="shortage-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-SHORT-OTHER", customer_name="隐藏客户", sales_owner=other_user)
        other_product = CustomerProduct.objects.create(
            customer=other_customer,
            customer_product_no="CP-SHORT-OTHER",
            customer_product_name="隐藏产品",
            finished_material=self.finished,
        )
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.sales_order_no = "SO-SHORT-MINE"
        order.save(update_fields=["sales_order_no"])
        ShortageAlert.objects.create(
            shortage_no="SA-EXPORT-MINE",
            sales_order=order,
            sales_order_item=item,
            material=self.raw,
            required_qty=Decimal("20.0000"),
            available_qty=Decimal("5.0000"),
            shortage_qty=Decimal("15.0000"),
        )
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-SHORT-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.CONFIRMED,
            total_amount=Decimal("10.00"),
        )
        other_item = SalesOrderItem.objects.create(
            sales_order=other_order,
            line_no=1,
            customer_product=other_product,
            finished_material=self.finished,
            order_qty=Decimal("1.0000"),
            unit_price=Decimal("10.0000"),
            line_amount=Decimal("10.00"),
        )
        ShortageAlert.objects.create(
            shortage_no="SA-EXPORT-OTHER",
            sales_order=other_order,
            sales_order_item=other_item,
            material=self.raw,
            required_qty=Decimal("10.0000"),
            available_qty=Decimal("0.0000"),
            shortage_qty=Decimal("10.0000"),
        )

        list_response = self.client.get("/sales/shortages/")
        response = self.client.get("/sales/shortages/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("欠料号,销售订单,物料,需求数量,可用数量,欠料数量,状态", content)
        self.assertIn("SA-EXPORT-MINE", content)
        self.assertNotIn("SA-EXPORT-OTHER", content)
        export_log = ExportLog.objects.get(module="shortage_alerts")
        self.assertEqual(export_log.row_count, 1)

    def test_shortage_create_purchase_request_action_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        list_response = self.client.get("/sales/shortages/")
        get_response = self.client.get("/sales/shortages/create-purchase-request/")
        post_response = self.client.post("/sales/shortages/create-purchase-request/", {"shortage_ids": [], "merge_mode": "by_material"})

        self.assertNotContains(list_response, "/sales/shortages/create-purchase-request/")
        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)

    def test_create_sales_order_without_amount_permission_uses_default_price(self):
        self.customer_product.default_sale_price = Decimal("8.5000")
        self.customer_product.save(update_fields=["default_sale_price"])
        self.client.force_login(self.user)

        response = self.client.post(
            "/sales/orders/new/",
            {
                "customer": self.customer.id,
                "order_date": timezone.localdate().isoformat(),
                "delivery_date": "",
                "remark": "无金额权限录单",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-customer_product": self.customer_product.id,
                "items-0-order_qty": "2",
                "items-0-unit_price": "",
                "items-1-customer_product": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-2-customer_product": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "action": "draft",
            },
        )

        order = SalesOrder.objects.order_by("-id").first()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order.total_amount, Decimal("17.00"))
        self.assertEqual(order.items.get().unit_price, Decimal("8.5000"))

    def test_sales_order_detail_renders_attachment_panel(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()

        response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertContains(response, "附件")
        self.assertContains(response, "上传附件")
        self.assertContains(response, f'name="source_doc_type" value="sales_order"', html=False)
        self.assertContains(response, f'name="source_doc_id" value="{order.id}"', html=False)
        self.assertContains(response, f'name="source_doc_no" value="{order.sales_order_no}"', html=False)

    def test_sales_order_detail_uploads_attachment_with_hidden_source(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()

        with TemporaryDirectory() as temp_dir, override_settings(MEDIA_ROOT=temp_dir):
            response = self.client.post(
                "/files/upload/",
                {
                    "source_doc_type": "sales_order",
                    "source_doc_id": str(order.id),
                    "source_doc_no": order.sales_order_no,
                    "file": SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf"),
                },
            )

        attachment = Attachment.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/files/{attachment.id}/")
        self.assertEqual(attachment.source_doc_type, "sales_order")
        self.assertEqual(attachment.source_doc_id, order.id)
        self.assertEqual(attachment.source_doc_no, order.sales_order_no)

    def test_sales_order_attachment_panel_hides_sensitive_download_without_permission(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        attachment = Attachment.objects.create(
            attachment_no="ATT-SO-SENSITIVE",
            source_doc_type="sales_order",
            source_doc_id=order.id,
            source_doc_no=order.sales_order_no,
            original_filename="secret.pdf",
            stored_filename="secret.pdf",
            file_path="attachments/secret.pdf",
            file_size=100,
            is_sensitive=True,
            uploaded_by=self.user,
        )

        response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertContains(response, "secret.pdf")
        self.assertContains(response, "无权限")
        self.assertNotContains(response, f"/files/{attachment.id}/download/")

    def test_sales_order_attachment_panel_shows_sensitive_download_with_permission(self):
        self._grant_permission(PermissionCode.ATTACHMENT_VIEW_SENSITIVE)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        attachment = Attachment.objects.create(
            attachment_no="ATT-SO-SENSITIVE",
            source_doc_type="sales_order",
            source_doc_id=order.id,
            source_doc_no=order.sales_order_no,
            original_filename="secret.pdf",
            stored_filename="secret.pdf",
            file_path="attachments/secret.pdf",
            file_size=100,
            is_sensitive=True,
            uploaded_by=self.user,
        )

        response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertContains(response, "secret.pdf")
        self.assertContains(response, f"/files/{attachment.id}/download/")

    def test_create_sales_order_submit_and_confirm_from_detail(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)

        create_response = self.client.post(
            "/sales/orders/new/",
            {
                "customer": self.customer.id,
                "order_date": timezone.localdate().isoformat(),
                "delivery_date": "",
                "remark": "",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-customer_product": self.customer_product.id,
                "items-0-order_qty": "10",
                "items-0-unit_price": "10",
                "items-1-customer_product": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-2-customer_product": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "action": "submit",
            },
        )
        order = SalesOrder.objects.order_by("-id").first()
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(order.status, SalesOrder.Status.PENDING_APPROVAL)

        confirm_response = self.client.post(f"/sales/orders/{order.id}/confirm/", {"current_password": "x"})

        self.assertEqual(confirm_response.status_code, 302)
        order.refresh_from_db()
        item = order.items.get()
        self.assertEqual(order.status, SalesOrder.Status.CONFIRMED)
        self.assertEqual(item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SHORTAGE)
        self.assertTrue(order.shortage_alerts.exists())
        audit_log = AuditLog.objects.get(action="sales_order_confirm", source_doc_id=order.id)
        self.assertEqual(audit_log.operator, self.user)
        self.assertEqual(audit_log.source_doc_type, "sales_order")

    def test_sales_order_edit_updates_draft_order_and_writes_logs(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.DRAFT
        order.created_by = self.user
        order.save(update_fields=["status", "created_by"])

        response = self.client.post(
            f"/sales/orders/{order.id}/edit/",
            {
                "customer": self.customer.id,
                "customer_address": "",
                "order_date": timezone.localdate().isoformat(),
                "delivery_date": "",
                "remark": "编辑后备注",
                "items-TOTAL_FORMS": "4",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-customer_product": self.customer_product.id,
                "items-0-order_qty": "3",
                "items-0-unit_price": "12.50",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-customer_product": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-customer_product": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "items-2-DELETE": "",
                "items-3-id": "",
                "items-3-customer_product": "",
                "items-3-order_qty": "",
                "items-3-unit_price": "",
                "items-3-DELETE": "",
                "action": "draft",
                "operation_reason": "客户改数量",
            },
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.DRAFT)
        self.assertEqual(order.remark, "编辑后备注")
        self.assertEqual(order.total_amount, Decimal("37.50"))
        self.assertEqual(item.line_no, 1)
        self.assertEqual(order.change_logs.count(), 1)
        audit_log = AuditLog.objects.get(action="sales_order_update", source_doc_id=order.id)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "客户改数量")
        self.assertEqual(order.change_logs.get().change_reason, "客户改数量")

    def test_sales_order_edit_rejects_confirmed_order(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.CONFIRMED
        order.created_by = self.user
        order.save(update_fields=["status", "created_by"])

        response = self.client.get(f"/sales/orders/{order.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/orders/{order.id}/")

    def test_sales_order_voids_pending_order_and_writes_logs(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()
        order.created_by = self.user
        order.save(update_fields=["created_by"])

        response = self.client.post(
            f"/sales/orders/{order.id}/void/",
            {"void_reason": "客户取消", "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.VOIDED)
        change_log = order.change_logs.get()
        self.assertEqual(change_log.change_reason, "客户取消")
        audit_log = AuditLog.objects.get(action="sales_order_void", source_doc_id=order.id)
        self.assertEqual(audit_log.operator, self.user)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "客户取消")

    def test_sales_order_void_requires_reason(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()
        order.created_by = self.user
        order.save(update_fields=["created_by"])

        response = self.client.post(
            f"/sales/orders/{order.id}/void/",
            {"current_password": "x", "void_reason": ""},
            follow=True,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.PENDING_APPROVAL)
        self.assertContains(response, "请填写销售订单作废原因")
        self.assertFalse(AuditLog.objects.filter(action="sales_order_void", source_doc_id=order.id).exists())

    def test_sales_order_detail_renders_actions(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()

        response = self.client.get(f"/sales/orders/{order.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.sales_order_no)
        self.assertContains(response, "审核确认")
        self.assertContains(response, 'name="current_password"', html=False)

    def test_sales_order_confirm_requires_sales_process_permission(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()

        response = self.client.post(f"/sales/orders/{order.id}/confirm/")

        self.assertEqual(response.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.PENDING_APPROVAL)

    def test_sales_order_confirm_requires_second_verify_password(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()

        response = self.client.post(
            f"/sales/orders/{order.id}/confirm/",
            {"current_password": "wrong-password"},
            follow=True,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.PENDING_APPROVAL)
        self.assertContains(response, "二次验证失败")
        self.assertFalse(AuditLog.objects.filter(action="sales_order_confirm", source_doc_id=order.id).exists())

    def test_shortage_create_purchase_request_page_creates_request(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._batch(self.raw, Decimal("5"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        alert = ShortageAlert.objects.get()

        page_response = self.client.get("/sales/shortages/create-purchase-request/")
        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, alert.shortage_no)

        response = self.client.post(
            "/sales/shortages/create-purchase-request/",
            {"shortage_ids": [str(alert.id)], "merge_mode": "by_material"},
        )

        request = PurchaseRequest.objects.get()
        request_item = PurchaseRequestItem.objects.get()
        alert.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/requests/{request.id}/")
        self.assertEqual(request.source_type, PurchaseRequest.SourceType.SHORTAGE)
        self.assertEqual(request_item.request_qty, Decimal("15.0000"))
        self.assertEqual(alert.status, ShortageAlert.Status.PURCHASE_REQUESTED)

    def test_purchase_request_detail_renders_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_VIEW)
        self._batch(self.raw, Decimal("5"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        alert = ShortageAlert.objects.get()
        result = create_purchase_request_from_shortages([alert.id], self.user.id, idempotency_key="view-pr")
        request = PurchaseRequest.objects.get(id=result.data["purchase_request_id"])

        response = self.client.get(f"/purchase/requests/{request.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, request.purchase_request_no)
        self.assertContains(response, self.raw.material_code)

    def test_sales_shipment_detail_renders_confirm_action(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS001",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
        )

        response = self.client.get(f"/sales/shipments/{shipment.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, shipment.shipment_no)
        self.assertContains(response, "打印")
        self.assertContains(response, "确认出库")
        self.assertContains(response, self.finished.material_code)

    def test_sales_shipment_print_excludes_price_and_records_log(self):
        self.client.force_login(self.user)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS-PRINT",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            customer_contract_no="HT-001",
            customer_address_text="深圳市测试路 1 号",
            customer_contact_name="王五",
            customer_contact_phone="13900000000",
            settlement_method="月结",
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=self.user,
            remark="送货备注",
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
            cost_price=Decimal("3.123456"),
        )

        response = self.client.get(f"/sales/shipments/{shipment.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "送货单")
        self.assertContains(response, shipment.shipment_no)
        self.assertContains(response, "HT-001")
        self.assertContains(response, "深圳市测试路 1 号")
        self.assertContains(response, "月结")
        self.assertContains(response, "送货备注")
        self.assertNotContains(response, "成本价")
        self.assertNotContains(response, "******")
        self.assertNotContains(response, "3.123456")
        print_log = PrintLog.objects.get(source_doc_type="sales_shipment", source_doc_id=shipment.id)
        self.assertEqual(print_log.template_type, "sales_shipment")
        self.assertEqual(print_log.source_doc_no, shipment.shipment_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_sales_shipment_print_excludes_price_even_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS-PRINT-AMOUNT",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
            cost_price=Decimal("3.123456"),
        )

        response = self.client.get(f"/sales/shipments/{shipment.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "成本价")
        self.assertNotContains(response, "3.123456")

    def test_sales_shipment_export_respects_scope_and_logs(self):
        other_user = get_user_model().objects.create_user(username="ship-export-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-SHIP-OTHER", customer_name="别人的客户", sales_owner=other_user)
        self.client.force_login(self.user)
        order, item = self._sales_order()
        shipment = SalesShipment.objects.create(
            shipment_no="SS-EXPORT-MINE",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=self.user,
        )
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-SHIP-OTHER",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
        )
        SalesShipment.objects.create(
            shipment_no="SS-EXPORT-OTHER",
            sales_order=other_order,
            customer=other_customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=other_user,
        )

        list_response = self.client.get("/sales/shipments/")
        response = self.client.get("/sales/shipments/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("出库单号,销售订单,客户,出库日期,状态", content)
        self.assertIn(shipment.shipment_no, content)
        self.assertNotIn("SS-EXPORT-OTHER", content)
        export_log = ExportLog.objects.get(module="sales_shipments")
        self.assertEqual(export_log.row_count, 1)

    def test_customer_return_export_masks_amount_and_respects_scope(self):
        other_user = get_user_model().objects.create_user(username="return-export-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-RETURN-OTHER", customer_name="隐藏退货客户", sales_owner=other_user)
        self.client.force_login(self.user)
        customer_return = self._customer_return()
        customer_return.return_no = "RT-EXPORT-MINE"
        customer_return.return_amount = Decimal("20.00")
        customer_return.save(update_fields=["return_no", "return_amount"])
        CustomerReturn.objects.create(
            return_no="RT-EXPORT-OTHER",
            customer=other_customer,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.CONFIRMED,
            return_amount=Decimal("66.00"),
        )

        list_response = self.client.get("/sales/returns/")
        response = self.client.get("/sales/returns/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("退货单号,客户,退货日期,状态,金额", content)
        self.assertIn("RT-EXPORT-MINE", content)
        self.assertNotIn("RT-EXPORT-OTHER", content)
        self.assertIn("******", content)
        self.assertNotIn("20.00", content)
        export_log = ExportLog.objects.get(module="customer_returns")
        self.assertEqual(export_log.row_count, 1)

    def test_customer_return_print_masks_amount_and_records_log(self):
        self.client.force_login(self.user)
        customer_return = self._customer_return()
        customer_return.return_no = "RT-PRINT"
        customer_return.return_amount = Decimal("20.00")
        customer_return.save(update_fields=["return_no", "return_amount"])

        detail_response = self.client.get(f"/sales/returns/{customer_return.id}/")
        response = self.client.get(f"/sales/returns/{customer_return.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "客户退货单")
        self.assertContains(response, "RT-PRINT")
        self.assertContains(response, "******")
        self.assertNotContains(response, "20.00")
        print_log = PrintLog.objects.get(source_doc_type="customer_return", source_doc_id=customer_return.id)
        self.assertEqual(print_log.template_type, "customer_return")
        self.assertEqual(print_log.source_doc_no, customer_return.return_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_sample_loan_export_respects_scope_and_logs(self):
        other_user = get_user_model().objects.create_user(username="loan-export-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-LOAN-OTHER", customer_name="隐藏借样客户", sales_owner=other_user)
        self.client.force_login(self.user)
        loan, loan_item, sample_return = self._sample_return()
        loan.sample_loan_no = "SL-EXPORT-MINE"
        loan.save(update_fields=["sample_loan_no"])
        SampleLoan.objects.create(
            sample_loan_no="SL-EXPORT-OTHER",
            customer=other_customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
        )

        list_response = self.client.get("/sales/sample-loans/")
        response = self.client.get("/sales/sample-loans/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("借样单号,客户,借出日期,预计归还,状态,逾期状态", content)
        self.assertIn("SL-EXPORT-MINE", content)
        self.assertNotIn("SL-EXPORT-OTHER", content)
        export_log = ExportLog.objects.get(module="sample_loans")
        self.assertEqual(export_log.row_count, 1)

    def test_sales_shipment_confirm_view_deducts_finished_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS001",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
        )

        response = self.client.post(f"/sales/shipments/{shipment.id}/confirm/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/shipments/{shipment.id}/")
        batch.refresh_from_db()
        item.refresh_from_db()
        order.refresh_from_db()
        shipment.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        transaction_row = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.SALES_OUT)
        self.assertEqual(batch.remaining_qty, Decimal("0.0000"))
        self.assertEqual(inventory.qty, Decimal("0.0000"))
        self.assertEqual(item.shipped_qty, Decimal("10.0000"))
        self.assertEqual(item.line_status, SalesOrderItem.LineStatus.SHIPPED)
        self.assertEqual(order.status, SalesOrder.Status.SHIPPED)
        self.assertEqual(shipment.status, SalesShipment.Status.SHIPPED)
        self.assertEqual(transaction_row.qty_delta, Decimal("-10.0000"))

    def test_sales_shipment_edit_updates_pending_shipment_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("10.0000"))
        second_batch = InventoryBatch.objects.create(
            batch_no="B-SECOND",
            material=self.finished,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("5.0000"),
            remaining_qty=Decimal("5.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS-EDIT",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=self.user,
        )
        shipment_item = SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
            cost_price=Decimal("1.000000"),
        )

        response = self.client.post(
            f"/sales/shipments/{shipment.id}/edit/",
            {
                "shipment_date": timezone.localdate().isoformat(),
                "remark": "编辑出库",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": shipment_item.id,
                "items-0-shipment_qty": "5",
                "items-0-batch": second_batch.id,
                "items-0-location": self.location.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        shipment.refresh_from_db()
        shipment_item.refresh_from_db()
        self.assertEqual(shipment.remark, "编辑出库")
        self.assertEqual(shipment_item.shipment_qty, Decimal("5"))
        self.assertEqual(shipment_item.batch, second_batch)
        self.assertTrue(AuditLog.objects.filter(action="sales_shipment_update", source_doc_id=shipment.id).exists())

    def test_sales_shipment_edit_rejects_qty_over_batch_remaining(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("3.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS-OVER",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=self.user,
        )
        shipment_item = SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("3.0000"),
            batch=batch,
            location=self.location,
        )

        response = self.client.post(
            f"/sales/shipments/{shipment.id}/edit/",
            {
                "shipment_date": timezone.localdate().isoformat(),
                "remark": "",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": shipment_item.id,
                "items-0-shipment_qty": "4",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "出库数量不能超过批次剩余数量")

    def test_sales_shipment_edit_and_void_require_sales_process_permission(self):
        self.client.force_login(self.user)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS-PERM",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=self.user,
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
        )

        detail_response = self.client.get(f"/sales/shipments/{shipment.id}/")
        edit_response = self.client.get(f"/sales/shipments/{shipment.id}/edit/")
        void_response = self.client.post(f"/sales/shipments/{shipment.id}/void/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertNotContains(detail_response, "确认出库")
        self.assertNotContains(detail_response, "编辑")
        self.assertNotContains(detail_response, "作废")
        self.assertEqual(edit_response.status_code, 403)
        self.assertEqual(void_response.status_code, 403)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, SalesShipment.Status.PENDING_CONFIRM)

    def test_sales_shipment_voids_pending_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)
        shipment = SalesShipment.objects.create(
            shipment_no="SS-VOID",
            sales_order=order,
            customer=self.customer,
            shipment_date=timezone.localdate(),
            status=SalesShipment.Status.PENDING_CONFIRM,
            created_by=self.user,
        )
        SalesShipmentItem.objects.create(
            shipment=shipment,
            sales_order_item=item,
            material=self.finished,
            shipment_qty=Decimal("10.0000"),
            batch=batch,
            location=self.location,
        )

        response = self.client.post(f"/sales/shipments/{shipment.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/shipments/{shipment.id}/")
        shipment.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(shipment.status, SalesShipment.Status.VOIDED)
        self.assertEqual(item.shipped_qty, Decimal("0.0000"))
        self.assertTrue(AuditLog.objects.filter(action="sales_shipment_void", source_doc_id=shipment.id).exists())

    def test_sales_order_detail_creates_shipment_from_sufficient_lines(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        address = CustomerAddress.objects.create(
            customer=self.customer,
            receiver_name="王五",
            receiver_phone_encrypted="13900000000",
            address_encrypted="深圳市测试路 1 号",
            status=CustomerAddress.AddressStatus.ACTIVE,
        )
        self.customer.settlement_method = "月结"
        self.customer.save(update_fields=["settlement_method"])
        order.customer_address = address
        order.customer_contract_no = "HT-001"
        order.save(update_fields=["customer_address", "customer_contract_no"])
        confirm_sales_order(order.id, self.user.id)

        page_response = self.client.get(f"/sales/orders/{order.id}/")
        self.assertContains(page_response, "生成出库单")

        response = self.client.post(f"/sales/orders/{order.id}/create-shipment/")

        shipment = SalesShipment.objects.get()
        shipment_item = shipment.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/shipments/{shipment.id}/")
        self.assertEqual(shipment.sales_order, order)
        self.assertEqual(shipment.status, SalesShipment.Status.PENDING_CONFIRM)
        self.assertEqual(shipment.customer_contract_no, "HT-001")
        self.assertEqual(shipment.customer_address_text, "深圳市测试路 1 号")
        self.assertEqual(shipment.customer_contact_name, "王五")
        self.assertEqual(shipment.customer_contact_phone, "13900000000")
        self.assertEqual(shipment.settlement_method, "月结")
        self.assertEqual(shipment_item.sales_order_item, item)
        self.assertEqual(shipment_item.shipment_qty, Decimal("10.0000"))
        self.assertEqual(shipment_item.batch, batch)

    def test_sales_order_create_shipment_requires_sales_process_permission(self):
        self.client.force_login(self.user)
        self._batch(self.finished, Decimal("10.0000"))
        order, item = self._sales_order()
        confirm_sales_order(order.id, self.user.id)

        page_response = self.client.get(f"/sales/orders/{order.id}/")
        response = self.client.post(f"/sales/orders/{order.id}/create-shipment/")

        self.assertNotContains(page_response, "生成出库单")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SalesShipment.objects.exists())

    def test_sales_process_actions_require_sales_process_permission(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.DRAFT
        order.created_by = self.user
        order.save(update_fields=["status", "created_by"])
        customer_return = CustomerReturn.objects.create(
            return_no="RT-NOPERM",
            customer=self.customer,
            sales_order=order,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.DRAFT,
            return_amount=Decimal("0.00"),
        )
        loan = SampleLoan.objects.create(
            sample_loan_no="SL-NOPERM",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
            created_by=self.user,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("2.0000"),
            line_status=SampleLoanItem.LineStatus.OUT,
        )
        sample_return = SampleLoanReturn.objects.create(
            sample_return_no="SR-NOPERM",
            sample_loan=loan,
            customer=self.customer,
            return_date=timezone.localdate(),
            status=SampleLoanReturn.Status.DRAFT,
        )
        SampleLoanReturnItem.objects.create(
            sample_return=sample_return,
            sample_loan=loan,
            sample_loan_item=loan_item,
            material=self.finished,
            return_qty=Decimal("1.0000"),
            location=self.location,
        )

        order_detail = self.client.get(f"/sales/orders/{order.id}/")
        return_detail = self.client.get(f"/sales/returns/{customer_return.id}/")
        loan_detail = self.client.get(f"/sales/sample-loans/{loan.id}/")
        sample_return_detail = self.client.get(f"/sales/sample-returns/{sample_return.id}/")

        self.assertNotContains(order_detail, f"/sales/orders/{order.id}/edit/")
        self.assertNotContains(order_detail, "提交审核")
        self.assertNotContains(return_detail, f"/sales/returns/{customer_return.id}/edit/")
        self.assertNotContains(loan_detail, "登记归还")
        self.assertNotContains(sample_return_detail, f"/sales/sample-returns/{sample_return.id}/edit/")

        blocked_responses = [
            self.client.get(f"/sales/orders/{order.id}/edit/"),
            self.client.post(f"/sales/orders/{order.id}/submit/"),
            self.client.post(f"/sales/orders/{order.id}/void/"),
            self.client.get(f"/sales/returns/{customer_return.id}/edit/"),
            self.client.post(f"/sales/returns/{customer_return.id}/void/"),
            self.client.post(f"/sales/sample-loans/{loan.id}/items/new/"),
            self.client.get(f"/sales/sample-loans/{loan.id}/returns/new/"),
            self.client.get(f"/sales/sample-returns/{sample_return.id}/edit/"),
            self.client.post(f"/sales/sample-returns/{sample_return.id}/void/"),
        ]
        self.assertTrue(all(response.status_code == 403 for response in blocked_responses))
        order.refresh_from_db()
        customer_return.refresh_from_db()
        sample_return.refresh_from_db()
        self.assertEqual(order.status, SalesOrder.Status.DRAFT)
        self.assertEqual(customer_return.status, CustomerReturn.Status.DRAFT)
        self.assertEqual(sample_return.status, SampleLoanReturn.Status.DRAFT)

    def test_customer_return_detail_and_confirm_receipt_view(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        customer_return = self._customer_return()

        page_response = self.client.get(f"/sales/returns/{customer_return.id}/")

        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, customer_return.return_no)
        self.assertContains(page_response, "确认退货入库")
        self.assertContains(page_response, self.finished.material_code)

        response = self.client.post(f"/sales/returns/{customer_return.id}/confirm-receipt/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/returns/{customer_return.id}/")
        customer_return.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        self.assertEqual(customer_return.status, CustomerReturn.Status.RECEIVED)
        self.assertEqual(inventory.qty, Decimal("2.0000"))

    def test_customer_return_create_view_saves_header_and_items(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["status"])
        item.shipped_qty = Decimal("10.0000")
        item.line_status = SalesOrderItem.LineStatus.SHIPPED
        item.save(update_fields=["shipped_qty", "line_status"])

        response = self.client.post(
            "/sales/returns/new/",
            {
                "customer": self.customer.id,
                "sales_order": order.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "页面创建退货",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-sales_order_item": item.id,
                "items-0-material": "",
                "items-0-return_qty": "2",
                "items-0-unit_price": "",
                "items-0-location": self.location.id,
                "items-0-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-0-return_reason": "客户退回",
                "items-1-sales_order_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-location": "",
                "items-1-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-1-return_reason": "",
                "items-2-sales_order_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-location": "",
                "items-2-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-2-return_reason": "",
                "action": "draft",
            },
        )

        customer_return = CustomerReturn.objects.order_by("-id").first()
        return_item = customer_return.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/returns/{customer_return.id}/")
        self.assertEqual(customer_return.status, CustomerReturn.Status.DRAFT)
        self.assertEqual(customer_return.return_amount, Decimal("20.00"))
        self.assertEqual(return_item.material, self.finished)
        self.assertEqual(return_item.unit_price, Decimal("10.0000"))
        self.assertEqual(return_item.return_qty, Decimal("2"))

    def test_customer_return_submit_requires_sales_process_permission(self):
        self.client.force_login(self.user)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["status"])
        item.shipped_qty = Decimal("10.0000")
        item.line_status = SalesOrderItem.LineStatus.SHIPPED
        item.save(update_fields=["shipped_qty", "line_status"])

        get_response = self.client.get("/sales/returns/new/")
        post_response = self.client.post(
            "/sales/returns/new/",
            {
                "customer": self.customer.id,
                "sales_order": order.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "无权限提交退货",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-sales_order_item": item.id,
                "items-0-material": "",
                "items-0-return_qty": "2",
                "items-0-unit_price": "",
                "items-0-location": self.location.id,
                "items-0-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-0-return_reason": "客户退回",
                "items-1-sales_order_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-location": "",
                "items-1-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-1-return_reason": "",
                "items-2-sales_order_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-location": "",
                "items-2-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-2-return_reason": "",
                "action": "submit",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertNotContains(get_response, "保存并提交审核")
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(CustomerReturn.objects.exists())

    def test_customer_return_edit_updates_draft_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["status"])
        item.shipped_qty = Decimal("10.0000")
        item.line_status = SalesOrderItem.LineStatus.SHIPPED
        item.save(update_fields=["shipped_qty", "line_status"])
        customer_return = CustomerReturn.objects.create(
            return_no="RT-EDIT",
            customer=self.customer,
            sales_order=order,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.DRAFT,
            return_amount=Decimal("10.00"),
        )
        return_item = CustomerReturnItem.objects.create(
            customer_return=customer_return,
            sales_order_item=item,
            material=self.finished,
            return_qty=Decimal("1.0000"),
            unit_price=Decimal("10.0000"),
            return_amount=Decimal("10.00"),
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        )

        response = self.client.post(
            f"/sales/returns/{customer_return.id}/edit/",
            {
                "customer": self.customer.id,
                "sales_order": order.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "编辑退货",
                "items-TOTAL_FORMS": "4",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": return_item.id,
                "items-0-sales_order_item": item.id,
                "items-0-material": self.finished.id,
                "items-0-return_qty": "3",
                "items-0-unit_price": "12.5",
                "items-0-location": self.location.id,
                "items-0-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-0-return_reason": "改数量",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-sales_order_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-location": "",
                "items-1-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-1-return_reason": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-sales_order_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-location": "",
                "items-2-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-2-return_reason": "",
                "items-2-DELETE": "",
                "items-3-id": "",
                "items-3-sales_order_item": "",
                "items-3-material": "",
                "items-3-return_qty": "",
                "items-3-unit_price": "",
                "items-3-location": "",
                "items-3-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-3-return_reason": "",
                "items-3-DELETE": "",
                "action": "submit",
            },
        )

        self.assertEqual(response.status_code, 302)
        customer_return.refresh_from_db()
        return_item.refresh_from_db()
        self.assertEqual(customer_return.status, CustomerReturn.Status.PENDING_APPROVAL)
        self.assertEqual(customer_return.return_amount, Decimal("37.50"))
        self.assertEqual(return_item.return_qty, Decimal("3"))
        self.assertEqual(return_item.return_amount, Decimal("37.50"))
        self.assertTrue(AuditLog.objects.filter(action="customer_return_update", source_doc_id=customer_return.id).exists())

    def test_customer_return_edit_rejects_over_return_qty(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["status"])
        item.shipped_qty = Decimal("10.0000")
        item.line_status = SalesOrderItem.LineStatus.SHIPPED
        item.save(update_fields=["shipped_qty", "line_status"])
        existing_return = CustomerReturn.objects.create(
            return_no="RT-EXISTING",
            customer=self.customer,
            sales_order=order,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.CONFIRMED,
            return_amount=Decimal("80.00"),
        )
        CustomerReturnItem.objects.create(
            customer_return=existing_return,
            sales_order_item=item,
            material=self.finished,
            return_qty=Decimal("8.0000"),
            unit_price=Decimal("10.0000"),
            return_amount=Decimal("80.00"),
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        )
        customer_return = CustomerReturn.objects.create(
            return_no="RT-DRAFT-OVER",
            customer=self.customer,
            sales_order=order,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.DRAFT,
            return_amount=Decimal("0.00"),
        )

        response = self.client.post(
            f"/sales/returns/{customer_return.id}/edit/",
            {
                "customer": self.customer.id,
                "sales_order": order.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-sales_order_item": item.id,
                "items-0-material": "",
                "items-0-return_qty": "3",
                "items-0-unit_price": "",
                "items-0-location": self.location.id,
                "items-0-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-0-return_reason": "",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-sales_order_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-location": "",
                "items-1-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-1-return_reason": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-sales_order_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-location": "",
                "items-2-inventory_type": InventoryBatch.InventoryType.AVAILABLE,
                "items-2-return_reason": "",
                "items-2-DELETE": "",
                "action": "draft",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "退货数量不能超过可退数量")
        self.assertEqual(customer_return.items.count(), 0)

    def test_customer_return_voids_pending_order_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        order, item = self._sales_order()
        order.status = SalesOrder.Status.SHIPPED
        order.save(update_fields=["status"])
        customer_return = CustomerReturn.objects.create(
            return_no="RT-VOID",
            customer=self.customer,
            sales_order=order,
            return_date=timezone.localdate(),
            status=CustomerReturn.Status.PENDING_APPROVAL,
            return_amount=Decimal("0.00"),
        )

        response = self.client.post(f"/sales/returns/{customer_return.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/returns/{customer_return.id}/")
        customer_return.refresh_from_db()
        self.assertEqual(customer_return.status, CustomerReturn.Status.VOIDED)
        self.assertTrue(AuditLog.objects.filter(action="customer_return_void", source_doc_id=customer_return.id).exists())

    def test_sample_loan_detail_renders_items_and_returns(self):
        self.client.force_login(self.user)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.get(f"/sales/sample-loans/{loan.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, loan.sample_loan_no)
        self.assertContains(response, self.finished.material_code)
        self.assertContains(response, sample_return.sample_return_no)
        self.assertContains(response, f"/sales/sample-loans/{loan.id}/print/")

    def test_sample_loan_print_writes_log(self):
        self.client.force_login(self.user)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.get(f"/sales/sample-loans/{loan.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, loan.sample_loan_no)
        self.assertContains(response, "借样单")
        self.assertContains(response, self.finished.material_code)
        print_log = PrintLog.objects.get(source_doc_type="sample_loan", source_doc_id=loan.id)
        self.assertEqual(print_log.template_type, "sample_loan")
        self.assertEqual(print_log.source_doc_no, loan.sample_loan_no)

    def test_sample_loan_print_respects_sales_scope(self):
        other_user = get_user_model().objects.create_user(username="loan-print-other", password="x")
        permission, _ = Permission.objects.get_or_create(
            permission_code=PermissionCode.SALES_VIEW,
            defaults={"permission_name": PermissionCode.SALES_VIEW, "permission_type": Permission.PermissionType.MODULE},
        )
        role = Role.objects.create(role_code=f"sales-view-other-{other_user.id}", role_name="销售查看")
        role.permissions.add(permission)
        other_user.roles.add(role)
        self.client.force_login(other_user)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.get(f"/sales/sample-loans/{loan.id}/print/")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PrintLog.objects.exists())

    def test_sample_return_list_filter_and_export_respect_scope(self):
        self.client.force_login(self.user)
        loan, loan_item, sample_return = self._sample_return()
        sample_return.sample_return_no = "SR-EXPORT-KEEP"
        sample_return.status = SampleLoanReturn.Status.PENDING_CONFIRM
        sample_return.save(update_fields=["sample_return_no", "status"])
        other_user = get_user_model().objects.create_user(username="sample-return-other", password="x")
        other_customer = Customer.objects.create(customer_no="C-SR-OTHER", customer_name="隐藏借样客户", sales_owner=other_user)
        other_loan = SampleLoan.objects.create(
            sample_loan_no="SL-SR-OTHER",
            customer=other_customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
            created_by=other_user,
        )
        SampleLoanReturn.objects.create(
            sample_return_no="SR-EXPORT-HIDE",
            sample_loan=other_loan,
            customer=other_customer,
            return_date=timezone.localdate(),
            status=SampleLoanReturn.Status.PENDING_CONFIRM,
        )

        list_response = self.client.get("/sales/sample-returns/?q=KEEP&status=pending_confirm")
        export_response = self.client.get("/sales/sample-returns/export/?q=KEEP&status=pending_confirm")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "借样归还")
        self.assertContains(list_response, "SR-EXPORT-KEEP")
        self.assertNotContains(list_response, "SR-EXPORT-HIDE")
        self.assertContains(list_response, "/sales/sample-returns/export/?q=KEEP&amp;status=pending_confirm")
        self.assertEqual(export_response.status_code, 200)
        self.assertIn("归还单号,借样单号,客户,归还日期,状态", content)
        self.assertIn("SR-EXPORT-KEEP", content)
        self.assertNotIn("SR-EXPORT-HIDE", content)
        export_log = ExportLog.objects.get(module="sample_loan_returns")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "pending_confirm")

    def test_sample_loan_create_view_saves_header_and_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)

        response = self.client.post(
            "/sales/sample-loans/new/",
            {
                "customer": self.customer.id,
                "loan_date": timezone.localdate().isoformat(),
                "expected_return_date": timezone.localdate().isoformat(),
                "remark": "页面创建",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-material": self.finished.id,
                "items-0-loan_qty": "2",
                "items-0-expected_return_date": "",
                "items-0-batch": "",
                "items-0-location": "",
                "items-1-material": "",
                "items-1-loan_qty": "",
                "items-1-expected_return_date": "",
                "items-1-batch": "",
                "items-1-location": "",
                "items-2-material": "",
                "items-2-loan_qty": "",
                "items-2-expected_return_date": "",
                "items-2-batch": "",
                "items-2-location": "",
            },
        )

        loan = SampleLoan.objects.order_by("-id").first()
        item = loan.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/sample-loans/{loan.id}/")
        self.assertEqual(loan.status, SampleLoan.Status.PENDING_APPROVAL)
        self.assertEqual(loan.created_by, self.user)
        self.assertEqual(item.material, self.finished)
        self.assertEqual(item.loan_qty, Decimal("2"))

    def test_sample_loan_create_requires_sales_process_permission(self):
        self.client.force_login(self.user)

        list_response = self.client.get("/sales/sample-loans/")
        get_response = self.client.get("/sales/sample-loans/new/")
        post_response = self.client.post(
            "/sales/sample-loans/new/",
            {
                "customer": self.customer.id,
                "loan_date": timezone.localdate().isoformat(),
                "expected_return_date": timezone.localdate().isoformat(),
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
        )

        self.assertNotContains(list_response, "/sales/sample-loans/new/")
        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(SampleLoan.objects.exists())

    def test_sample_loan_detail_adds_item(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.PENDING_APPROVAL,
            created_by=self.user,
        )

        response = self.client.post(
            f"/sales/sample-loans/{loan.id}/items/new/",
            {
                "items-0-material": self.finished.id,
                "items-0-loan_qty": "3",
                "items-0-expected_return_date": "2026/7/8",
                "items-0-batch": "",
                "items-0-location": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/sample-loans/{loan.id}/")
        item = loan.items.get()
        self.assertEqual(item.line_no, 1)
        self.assertEqual(item.material, self.finished)
        self.assertEqual(item.loan_qty, Decimal("3"))
        self.assertEqual(item.expected_return_date, date(2026, 7, 8))

    def test_sample_loan_confirm_out_view_deducts_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        batch = self._batch(self.finished, Decimal("5.0000"))
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.PENDING_APPROVAL,
            created_by=self.user,
        )
        SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("2.0000"),
            batch=batch,
            location=self.location,
        )

        response = self.client.post(f"/sales/sample-loans/{loan.id}/confirm-out/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/sample-loans/{loan.id}/")
        loan.refresh_from_db()
        batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        self.assertEqual(loan.status, SampleLoan.Status.OUT)
        self.assertEqual(batch.remaining_qty, Decimal("3.0000"))
        self.assertEqual(inventory.qty, Decimal("3.0000"))

    def test_sample_loan_convert_to_sales_view_creates_order(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        batch = self._batch(self.finished, Decimal("5.0000"))
        loan = SampleLoan.objects.create(
            sample_loan_no="SL001",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
            created_by=self.user,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("3.0000"),
            batch=batch,
            location=self.location,
            line_status=SampleLoanItem.LineStatus.OUT,
        )

        page_response = self.client.get(f"/sales/sample-loans/{loan.id}/")
        self.assertContains(page_response, "转销售")

        response = self.client.post(
            f"/sales/sample-loans/{loan.id}/convert-to-sales/",
            {
                "sample_loan_item": loan_item.id,
                "convert_qty": "2",
                "unit_price": "15",
            },
        )

        order = SalesOrder.objects.order_by("-id").first()
        loan_item.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/orders/{order.id}/")
        self.assertEqual(order.total_amount, Decimal("30.00"))
        self.assertEqual(loan_item.sold_qty, Decimal("2.0000"))

    def test_sample_loan_convert_to_sales_requires_amount_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan = SampleLoan.objects.create(
            sample_loan_no="SL-NO-AMOUNT",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
            created_by=self.user,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("3.0000"),
            line_status=SampleLoanItem.LineStatus.OUT,
        )

        page_response = self.client.get(f"/sales/sample-loans/{loan.id}/")
        response = self.client.post(
            f"/sales/sample-loans/{loan.id}/convert-to-sales/",
            {
                "sample_loan_item": loan_item.id,
                "convert_qty": "2",
                "unit_price": "15",
            },
        )

        self.assertNotContains(page_response, f"/sales/sample-loans/{loan.id}/convert-to-sales/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SalesOrder.objects.filter(sales_order_no__startswith="SO").exclude(sales_order_no="SO001").exists())
        loan_item.refresh_from_db()
        self.assertEqual(loan_item.sold_qty, Decimal("0.0000"))

    def test_sample_return_detail_renders_confirm_action(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.get(f"/sales/sample-returns/{sample_return.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, sample_return.sample_return_no)
        self.assertContains(response, "确认归还入库")
        self.assertContains(response, self.finished.material_code)
        self.assertContains(response, f"/sales/sample-returns/{sample_return.id}/print/")

    def test_sample_return_print_writes_log(self):
        self.client.force_login(self.user)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.get(f"/sales/sample-returns/{sample_return.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, sample_return.sample_return_no)
        self.assertContains(response, "借样归还单")
        self.assertContains(response, self.finished.material_code)
        print_log = PrintLog.objects.get(source_doc_type="sample_loan_return", source_doc_id=sample_return.id)
        self.assertEqual(print_log.template_type, "sample_loan_return")
        self.assertEqual(print_log.source_doc_no, sample_return.sample_return_no)

    def test_sample_return_create_view_saves_header_and_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan = SampleLoan.objects.create(
            sample_loan_no="SL-RETURN-CREATE",
            customer=self.customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate(),
            status=SampleLoan.Status.OUT,
            created_by=self.user,
        )
        loan_item = SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=1,
            material=self.finished,
            loan_qty=Decimal("5.0000"),
            line_status=SampleLoanItem.LineStatus.OUT,
        )

        response = self.client.post(
            f"/sales/sample-loans/{loan.id}/returns/new/",
            {
                "return_date": timezone.localdate().isoformat(),
                "remark": "页面登记归还",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-sample_loan_item": loan_item.id,
                "items-0-return_qty": "2",
                "items-0-location": self.location.id,
                "items-0-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-0-remark": "完好",
                "items-1-sample_loan_item": "",
                "items-1-return_qty": "",
                "items-1-location": "",
                "items-1-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-1-remark": "",
                "items-2-sample_loan_item": "",
                "items-2-return_qty": "",
                "items-2-location": "",
                "items-2-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-2-remark": "",
                "action": "draft",
            },
        )

        sample_return = SampleLoanReturn.objects.order_by("-id").first()
        return_item = sample_return.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/sample-returns/{sample_return.id}/")
        self.assertEqual(sample_return.status, SampleLoanReturn.Status.DRAFT)
        self.assertEqual(sample_return.sample_loan, loan)
        self.assertEqual(sample_return.customer, self.customer)
        self.assertEqual(return_item.material, self.finished)
        self.assertEqual(return_item.return_qty, Decimal("2"))
        self.assertEqual(return_item.inventory_type, InventoryBatch.InventoryType.AVAILABLE)

    def test_sample_return_edit_updates_draft_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan, loan_item, sample_return = self._sample_return(qty=Decimal("3.0000"))
        sample_return.status = SampleLoanReturn.Status.DRAFT
        sample_return.save(update_fields=["status"])
        return_item = sample_return.items.get()

        response = self.client.post(
            f"/sales/sample-returns/{sample_return.id}/edit/",
            {
                "return_date": timezone.localdate().isoformat(),
                "remark": "编辑归还",
                "items-TOTAL_FORMS": "4",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": return_item.id,
                "items-0-sample_loan_item": loan_item.id,
                "items-0-return_qty": "2",
                "items-0-location": self.location.id,
                "items-0-sample_condition": SampleLoanReturnItem.SampleCondition.DAMAGED,
                "items-0-remark": "轻微损坏",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-sample_loan_item": "",
                "items-1-return_qty": "",
                "items-1-location": "",
                "items-1-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-1-remark": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-sample_loan_item": "",
                "items-2-return_qty": "",
                "items-2-location": "",
                "items-2-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-2-remark": "",
                "items-2-DELETE": "",
                "items-3-id": "",
                "items-3-sample_loan_item": "",
                "items-3-return_qty": "",
                "items-3-location": "",
                "items-3-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-3-remark": "",
                "items-3-DELETE": "",
                "action": "submit",
            },
        )

        self.assertEqual(response.status_code, 302)
        sample_return.refresh_from_db()
        return_item.refresh_from_db()
        self.assertEqual(sample_return.status, SampleLoanReturn.Status.PENDING_CONFIRM)
        self.assertEqual(return_item.return_qty, Decimal("2"))
        self.assertEqual(return_item.sample_condition, SampleLoanReturnItem.SampleCondition.DAMAGED)
        self.assertEqual(return_item.inventory_type, InventoryBatch.InventoryType.PENDING)
        self.assertTrue(AuditLog.objects.filter(action="sample_return_update", source_doc_id=sample_return.id).exists())

    def test_sample_return_edit_rejects_over_return_qty(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan, loan_item, sample_return = self._sample_return(qty=Decimal("1.0000"))
        sample_return.status = SampleLoanReturn.Status.DRAFT
        sample_return.save(update_fields=["status"])
        return_item = sample_return.items.get()
        loan_item.returned_qty = Decimal("1.0000")
        loan_item.save(update_fields=["returned_qty"])

        response = self.client.post(
            f"/sales/sample-returns/{sample_return.id}/edit/",
            {
                "return_date": timezone.localdate().isoformat(),
                "remark": "",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": return_item.id,
                "items-0-sample_loan_item": loan_item.id,
                "items-0-return_qty": "2",
                "items-0-location": self.location.id,
                "items-0-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-0-remark": "",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-sample_loan_item": "",
                "items-1-return_qty": "",
                "items-1-location": "",
                "items-1-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-1-remark": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-sample_loan_item": "",
                "items-2-return_qty": "",
                "items-2-location": "",
                "items-2-sample_condition": SampleLoanReturnItem.SampleCondition.GOOD,
                "items-2-remark": "",
                "items-2-DELETE": "",
                "action": "draft",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "归还数量不能超过可归还数量")

    def test_sample_return_voids_pending_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.post(f"/sales/sample-returns/{sample_return.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/sample-returns/{sample_return.id}/")
        sample_return.refresh_from_db()
        self.assertEqual(sample_return.status, SampleLoanReturn.Status.VOIDED)
        self.assertTrue(AuditLog.objects.filter(action="sample_return_void", source_doc_id=sample_return.id).exists())

    def test_sample_return_confirm_view_increases_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.SALES_PROCESS)
        loan, loan_item, sample_return = self._sample_return()

        response = self.client.post(f"/sales/sample-returns/{sample_return.id}/confirm/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/sales/sample-returns/{sample_return.id}/")
        loan.refresh_from_db()
        loan_item.refresh_from_db()
        sample_return.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        transaction_row = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.SAMPLE_RETURN_IN)
        self.assertEqual(sample_return.status, SampleLoanReturn.Status.RECEIVED)
        self.assertEqual(loan.status, SampleLoan.Status.RETURNED)
        self.assertEqual(loan.overdue_status, SampleLoan.OverdueStatus.CLOSED)
        self.assertEqual(loan_item.returned_qty, Decimal("2.0000"))
        self.assertEqual(inventory.qty, Decimal("2.0000"))
        self.assertEqual(transaction_row.qty_delta, Decimal("2.0000"))


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
