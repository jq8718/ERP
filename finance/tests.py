from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from files.models import Attachment, ExportLog, ImportJob, PrintLog
from finance.models import (
    CustomerCreditBalance,
    CustomerCreditBalanceTransaction,
    CustomerReceipt,
    CustomerReceiptAllocation,
    CustomerReceiptReversal,
    ExpenseRecord,
    OpeningPayable,
    OpeningReceivable,
    Reconciliation,
    ReconciliationItem,
    SupplierCreditBalance,
    SupplierCreditBalanceTransaction,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from finance.services import (
    apply_customer_credit_balance,
    confirm_customer_receipt,
    confirm_supplier_payment,
    reverse_customer_receipt,
    reverse_supplier_payment,
    apply_supplier_credit_balance,
)
from inventory.models import WarehouseLocation
from masterdata.models import Customer, CustomerProduct, Material, Supplier
from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
from sales.models import SalesOrder, SalesOrderItem
from system.models import AuditLog


class FinanceServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="finance", password="x")
        self.customer = Customer.objects.create(customer_no="C001", customer_name="测试客户")
        self.supplier = Supplier.objects.create(supplier_no="S001", supplier_name="测试供应商")
        self.location = WarehouseLocation.objects.create(location_code="A01", location_name="A01")
        self.finished = Material.objects.create(
            material_code="FG001",
            material_name="成品 1",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        self.raw = Material.objects.create(
            material_code="RM001",
            material_name="原料 1",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
        )
        self.customer_product = CustomerProduct.objects.create(
            customer=self.customer,
            customer_product_no="CP001",
            customer_product_name="客户产品",
            finished_material=self.finished,
        )

    def _grant_permission(self, permission_code: str):
        permission_types = {
            PermissionCode.FINANCE_VIEW_AMOUNT: Permission.PermissionType.FIELD,
            PermissionCode.FINANCE_PAYMENT_PROCESS: Permission.PermissionType.ACTION,
        }
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_types.get(permission_code, Permission.PermissionType.ACTION),
            },
        )
        role = Role.objects.create(role_code=f"finance-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def _grant_finance_process_permissions(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self._grant_permission(PermissionCode.FINANCE_PAYMENT_PROCESS)

    def _sales_order(self, no="SO001", amount=Decimal("100.00")):
        order = SalesOrder.objects.create(
            sales_order_no=no,
            customer=self.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.SHIPPED,
            total_amount=amount,
        )
        SalesOrderItem.objects.create(
            sales_order=order,
            line_no=1,
            customer_product=self.customer_product,
            finished_material=self.finished,
            order_qty=Decimal("10.0000"),
            unit_price=Decimal("10.0000"),
            line_amount=amount,
            line_status=SalesOrderItem.LineStatus.SHIPPED,
        )
        return order

    def _purchase_receipt(self, amount=Decimal("100.00")):
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO001",
            supplier=self.supplier,
            status=PurchaseOrder.Status.RECEIVED,
            order_date=timezone.localdate(),
            total_amount=amount,
        )
        order_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("10.0000"),
            received_qty=Decimal("10.0000"),
            unit_price=Decimal("10.000000"),
            line_amount=amount,
            line_status=PurchaseOrderItem.LineStatus.RECEIVED,
        )
        receipt = PurchaseReceipt.objects.create(
            purchase_receipt_no="GR001",
            purchase_order=order,
            supplier=self.supplier,
            receipt_date=timezone.localdate(),
            status=PurchaseReceipt.Status.RECEIVED,
        )
        PurchaseReceiptItem.objects.create(
            purchase_receipt=receipt,
            purchase_order_item=order_item,
            material=self.raw,
            received_qty=Decimal("10.0000"),
            accepted_qty=Decimal("10.0000"),
            unit_price=Decimal("10.000000"),
            location=self.location,
        )
        return receipt

    def _purchase_receipt_with_no(self, receipt_no, order_no, amount=Decimal("100.00")):
        order = PurchaseOrder.objects.create(
            purchase_order_no=order_no,
            supplier=self.supplier,
            status=PurchaseOrder.Status.RECEIVED,
            order_date=timezone.localdate(),
            total_amount=amount,
        )
        order_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("10.0000"),
            received_qty=Decimal("10.0000"),
            unit_price=Decimal("10.000000"),
            line_amount=amount,
            line_status=PurchaseOrderItem.LineStatus.RECEIVED,
        )
        receipt = PurchaseReceipt.objects.create(
            purchase_receipt_no=receipt_no,
            purchase_order=order,
            supplier=self.supplier,
            receipt_date=timezone.localdate(),
            status=PurchaseReceipt.Status.RECEIVED,
        )
        PurchaseReceiptItem.objects.create(
            purchase_receipt=receipt,
            purchase_order_item=order_item,
            material=self.raw,
            received_qty=Decimal("10.0000"),
            accepted_qty=Decimal("10.0000"),
            unit_price=Decimal("10.000000"),
            location=self.location,
        )
        return receipt

    def test_confirm_customer_receipt_allocates_order_and_creates_balance(self):
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC001",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("120.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        result = confirm_customer_receipt(receipt.id, [{"sales_order_id": order.id, "allocated_amount": "100.00"}], self.user.id, "rc-1")

        self.assertTrue(result.success)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.CONFIRMED)
        self.assertEqual(receipt.unallocated_amount, Decimal("20.00"))
        self.assertEqual(CustomerReceiptAllocation.objects.get().allocated_amount, Decimal("100.00"))
        balance = CustomerCreditBalance.objects.get()
        self.assertEqual(balance.remaining_amount, Decimal("20.00"))

    def test_confirm_customer_receipt_allocates_opening_receivable(self):
        opening = OpeningReceivable.objects.create(
            opening_no="OR001",
            customer=self.customer,
            source_doc_no="OLD-SO-001",
            opening_date=timezone.localdate(),
            opening_amount=Decimal("150.00"),
            remaining_amount=Decimal("150.00"),
            status=OpeningReceivable.Status.OPEN,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-OPEN",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("60.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        result = confirm_customer_receipt(
            receipt.id,
            [{"opening_receivable_id": opening.id, "allocated_amount": "60.00"}],
            self.user.id,
            "rc-open",
        )

        self.assertTrue(result.success)
        opening.refresh_from_db()
        self.assertEqual(opening.status, OpeningReceivable.Status.PART_SETTLED)
        self.assertEqual(opening.settled_amount, Decimal("60.00"))
        self.assertEqual(opening.remaining_amount, Decimal("90.00"))
        allocation = CustomerReceiptAllocation.objects.get(customer_receipt=receipt)
        self.assertEqual(allocation.opening_receivable, opening)
        self.assertEqual(allocation.allocation_type, CustomerReceiptAllocation.AllocationType.OPENING_RECEIVABLE)

    def test_reverse_customer_receipt_rolls_back_opening_receivable(self):
        opening = OpeningReceivable.objects.create(
            opening_no="OR-RCR",
            customer=self.customer,
            opening_date=timezone.localdate(),
            opening_amount=Decimal("100.00"),
            remaining_amount=Decimal("100.00"),
            status=OpeningReceivable.Status.OPEN,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-RCR-OPEN",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(
            receipt.id,
            [{"opening_receivable_id": opening.id, "allocated_amount": "100.00"}],
            self.user.id,
            "rc-rcr-open",
        )

        result = reverse_customer_receipt(receipt.id, Decimal("100.00"), "录错", self.user.id, "rcr-open")

        self.assertTrue(result.success)
        opening.refresh_from_db()
        self.assertEqual(opening.status, OpeningReceivable.Status.OPEN)
        self.assertEqual(opening.settled_amount, Decimal("0.00"))
        self.assertEqual(opening.remaining_amount, Decimal("100.00"))

    def test_confirm_customer_receipt_sorts_allocations_by_sales_order_id(self):
        order_1 = self._sales_order("SO001", Decimal("100.00"))
        order_2 = self._sales_order("SO002", Decimal("100.00"))
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-SORT",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("200.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        result = confirm_customer_receipt(
            receipt.id,
            [
                {"sales_order_id": order_2.id, "allocated_amount": "100.00"},
                {"sales_order_id": order_1.id, "allocated_amount": "100.00"},
            ],
            self.user.id,
            "rc-sort",
        )

        self.assertTrue(result.success)
        allocation_order_ids = list(
            CustomerReceiptAllocation.objects.filter(customer_receipt=receipt).order_by("id").values_list("sales_order_id", flat=True)
        )
        self.assertEqual(allocation_order_ids, [order_1.id, order_2.id])

    def test_confirm_customer_receipt_allocates_confirmed_reconciliation(self):
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-CUST-ALLOC",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("90.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-ALLOC",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("90.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        result = confirm_customer_receipt(
            receipt.id,
            [{"reconciliation_id": reconciliation.id, "allocated_amount": "90.00"}],
            self.user.id,
            "rc-rec-alloc",
        )

        self.assertTrue(result.success)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.CONFIRMED)
        self.assertEqual(receipt.unallocated_amount, Decimal("0.00"))
        allocation = CustomerReceiptAllocation.objects.get(customer_receipt=receipt)
        self.assertEqual(allocation.reconciliation, reconciliation)
        self.assertIsNone(allocation.sales_order)
        self.assertEqual(allocation.allocation_type, CustomerReceiptAllocation.AllocationType.RECONCILIATION)

    def test_confirm_customer_receipt_rejects_reconciliation_over_allocation(self):
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-CUST-OVER",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("100.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        previous_receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-OLD-ALLOC",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("70.00"),
            status=CustomerReceipt.Status.CONFIRMED,
        )
        CustomerReceiptAllocation.objects.create(
            customer_receipt=previous_receipt,
            reconciliation=reconciliation,
            allocated_amount=Decimal("70.00"),
            allocation_type=CustomerReceiptAllocation.AllocationType.RECONCILIATION,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-OVER",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("40.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        result = confirm_customer_receipt(
            receipt.id,
            [{"reconciliation_id": reconciliation.id, "allocated_amount": "40.00"}],
            self.user.id,
            "rc-rec-over",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "PAYMENT_ALLOCATION_OVER")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertFalse(CustomerReceiptAllocation.objects.filter(customer_receipt=receipt).exists())

    def test_reverse_customer_receipt_creates_negative_allocation(self):
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC002",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(receipt.id, [{"sales_order_id": order.id, "allocated_amount": "100.00"}], self.user.id, "rc-2")

        result = reverse_customer_receipt(receipt.id, Decimal("100.00"), "录错", self.user.id, "rcr-1")

        self.assertTrue(result.success)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.REVERSED)
        self.assertEqual(CustomerReceiptReversal.objects.get().reversal_amount, Decimal("100.00"))
        self.assertEqual(CustomerReceiptAllocation.objects.filter(allocated_amount__lt=0).get().allocated_amount, Decimal("-100.00"))

    def test_apply_customer_credit_balance_closes_balance(self):
        balance = CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=1,
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=CustomerCreditBalance.Status.PENDING,
        )

        result = apply_customer_credit_balance(
            balance.id,
            CustomerCreditBalanceTransaction.ActionType.CLOSE,
            Decimal("20.00"),
            self.user.id,
            reason="不再处理",
            idempotency_key="cb-1",
        )

        self.assertTrue(result.success)
        balance.refresh_from_db()
        self.assertEqual(balance.status, CustomerCreditBalance.Status.CLOSED)
        self.assertEqual(balance.remaining_amount, Decimal("0.00"))

    def test_confirm_supplier_payment_and_reverse(self):
        receipt = self._purchase_receipt()
        payment = SupplierPayment.objects.create(
            payment_no="PY001",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        result = confirm_supplier_payment(payment.id, [{"purchase_receipt_id": receipt.id, "allocated_amount": "100.00"}], self.user.id, "py-1")

        self.assertTrue(result.success)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.CONFIRMED)
        self.assertEqual(SupplierPaymentAllocation.objects.get().allocated_amount, Decimal("100.00"))

        reverse_result = reverse_supplier_payment(payment.id, Decimal("100.00"), "录错", self.user.id, "rpy-1")

        self.assertTrue(reverse_result.success)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.REVERSED)
        self.assertEqual(SupplierPaymentReversal.objects.get().reversal_amount, Decimal("100.00"))

    def test_confirm_supplier_payment_allocates_opening_payable(self):
        opening = OpeningPayable.objects.create(
            opening_no="OP001",
            supplier=self.supplier,
            source_doc_no="OLD-GR-001",
            opening_date=timezone.localdate(),
            opening_amount=Decimal("120.00"),
            remaining_amount=Decimal("120.00"),
            status=OpeningPayable.Status.OPEN,
        )
        payment = SupplierPayment.objects.create(
            payment_no="PY-OPEN",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("120.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        result = confirm_supplier_payment(
            payment.id,
            [{"opening_payable_id": opening.id, "allocated_amount": "120.00"}],
            self.user.id,
            "py-open",
        )

        self.assertTrue(result.success)
        opening.refresh_from_db()
        self.assertEqual(opening.status, OpeningPayable.Status.SETTLED)
        self.assertEqual(opening.settled_amount, Decimal("120.00"))
        self.assertEqual(opening.remaining_amount, Decimal("0.00"))
        allocation = SupplierPaymentAllocation.objects.get(supplier_payment=payment)
        self.assertEqual(allocation.opening_payable, opening)
        self.assertEqual(allocation.allocation_type, SupplierPaymentAllocation.AllocationType.OPENING_PAYABLE)

    def test_reverse_supplier_payment_rolls_back_opening_payable(self):
        opening = OpeningPayable.objects.create(
            opening_no="OP-RPY",
            supplier=self.supplier,
            opening_date=timezone.localdate(),
            opening_amount=Decimal("80.00"),
            remaining_amount=Decimal("80.00"),
            status=OpeningPayable.Status.OPEN,
        )
        payment = SupplierPayment.objects.create(
            payment_no="PY-RPY-OPEN",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("80.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        confirm_supplier_payment(
            payment.id,
            [{"opening_payable_id": opening.id, "allocated_amount": "80.00"}],
            self.user.id,
            "py-rpy-open",
        )

        result = reverse_supplier_payment(payment.id, Decimal("80.00"), "录错", self.user.id, "rpy-open")

        self.assertTrue(result.success)
        opening.refresh_from_db()
        self.assertEqual(opening.status, OpeningPayable.Status.OPEN)
        self.assertEqual(opening.settled_amount, Decimal("0.00"))
        self.assertEqual(opening.remaining_amount, Decimal("80.00"))

    def test_confirm_supplier_payment_sorts_allocations_by_purchase_receipt_id(self):
        receipt_1 = self._purchase_receipt_with_no("GR-SORT-1", "PO-SORT-1")
        receipt_2 = self._purchase_receipt_with_no("GR-SORT-2", "PO-SORT-2")
        payment = SupplierPayment.objects.create(
            payment_no="PY-SORT",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("200.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        result = confirm_supplier_payment(
            payment.id,
            [
                {"purchase_receipt_id": receipt_2.id, "allocated_amount": "100.00"},
                {"purchase_receipt_id": receipt_1.id, "allocated_amount": "100.00"},
            ],
            self.user.id,
            "py-sort",
        )

        self.assertTrue(result.success)
        allocation_receipt_ids = list(
            SupplierPaymentAllocation.objects.filter(supplier_payment=payment).order_by("id").values_list("purchase_receipt_id", flat=True)
        )
        self.assertEqual(allocation_receipt_ids, [receipt_1.id, receipt_2.id])

    def test_confirm_supplier_payment_allocates_confirmed_reconciliation(self):
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-SUP-ALLOC",
            party_type=Reconciliation.PartyType.SUPPLIER,
            supplier=self.supplier,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("80.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        payment = SupplierPayment.objects.create(
            payment_no="PY-REC-ALLOC",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("80.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        result = confirm_supplier_payment(
            payment.id,
            [{"reconciliation_id": reconciliation.id, "allocated_amount": "80.00"}],
            self.user.id,
            "py-rec-alloc",
        )

        self.assertTrue(result.success)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.CONFIRMED)
        self.assertEqual(payment.unallocated_amount, Decimal("0.00"))
        allocation = SupplierPaymentAllocation.objects.get(supplier_payment=payment)
        self.assertEqual(allocation.reconciliation, reconciliation)
        self.assertIsNone(allocation.purchase_receipt)
        self.assertEqual(allocation.allocation_type, SupplierPaymentAllocation.AllocationType.RECONCILIATION)

    def test_customer_receipt_detail_renders_reverse_action(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-VIEW",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(receipt.id, [{"sales_order_id": order.id, "allocated_amount": "100.00"}], self.user.id, "rc-view")

        response = self.client.get(f"/finance/customer-receipts/{receipt.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, receipt.receipt_no)
        self.assertContains(response, "确认红冲")
        self.assertContains(response, order.sales_order_no)
        self.assertContains(response, "100.00")

    def test_customer_receipt_detail_masks_amount_without_permission(self):
        self.client.force_login(self.user)
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-MASK",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(receipt.id, [{"sales_order_id": order.id, "allocated_amount": "100.00"}], self.user.id, "rc-mask")

        response = self.client.get(f"/finance/customer-receipts/{receipt.id}/")

        self.assertEqual(response.status_code, 403)

    def test_customer_receipt_list_masks_amount_without_permission(self):
        self.client.force_login(self.user)
        CustomerReceipt.objects.create(
            receipt_no="RC-LIST-MASK",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("123.45"),
            unallocated_amount=Decimal("23.45"),
            status=CustomerReceipt.Status.CONFIRMED,
        )

        response = self.client.get("/finance/customer-receipts/")

        self.assertEqual(response.status_code, 403)

    def test_finance_create_buttons_require_amount_and_process_permissions(self):
        self.client.force_login(self.user)

        receipt_response = self.client.get("/finance/customer-receipts/")
        payment_response = self.client.get("/finance/supplier-payments/")
        reconciliation_response = self.client.get("/finance/reconciliations/")

        self.assertEqual(receipt_response.status_code, 403)
        self.assertEqual(payment_response.status_code, 403)
        self.assertEqual(reconciliation_response.status_code, 403)

        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        receipt_response = self.client.get("/finance/customer-receipts/")
        self.assertContains(receipt_response, "/finance/customer-receipts/export/")
        self.assertNotContains(receipt_response, "/finance/customer-receipts/new/")

        self._grant_permission(PermissionCode.FINANCE_PAYMENT_PROCESS)

        receipt_response = self.client.get("/finance/customer-receipts/")
        payment_response = self.client.get("/finance/supplier-payments/")
        reconciliation_response = self.client.get("/finance/reconciliations/")

        self.assertContains(receipt_response, "/finance/customer-receipts/new/")
        self.assertContains(payment_response, "/finance/supplier-payments/new/")
        self.assertContains(reconciliation_response, "/finance/reconciliations/new/")

    def test_customer_receipt_export_masks_amount_and_filter_matches_list(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        CustomerReceipt.objects.create(
            receipt_no="RC-FILTER-KEEP",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("123.45"),
            unallocated_amount=Decimal("23.45"),
            status=CustomerReceipt.Status.CONFIRMED,
        )
        CustomerReceipt.objects.create(
            receipt_no="RC-FILTER-HIDE",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("99.99"),
            unallocated_amount=Decimal("9.99"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        list_response = self.client.get("/finance/customer-receipts/?q=KEEP&status=confirmed")
        export_response = self.client.get("/finance/customer-receipts/export/?q=KEEP&status=confirmed")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "RC-FILTER-KEEP")
        self.assertNotContains(list_response, "RC-FILTER-HIDE")
        self.assertContains(list_response, "/finance/customer-receipts/export/?q=KEEP&amp;status=confirmed")
        self.assertIn("收款单号,客户,收款日期,金额,未分配,状态", content)
        self.assertIn("RC-FILTER-KEEP", content)
        self.assertNotIn("RC-FILTER-HIDE", content)
        self.assertIn("123.45", content)
        export_log = ExportLog.objects.get(module="customer_receipts")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")

    def test_customer_receipt_export_shows_amount_with_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        CustomerReceipt.objects.create(
            receipt_no="RC-EXPORT-AMOUNT",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("123.45"),
            unallocated_amount=Decimal("23.45"),
            status=CustomerReceipt.Status.CONFIRMED,
        )

        response = self.client.get("/finance/customer-receipts/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("123.45", content)
        self.assertIn("23.45", content)

    def test_customer_receipt_import_template_downloads_csv(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()

        response = self.client.get("/finance/customer-receipts/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("收款单号,客户编号,单据日期,收款金额,收款方式,备注", content)
        self.assertIn("RC-INIT-001", content)

    def test_customer_receipt_import_creates_pending_receipts_without_allocation(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        upload = SimpleUploadedFile(
            "customer_receipts.csv",
            (
                "收款单号,客户编号,单据日期,收款金额,收款方式,备注\n"
                "RC-IMP-001,C001,2026-06-10,120.50,transfer,导入收款\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/finance/customer-receipts/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/finance/customer-receipts/")
        receipt = CustomerReceipt.objects.get(receipt_no="RC-IMP-001")
        self.assertEqual(receipt.customer, self.customer)
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertEqual(receipt.receipt_amount, Decimal("120.50"))
        self.assertEqual(receipt.unallocated_amount, Decimal("120.50"))
        self.assertEqual(receipt.receipt_method, CustomerReceipt.ReceiptMethod.TRANSFER)
        self.assertEqual(receipt.created_by, self.user)
        self.assertFalse(CustomerReceiptAllocation.objects.filter(customer_receipt=receipt).exists())
        job = ImportJob.objects.get(template_type="customer_receipts")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_customer_receipt_import_reports_validation_errors(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        CustomerReceipt.objects.create(
            receipt_no="RC-DUP",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("10.00"),
            unallocated_amount=Decimal("10.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        upload = SimpleUploadedFile(
            "customer_receipts.csv",
            (
                "收款单号,客户编号,单据日期,收款金额,收款方式,备注\n"
                "RC-DUP,C-MISSING,bad-date,-1,bad-method,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/finance/customer-receipts/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "客户收款单号已存在")
        self.assertContains(response, "客户不存在或未启用")
        self.assertContains(response, "收款日期格式错误")
        self.assertContains(response, "收款金额必须大于 0")
        self.assertContains(response, "收款方式必须是 cash、transfer、check 或 other")
        job = ImportJob.objects.get(template_type="customer_receipts")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertEqual(CustomerReceipt.objects.filter(receipt_no="RC-DUP").count(), 1)

    def test_customer_receipt_import_requires_amount_and_process_permissions(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/finance/customer-receipts/import-template/")
        import_response = self.client.get("/finance/customer-receipts/import/")
        list_response = self.client.get("/finance/customer-receipts/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)
        self.assertEqual(list_response.status_code, 403)

        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        list_response = self.client.get("/finance/customer-receipts/")
        self.assertNotContains(list_response, "/finance/customer-receipts/import/")

    def test_customer_receipt_print_masks_amount_and_records_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-PRINT",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            unallocated_amount=Decimal("0.00"),
            status=CustomerReceipt.Status.CONFIRMED,
            handled_by=self.user,
            created_by=self.user,
        )
        CustomerReceiptAllocation.objects.create(
            customer_receipt=receipt,
            sales_order=order,
            allocated_amount=Decimal("100.00"),
            allocation_type=CustomerReceiptAllocation.AllocationType.SALES_ORDER,
            created_by=self.user,
        )

        detail_response = self.client.get(f"/finance/customer-receipts/{receipt.id}/")
        response = self.client.get(f"/finance/customer-receipts/{receipt.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "客户收款凭证")
        self.assertContains(response, "RC-PRINT")
        self.assertContains(response, order.sales_order_no)
        self.assertContains(response, "100.00")
        print_log = PrintLog.objects.get(source_doc_type="customer_receipt", source_doc_id=receipt.id)
        self.assertEqual(print_log.template_type, "customer_receipt")
        self.assertEqual(print_log.source_doc_no, receipt.receipt_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_reconciliation_export_masks_amount_and_logs(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        Reconciliation.objects.create(
            reconciliation_no="REC-EXPORT",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("123.45"),
            status=Reconciliation.Status.CONFIRMED,
            created_by=self.user,
        )

        list_response = self.client.get("/finance/reconciliations/")
        response = self.client.get("/finance/reconciliations/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("对账单号,对象类型,客户,供应商,开始日期,结束日期,金额,状态", content)
        self.assertIn("REC-EXPORT", content)
        self.assertIn("123.45", content)
        export_log = ExportLog.objects.get(module="reconciliations")
        self.assertEqual(export_log.row_count, 1)

    def test_customer_receipt_reverse_view_creates_reversal(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REV",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(receipt.id, [{"sales_order_id": order.id, "allocated_amount": "100.00"}], self.user.id, "rc-rev")

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/reverse/",
            {"reversal_amount": "100.00", "reason": "录错", "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-receipts/{receipt.id}/")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.REVERSED)
        self.assertEqual(CustomerReceiptReversal.objects.get(source_receipt=receipt).reversal_amount, Decimal("100.00"))

    def test_customer_receipt_reverse_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REV-DENIED",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(receipt.id, [{"sales_order_id": order.id, "allocated_amount": "100.00"}], self.user.id, "rc-rev-denied")

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/reverse/",
            {"reversal_amount": "100.00", "reason": "录错", "current_password": "x"},
        )

        self.assertEqual(response.status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.CONFIRMED)
        self.assertFalse(CustomerReceiptReversal.objects.filter(source_receipt=receipt).exists())

    def test_customer_receipt_create_view_creates_pending_receipt(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()

        response = self.client.post(
            "/finance/customer-receipts/new/",
            {
                "customer": self.customer.id,
                "receipt_date": "2026/7/4",
                "receipt_amount": "120.00",
                "receipt_method": CustomerReceipt.ReceiptMethod.TRANSFER,
                "remark": "页面创建",
            },
        )

        receipt = CustomerReceipt.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-receipts/{receipt.id}/")
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertEqual(receipt.receipt_date, date(2026, 7, 4))
        self.assertEqual(receipt.receipt_amount, Decimal("120.00"))
        self.assertEqual(receipt.unallocated_amount, Decimal("120.00"))
        self.assertEqual(receipt.created_by, self.user)

    def test_customer_receipt_create_requires_amount_and_process_permissions(self):
        self.client.force_login(self.user)
        payload = {
            "customer": self.customer.id,
            "receipt_date": timezone.localdate().isoformat(),
            "receipt_amount": "120.00",
            "receipt_method": CustomerReceipt.ReceiptMethod.TRANSFER,
        }

        no_permission_response = self.client.post("/finance/customer-receipts/new/", payload)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        amount_only_response = self.client.post("/finance/customer-receipts/new/", payload)

        self.assertEqual(no_permission_response.status_code, 403)
        self.assertEqual(amount_only_response.status_code, 403)
        self.assertFalse(CustomerReceipt.objects.exists())

    def test_customer_receipt_edit_updates_pending_receipt_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-EDIT",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            unallocated_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
            receipt_method=CustomerReceipt.ReceiptMethod.CASH,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/edit/",
            {
                "customer": self.customer.id,
                "receipt_date": timezone.localdate().isoformat(),
                "receipt_amount": "88.50",
                "receipt_method": CustomerReceipt.ReceiptMethod.TRANSFER,
                "remark": "改金额",
                "operation_reason": "银行到账金额修正",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-receipts/{receipt.id}/")
        receipt.refresh_from_db()
        self.assertEqual(receipt.receipt_amount, Decimal("88.50"))
        self.assertEqual(receipt.unallocated_amount, Decimal("88.50"))
        self.assertEqual(receipt.receipt_method, CustomerReceipt.ReceiptMethod.TRANSFER)
        self.assertEqual(receipt.handled_by, self.user)
        audit_log = AuditLog.objects.get(action="customer_receipt_update", source_doc_id=receipt.id)
        self.assertEqual(audit_log.before_snapshot["receipt_amount"], "100.00")
        self.assertEqual(audit_log.after_snapshot["receipt_amount"], "88.50")
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "银行到账金额修正")

    def test_customer_receipt_voids_pending_receipt_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-VOID",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            unallocated_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(f"/finance/customer-receipts/{receipt.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-receipts/{receipt.id}/")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.VOIDED)
        audit_log = AuditLog.objects.get(action="customer_receipt_void", source_doc_id=receipt.id)
        self.assertEqual(audit_log.before_snapshot["status"], CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertEqual(audit_log.after_snapshot["status"], CustomerReceipt.Status.VOIDED)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "测试作废")

    def test_customer_receipt_void_requires_reason(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-VOID-NO-REASON",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            unallocated_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/void/",
            {"current_password": "x", "void_reason": ""},
            follow=True,
        )

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertContains(response, "请填写客户收款单作废原因")
        self.assertFalse(AuditLog.objects.filter(action="customer_receipt_void", source_doc_id=receipt.id).exists())

    def test_customer_receipt_edit_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-EDIT-DENIED",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/edit/",
            {
                "customer": self.customer.id,
                "receipt_date": timezone.localdate().isoformat(),
                "receipt_amount": "88.50",
                "receipt_method": CustomerReceipt.ReceiptMethod.TRANSFER,
            },
        )

        self.assertEqual(response.status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.receipt_amount, Decimal("100.00"))

    def test_customer_receipt_confirm_view_allocates_order(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-PAGE",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/confirm/",
            {"sales_order_id": [str(order.id)], "allocated_amount": ["100.00"], "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-receipts/{receipt.id}/")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.CONFIRMED)
        self.assertEqual(CustomerReceiptAllocation.objects.get(customer_receipt=receipt).allocated_amount, Decimal("100.00"))

    def test_customer_receipt_confirm_view_allocates_reconciliation(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-CUST-PAGE",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("65.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-PAGE",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("65.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/confirm/",
            {"reconciliation_id": [str(reconciliation.id)], "reconciliation_allocated_amount": ["65.00"], "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-receipts/{receipt.id}/")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.CONFIRMED)
        allocation = CustomerReceiptAllocation.objects.get(customer_receipt=receipt)
        self.assertEqual(allocation.reconciliation, reconciliation)
        self.assertEqual(allocation.allocation_type, CustomerReceiptAllocation.AllocationType.RECONCILIATION)

    def test_customer_receipt_detail_shows_available_and_suggested_allocations(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        older_order = self._sales_order("SO-ALLOC-1", Decimal("100.00"))
        newer_order = self._sales_order("SO-ALLOC-2", Decimal("60.00"))
        previous_receipt = CustomerReceipt.objects.create(
            receipt_no="RC-ALLOC-OLD",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("20.00"),
            status=CustomerReceipt.Status.CONFIRMED,
        )
        CustomerReceiptAllocation.objects.create(
            customer_receipt=previous_receipt,
            sales_order=newer_order,
            allocated_amount=Decimal("20.00"),
            allocation_type=CustomerReceiptAllocation.AllocationType.SALES_ORDER,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-ALLOC",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("70.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.get(f"/finance/customer-receipts/{receipt.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "可核销金额")
        self.assertContains(response, newer_order.sales_order_no)
        self.assertContains(response, older_order.sales_order_no)
        self.assertContains(response, "40.00")
        self.assertContains(response, 'value="40.00"')
        self.assertContains(response, 'value="30.00"')

    def test_customer_receipt_detail_shows_available_reconciliation_allocation(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-CUST-TARGET",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("100.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        previous_receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-TARGET-OLD",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("35.00"),
            status=CustomerReceipt.Status.CONFIRMED,
        )
        CustomerReceiptAllocation.objects.create(
            customer_receipt=previous_receipt,
            reconciliation=reconciliation,
            allocated_amount=Decimal("35.00"),
            allocation_type=CustomerReceiptAllocation.AllocationType.RECONCILIATION,
        )
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-TARGET",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("80.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.get(f"/finance/customer-receipts/{receipt.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已确认对账单")
        self.assertContains(response, reconciliation.reconciliation_no)
        self.assertContains(response, "65.00")
        self.assertContains(response, 'name="reconciliation_allocated_amount" value="65.00"')

    def test_customer_receipt_confirm_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-DENIED",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/confirm/",
            {"sales_order_id": [str(order.id)], "allocated_amount": ["100.00"]},
        )

        self.assertEqual(response.status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertFalse(CustomerReceiptAllocation.objects.filter(customer_receipt=receipt).exists())

    def test_customer_receipt_confirm_requires_second_verify_password(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        order = self._sales_order()
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-SECOND-VERIFY",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("100.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/customer-receipts/{receipt.id}/confirm/",
            {
                "sales_order_id": [str(order.id)],
                "allocated_amount": ["100.00"],
                "current_password": "wrong-password",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "二次验证失败")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)
        self.assertFalse(CustomerReceiptAllocation.objects.filter(customer_receipt=receipt).exists())

    def test_customer_credit_balance_apply_view_closes_balance(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        balance = CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-CB",
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=CustomerCreditBalance.Status.PENDING,
        )

        response = self.client.post(
            f"/finance/customer-balances/{balance.id}/apply/",
            {
                "action_type": CustomerCreditBalanceTransaction.ActionType.CLOSE,
                "amount": "20.00",
                "reason": "不再处理",
                "current_password": "x",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/customer-balances/{balance.id}/")
        balance.refresh_from_db()
        self.assertEqual(balance.status, CustomerCreditBalance.Status.CLOSED)
        self.assertEqual(balance.remaining_amount, Decimal("0.00"))

    def test_customer_credit_balance_apply_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        balance = CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-CB-DENIED",
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=CustomerCreditBalance.Status.PENDING,
        )

        response = self.client.post(
            f"/finance/customer-balances/{balance.id}/apply/",
            {
                "action_type": CustomerCreditBalanceTransaction.ActionType.CLOSE,
                "amount": "20.00",
                "reason": "不再处理",
                "current_password": "x",
            },
        )

        self.assertEqual(response.status_code, 403)
        balance.refresh_from_db()
        self.assertEqual(balance.status, CustomerCreditBalance.Status.PENDING)
        self.assertEqual(balance.remaining_amount, Decimal("20.00"))
        self.assertFalse(CustomerCreditBalanceTransaction.objects.filter(credit_balance=balance).exists())

    def test_customer_credit_balance_print_masks_amount_and_logs(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        balance = CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-CB-PRINT",
            balance_amount=Decimal("123.45"),
            remaining_amount=Decimal("123.45"),
            status=CustomerCreditBalance.Status.PENDING,
        )
        CustomerCreditBalanceTransaction.objects.create(
            transaction_no="CB-TXN-PRINT",
            credit_balance=balance,
            action_type=CustomerCreditBalanceTransaction.ActionType.CLOSE,
            amount=Decimal("23.45"),
            target_doc_no="TARGET-CB",
            reason="打印测试",
            idempotency_key="cb-print",
            created_by=self.user,
        )

        detail_response = self.client.get(f"/finance/customer-balances/{balance.id}/")
        response = self.client.get(f"/finance/customer-balances/{balance.id}/print/")

        self.assertContains(detail_response, f"/finance/customer-balances/{balance.id}/print/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "客户待处理余额单")
        self.assertContains(response, balance.source_doc_no)
        self.assertContains(response, "123.45")
        self.assertContains(response, "23.45")
        print_log = PrintLog.objects.get(source_doc_type="customer_credit_balance", source_doc_id=balance.id)
        self.assertEqual(print_log.template_type, "customer_credit_balance")
        self.assertEqual(print_log.source_doc_no, balance.source_doc_no)

    def test_customer_credit_balance_detail_shows_attachment_panel_with_amount_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        balance = CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-CB-ATT",
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=CustomerCreditBalance.Status.PENDING,
        )
        Attachment.objects.create(
            attachment_no="ATT-CB-001",
            source_doc_type="customer_credit_balance",
            source_doc_id=balance.id,
            source_doc_no=balance.source_doc_no,
            original_filename="customer-balance.pdf",
            stored_filename="customer-balance.pdf",
            file_path="attachments/customer-balance.pdf",
            file_size=100,
            uploaded_by=self.user,
        )

        response = self.client.get(f"/finance/customer-balances/{balance.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "customer-balance.pdf")
        self.assertContains(response, 'name="source_doc_type" value="customer_credit_balance"')

    def test_customer_credit_balance_export_masks_amount_and_filter_matches_list(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="CB-FILTER-KEEP",
            balance_amount=Decimal("123.45"),
            remaining_amount=Decimal("123.45"),
            status=CustomerCreditBalance.Status.PENDING,
        )
        CustomerCreditBalance.objects.create(
            customer=self.customer,
            source_doc_type="manual",
            source_doc_id=2,
            source_doc_no="CB-FILTER-HIDE",
            balance_amount=Decimal("99.99"),
            remaining_amount=Decimal("99.99"),
            status=CustomerCreditBalance.Status.CLOSED,
        )

        list_response = self.client.get("/finance/customer-balances/?q=KEEP&status=pending")
        export_response = self.client.get("/finance/customer-balances/export/?q=KEEP&status=pending")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "CB-FILTER-KEEP")
        self.assertNotContains(list_response, "CB-FILTER-HIDE")
        self.assertContains(list_response, "/finance/customer-balances/export/?q=KEEP&amp;status=pending")
        self.assertIn("客户,来源单号,余额,状态,创建时间", content)
        self.assertIn("CB-FILTER-KEEP", content)
        self.assertNotIn("CB-FILTER-HIDE", content)
        self.assertIn("123.45", content)
        export_log = ExportLog.objects.get(module="customer_credit_balances")
        self.assertEqual(export_log.row_count, 1)

    def test_customer_reconciliation_create_detail_confirm_and_void(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        order = self._sales_order("SO-REC", Decimal("100.00"))
        previous_receipt = CustomerReceipt.objects.create(
            receipt_no="RC-REC-OLD",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("30.00"),
            status=CustomerReceipt.Status.CONFIRMED,
        )
        CustomerReceiptAllocation.objects.create(
            customer_receipt=previous_receipt,
            sales_order=order,
            allocated_amount=Decimal("30.00"),
            allocation_type=CustomerReceiptAllocation.AllocationType.SALES_ORDER,
        )

        create_response = self.client.post(
            "/finance/reconciliations/new/",
            {
                "party_type": Reconciliation.PartyType.CUSTOMER,
                "customer": self.customer.id,
                "period_start": "2026年07月04日",
                "period_end": "2026年07月04日",
                "remark": "本月客户对账",
            },
        )

        reconciliation = Reconciliation.objects.get()
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(create_response["Location"], f"/finance/reconciliations/{reconciliation.id}/")
        self.assertEqual(reconciliation.total_amount, Decimal("70.00"))
        self.assertEqual(reconciliation.status, Reconciliation.Status.DRAFT)

        detail_response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/")
        self.assertContains(detail_response, order.sales_order_no)
        self.assertContains(detail_response, "70.00")
        self.assertContains(detail_response, f"/finance/reconciliations/{reconciliation.id}/print/")

        print_response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/print/")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, reconciliation.reconciliation_no)
        self.assertContains(print_response, "对账单")
        self.assertContains(print_response, order.sales_order_no)
        self.assertContains(print_response, "70.00")
        print_log = PrintLog.objects.get(source_doc_type="reconciliation", source_doc_id=reconciliation.id)
        self.assertEqual(print_log.template_type, "reconciliation")
        self.assertEqual(print_log.source_doc_no, reconciliation.reconciliation_no)

        confirm_response = self.client.post(
            f"/finance/reconciliations/{reconciliation.id}/confirm/",
            {"current_password": "x"},
        )
        self.assertEqual(confirm_response.status_code, 302)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, Reconciliation.Status.CONFIRMED)
        snapshot_item = ReconciliationItem.objects.get(reconciliation=reconciliation)
        self.assertEqual(snapshot_item.line_no, 1)
        self.assertEqual(snapshot_item.source_type, ReconciliationItem.SourceType.SALES_ORDER)
        self.assertEqual(snapshot_item.source_doc_id, order.id)
        self.assertEqual(snapshot_item.source_no, order.sales_order_no)
        self.assertEqual(snapshot_item.gross_amount, Decimal("100.00"))
        self.assertEqual(snapshot_item.allocated_amount, Decimal("30.00"))
        self.assertEqual(snapshot_item.open_amount, Decimal("70.00"))
        self.assertTrue(AuditLog.objects.filter(action="reconciliation_confirm", source_doc_id=reconciliation.id).exists())

        order.items.update(line_amount=Decimal("200.00"))
        order.total_amount = Decimal("200.00")
        order.save(update_fields=["total_amount"])
        confirmed_detail_response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/")
        self.assertContains(confirmed_detail_response, "明细合计")
        self.assertContains(confirmed_detail_response, "70.00")
        self.assertNotContains(confirmed_detail_response, "170.00")

        void_response = self.client.post(f"/finance/reconciliations/{reconciliation.id}/void/", {"current_password": "x", "void_reason": "测试作废"})
        self.assertEqual(void_response.status_code, 302)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, Reconciliation.Status.VOIDED)
        self.assertTrue(AuditLog.objects.filter(action="reconciliation_void", source_doc_id=reconciliation.id).exists())

    def test_supplier_reconciliation_create_sums_open_receipts(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = self._purchase_receipt_with_no("GR-REC", "PO-REC", Decimal("100.00"))
        previous_payment = SupplierPayment.objects.create(
            payment_no="PY-REC-OLD",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("25.00"),
            status=SupplierPayment.Status.CONFIRMED,
        )
        SupplierPaymentAllocation.objects.create(
            supplier_payment=previous_payment,
            purchase_receipt=receipt,
            allocated_amount=Decimal("25.00"),
            allocation_type=SupplierPaymentAllocation.AllocationType.PURCHASE_RECEIPT,
        )

        response = self.client.post(
            "/finance/reconciliations/new/",
            {
                "party_type": Reconciliation.PartyType.SUPPLIER,
                "supplier": self.supplier.id,
                "period_start": timezone.localdate().isoformat(),
                "period_end": timezone.localdate().isoformat(),
                "remark": "供应商对账",
            },
        )

        reconciliation = Reconciliation.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(reconciliation.total_amount, Decimal("75.00"))
        detail_response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/")
        self.assertContains(detail_response, receipt.purchase_receipt_no)
        self.assertContains(detail_response, "75.00")

        confirm_response = self.client.post(
            f"/finance/reconciliations/{reconciliation.id}/confirm/",
            {"current_password": "x"},
        )
        self.assertEqual(confirm_response.status_code, 302)
        reconciliation.refresh_from_db()
        self.assertEqual(reconciliation.status, Reconciliation.Status.CONFIRMED)
        snapshot_item = ReconciliationItem.objects.get(reconciliation=reconciliation)
        self.assertEqual(snapshot_item.source_type, ReconciliationItem.SourceType.PURCHASE_RECEIPT)
        self.assertEqual(snapshot_item.source_doc_id, receipt.id)
        self.assertEqual(snapshot_item.source_no, receipt.purchase_receipt_no)
        self.assertEqual(snapshot_item.gross_amount, Decimal("100.00"))
        self.assertEqual(snapshot_item.allocated_amount, Decimal("25.00"))
        self.assertEqual(snapshot_item.open_amount, Decimal("75.00"))

        receipt.items.update(accepted_qty=Decimal("20.0000"))
        confirmed_detail_response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/")
        self.assertContains(confirmed_detail_response, "75.00")
        self.assertNotContains(confirmed_detail_response, "175.00")

    def test_reconciliation_detail_requires_amount_permission(self):
        self.client.force_login(self.user)
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-DENIED",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("10.00"),
            status=Reconciliation.Status.DRAFT,
        )

        response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/")

        self.assertEqual(response.status_code, 403)

    def test_reconciliation_print_requires_amount_permission(self):
        self.client.force_login(self.user)
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-PRINT-DENIED",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("10.00"),
            status=Reconciliation.Status.DRAFT,
        )

        response = self.client.get(f"/finance/reconciliations/{reconciliation.id}/print/")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PrintLog.objects.exists())

    def test_supplier_payment_detail_renders_reverse_action(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = self._purchase_receipt()
        payment = SupplierPayment.objects.create(
            payment_no="PY-VIEW",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        confirm_supplier_payment(payment.id, [{"purchase_receipt_id": receipt.id, "allocated_amount": "100.00"}], self.user.id, "py-view")

        response = self.client.get(f"/finance/supplier-payments/{payment.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, payment.payment_no)
        self.assertContains(response, "确认红冲")
        self.assertContains(response, receipt.purchase_receipt_no)

    def test_supplier_payment_reverse_view_creates_reversal(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = self._purchase_receipt()
        payment = SupplierPayment.objects.create(
            payment_no="PY-REV",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        confirm_supplier_payment(payment.id, [{"purchase_receipt_id": receipt.id, "allocated_amount": "100.00"}], self.user.id, "py-rev")

        response = self.client.post(
            f"/finance/supplier-payments/{payment.id}/reverse/",
            {"reversal_amount": "100.00", "reason": "录错", "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-payments/{payment.id}/")
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.REVERSED)
        self.assertEqual(SupplierPaymentReversal.objects.get(source_payment=payment).reversal_amount, Decimal("100.00"))

    def test_supplier_payment_reverse_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        receipt = self._purchase_receipt()
        payment = SupplierPayment.objects.create(
            payment_no="PY-REV-DENIED",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        confirm_supplier_payment(payment.id, [{"purchase_receipt_id": receipt.id, "allocated_amount": "100.00"}], self.user.id, "py-rev-denied")

        response = self.client.post(
            f"/finance/supplier-payments/{payment.id}/reverse/",
            {"reversal_amount": "100.00", "reason": "录错", "current_password": "x"},
        )

        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.CONFIRMED)
        self.assertFalse(SupplierPaymentReversal.objects.filter(source_payment=payment).exists())

    def test_supplier_payment_create_view_creates_pending_payment(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()

        response = self.client.post(
            "/finance/supplier-payments/new/",
            {
                "supplier": self.supplier.id,
                "payment_date": "2026.07.04",
                "payment_amount": "100.00",
                "payment_method": SupplierPayment.PaymentMethod.TRANSFER,
                "remark": "页面创建",
            },
        )

        payment = SupplierPayment.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-payments/{payment.id}/")
        self.assertEqual(payment.status, SupplierPayment.Status.PENDING_APPROVAL)
        self.assertEqual(payment.payment_date, date(2026, 7, 4))
        self.assertEqual(payment.payment_amount, Decimal("100.00"))
        self.assertEqual(payment.unallocated_amount, Decimal("100.00"))
        self.assertEqual(payment.created_by, self.user)

    def test_supplier_payment_import_template_downloads_csv(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()

        response = self.client.get("/finance/supplier-payments/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("付款单号,供应商编号,付款日期,付款金额,付款方式,备注", content)
        self.assertIn("PY-INIT-001", content)

    def test_supplier_payment_import_creates_pending_payment_without_allocation(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        upload = SimpleUploadedFile(
            "supplier_payments.csv",
            (
                "付款单号,供应商编号,付款日期,付款金额,付款方式,备注\n"
                "PY-IMP-001,S001,2026-06-10,66.25,cash,导入付款\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/finance/supplier-payments/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/finance/supplier-payments/")
        payment = SupplierPayment.objects.get(payment_no="PY-IMP-001")
        self.assertEqual(payment.supplier, self.supplier)
        self.assertEqual(payment.status, SupplierPayment.Status.PENDING_APPROVAL)
        self.assertEqual(payment.payment_amount, Decimal("66.25"))
        self.assertEqual(payment.unallocated_amount, Decimal("66.25"))
        self.assertEqual(payment.payment_method, SupplierPayment.PaymentMethod.CASH)
        self.assertEqual(payment.created_by, self.user)
        self.assertFalse(SupplierPaymentAllocation.objects.filter(supplier_payment=payment).exists())
        job = ImportJob.objects.get(template_type="supplier_payments")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_supplier_payment_import_reports_validation_errors(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        SupplierPayment.objects.create(
            payment_no="PY-DUP",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("10.00"),
            unallocated_amount=Decimal("10.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        upload = SimpleUploadedFile(
            "supplier_payments.csv",
            (
                "付款单号,供应商编号,付款日期,付款金额,付款方式,备注\n"
                "PY-DUP,S-MISSING,bad-date,-1,bad-method,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/finance/supplier-payments/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "供应商付款单号已存在")
        self.assertContains(response, "供应商不存在或未启用")
        self.assertContains(response, "付款日期格式错误")
        self.assertContains(response, "付款金额必须大于 0")
        self.assertContains(response, "付款方式必须是 cash、transfer、check 或 other")
        job = ImportJob.objects.get(template_type="supplier_payments")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertEqual(SupplierPayment.objects.filter(payment_no="PY-DUP").count(), 1)

    def test_supplier_payment_import_requires_amount_and_process_permissions(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/finance/supplier-payments/import-template/")
        import_response = self.client.get("/finance/supplier-payments/import/")
        list_response = self.client.get("/finance/supplier-payments/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)
        self.assertEqual(list_response.status_code, 403)

        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        list_response = self.client.get("/finance/supplier-payments/")
        self.assertNotContains(list_response, "/finance/supplier-payments/import/")

    def test_supplier_payment_create_requires_amount_and_process_permissions(self):
        self.client.force_login(self.user)
        payload = {
            "supplier": self.supplier.id,
            "payment_date": timezone.localdate().isoformat(),
            "payment_amount": "100.00",
            "payment_method": SupplierPayment.PaymentMethod.TRANSFER,
        }

        no_permission_response = self.client.post("/finance/supplier-payments/new/", payload)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        amount_only_response = self.client.post("/finance/supplier-payments/new/", payload)

        self.assertEqual(no_permission_response.status_code, 403)
        self.assertEqual(amount_only_response.status_code, 403)
        self.assertFalse(SupplierPayment.objects.exists())

    def test_supplier_payment_edit_updates_pending_payment_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        payment = SupplierPayment.objects.create(
            payment_no="PY-EDIT",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            unallocated_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
            payment_method=SupplierPayment.PaymentMethod.CASH,
        )

        response = self.client.post(
            f"/finance/supplier-payments/{payment.id}/edit/",
            {
                "supplier": self.supplier.id,
                "payment_date": timezone.localdate().isoformat(),
                "payment_amount": "66.25",
                "payment_method": SupplierPayment.PaymentMethod.TRANSFER,
                "remark": "改付款",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-payments/{payment.id}/")
        payment.refresh_from_db()
        self.assertEqual(payment.payment_amount, Decimal("66.25"))
        self.assertEqual(payment.unallocated_amount, Decimal("66.25"))
        self.assertEqual(payment.payment_method, SupplierPayment.PaymentMethod.TRANSFER)
        self.assertEqual(payment.handled_by, self.user)
        audit_log = AuditLog.objects.get(action="supplier_payment_update", source_doc_id=payment.id)
        self.assertEqual(audit_log.before_snapshot["payment_amount"], "100.00")
        self.assertEqual(audit_log.after_snapshot["payment_amount"], "66.25")

    def test_supplier_payment_voids_pending_payment_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        payment = SupplierPayment.objects.create(
            payment_no="PY-VOID",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            unallocated_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        response = self.client.post(f"/finance/supplier-payments/{payment.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-payments/{payment.id}/")
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.VOIDED)
        audit_log = AuditLog.objects.get(action="supplier_payment_void", source_doc_id=payment.id)
        self.assertEqual(audit_log.before_snapshot["status"], SupplierPayment.Status.PENDING_APPROVAL)
        self.assertEqual(audit_log.after_snapshot["status"], SupplierPayment.Status.VOIDED)

    def test_supplier_payment_void_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        payment = SupplierPayment.objects.create(
            payment_no="PY-VOID-DENIED",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        response = self.client.post(f"/finance/supplier-payments/{payment.id}/void/")

        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.PENDING_APPROVAL)

    def test_supplier_payment_confirm_view_allocates_receipt(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        receipt = self._purchase_receipt()
        payment = SupplierPayment.objects.create(
            payment_no="PY-PAGE",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/supplier-payments/{payment.id}/confirm/",
            {"purchase_receipt_id": [str(receipt.id)], "allocated_amount": ["100.00"], "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-payments/{payment.id}/")
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.CONFIRMED)
        self.assertEqual(SupplierPaymentAllocation.objects.get(supplier_payment=payment).allocated_amount, Decimal("100.00"))

    def test_supplier_payment_confirm_view_allocates_reconciliation(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-SUP-PAGE",
            party_type=Reconciliation.PartyType.SUPPLIER,
            supplier=self.supplier,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("55.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        payment = SupplierPayment.objects.create(
            payment_no="PY-REC-PAGE",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("55.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        response = self.client.post(
            f"/finance/supplier-payments/{payment.id}/confirm/",
            {"reconciliation_id": [str(reconciliation.id)], "reconciliation_allocated_amount": ["55.00"], "current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-payments/{payment.id}/")
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.CONFIRMED)
        allocation = SupplierPaymentAllocation.objects.get(supplier_payment=payment)
        self.assertEqual(allocation.reconciliation, reconciliation)
        self.assertEqual(allocation.allocation_type, SupplierPaymentAllocation.AllocationType.RECONCILIATION)

    def test_supplier_payment_export_masks_amount_and_filter_matches_list(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        SupplierPayment.objects.create(
            payment_no="PY-FILTER-KEEP",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("123.45"),
            unallocated_amount=Decimal("23.45"),
            status=SupplierPayment.Status.CONFIRMED,
        )
        SupplierPayment.objects.create(
            payment_no="PY-FILTER-HIDE",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("99.99"),
            unallocated_amount=Decimal("9.99"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        list_response = self.client.get("/finance/supplier-payments/?q=KEEP&status=confirmed")
        export_response = self.client.get("/finance/supplier-payments/export/?q=KEEP&status=confirmed")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "PY-FILTER-KEEP")
        self.assertNotContains(list_response, "PY-FILTER-HIDE")
        self.assertContains(list_response, "/finance/supplier-payments/export/?q=KEEP&amp;status=confirmed")
        self.assertIn("付款单号,供应商,付款日期,金额,未分配,状态", content)
        self.assertIn("PY-FILTER-KEEP", content)
        self.assertNotIn("PY-FILTER-HIDE", content)
        self.assertIn("123.45", content)
        export_log = ExportLog.objects.get(module="supplier_payments")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["status"], "confirmed")

    def test_supplier_payment_print_masks_amount_and_records_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        receipt = self._purchase_receipt()
        payment = SupplierPayment.objects.create(
            payment_no="PY-PRINT",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            unallocated_amount=Decimal("0.00"),
            status=SupplierPayment.Status.CONFIRMED,
            handled_by=self.user,
            created_by=self.user,
        )
        SupplierPaymentAllocation.objects.create(
            supplier_payment=payment,
            purchase_receipt=receipt,
            allocated_amount=Decimal("100.00"),
            allocation_type=SupplierPaymentAllocation.AllocationType.PURCHASE_RECEIPT,
            created_by=self.user,
        )

        detail_response = self.client.get(f"/finance/supplier-payments/{payment.id}/")
        response = self.client.get(f"/finance/supplier-payments/{payment.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "供应商付款凭证")
        self.assertContains(response, "PY-PRINT")
        self.assertContains(response, receipt.purchase_receipt_no)
        self.assertContains(response, "100.00")
        print_log = PrintLog.objects.get(source_doc_type="supplier_payment", source_doc_id=payment.id)
        self.assertEqual(print_log.template_type, "supplier_payment")
        self.assertEqual(print_log.source_doc_no, payment.payment_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_supplier_payment_detail_shows_available_and_suggested_allocations(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        older_receipt = self._purchase_receipt_with_no("GR-ALLOC-1", "PO-ALLOC-1", Decimal("100.00"))
        newer_receipt = self._purchase_receipt_with_no("GR-ALLOC-2", "PO-ALLOC-2", Decimal("80.00"))
        previous_payment = SupplierPayment.objects.create(
            payment_no="PY-ALLOC-OLD",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("30.00"),
            status=SupplierPayment.Status.CONFIRMED,
        )
        SupplierPaymentAllocation.objects.create(
            supplier_payment=previous_payment,
            purchase_receipt=newer_receipt,
            allocated_amount=Decimal("30.00"),
            allocation_type=SupplierPaymentAllocation.AllocationType.PURCHASE_RECEIPT,
        )
        payment = SupplierPayment.objects.create(
            payment_no="PY-ALLOC",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("120.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        response = self.client.get(f"/finance/supplier-payments/{payment.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "可核销金额")
        self.assertContains(response, newer_receipt.purchase_receipt_no)
        self.assertContains(response, older_receipt.purchase_receipt_no)
        self.assertContains(response, "50.00")
        self.assertContains(response, 'value="50.00"')
        self.assertContains(response, 'value="70.00"')

    def test_supplier_payment_detail_shows_available_reconciliation_allocation(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-SUP-TARGET",
            party_type=Reconciliation.PartyType.SUPPLIER,
            supplier=self.supplier,
            period_start=timezone.localdate(),
            period_end=timezone.localdate(),
            total_amount=Decimal("90.00"),
            status=Reconciliation.Status.CONFIRMED,
        )
        previous_payment = SupplierPayment.objects.create(
            payment_no="PY-REC-TARGET-OLD",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("20.00"),
            status=SupplierPayment.Status.CONFIRMED,
        )
        SupplierPaymentAllocation.objects.create(
            supplier_payment=previous_payment,
            reconciliation=reconciliation,
            allocated_amount=Decimal("20.00"),
            allocation_type=SupplierPaymentAllocation.AllocationType.RECONCILIATION,
        )
        payment = SupplierPayment.objects.create(
            payment_no="PY-REC-TARGET",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("100.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )

        response = self.client.get(f"/finance/supplier-payments/{payment.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已确认对账单")
        self.assertContains(response, reconciliation.reconciliation_no)
        self.assertContains(response, "70.00")
        self.assertContains(response, 'name="reconciliation_allocated_amount" value="70.00"')

    def test_supplier_credit_balance_apply_view_closes_balance(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        balance = SupplierCreditBalance.objects.create(
            supplier=self.supplier,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-SB",
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=SupplierCreditBalance.Status.PENDING,
        )

        response = self.client.post(
            f"/finance/supplier-balances/{balance.id}/apply/",
            {
                "action_type": SupplierCreditBalanceTransaction.ActionType.CLOSE,
                "amount": "20.00",
                "reason": "不再处理",
                "current_password": "x",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/supplier-balances/{balance.id}/")
        balance.refresh_from_db()
        self.assertEqual(balance.status, SupplierCreditBalance.Status.CLOSED)
        self.assertEqual(balance.remaining_amount, Decimal("0.00"))

    def test_supplier_credit_balance_apply_requires_payment_process_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        balance = SupplierCreditBalance.objects.create(
            supplier=self.supplier,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-SB-DENIED",
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=SupplierCreditBalance.Status.PENDING,
        )

        response = self.client.post(
            f"/finance/supplier-balances/{balance.id}/apply/",
            {
                "action_type": SupplierCreditBalanceTransaction.ActionType.CLOSE,
                "amount": "20.00",
                "reason": "不再处理",
                "current_password": "x",
            },
        )

        self.assertEqual(response.status_code, 403)
        balance.refresh_from_db()
        self.assertEqual(balance.status, SupplierCreditBalance.Status.PENDING)
        self.assertEqual(balance.remaining_amount, Decimal("20.00"))
        self.assertFalse(SupplierCreditBalanceTransaction.objects.filter(credit_balance=balance).exists())

    def test_supplier_credit_balance_detail_shows_attachment_panel_with_amount_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        balance = SupplierCreditBalance.objects.create(
            supplier=self.supplier,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-SB-ATT",
            balance_amount=Decimal("20.00"),
            remaining_amount=Decimal("20.00"),
            status=SupplierCreditBalance.Status.PENDING,
        )
        Attachment.objects.create(
            attachment_no="ATT-SB-001",
            source_doc_type="supplier_credit_balance",
            source_doc_id=balance.id,
            source_doc_no=balance.source_doc_no,
            original_filename="supplier-balance.pdf",
            stored_filename="supplier-balance.pdf",
            file_path="attachments/supplier-balance.pdf",
            file_size=100,
            uploaded_by=self.user,
        )

        response = self.client.get(f"/finance/supplier-balances/{balance.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "supplier-balance.pdf")
        self.assertContains(response, 'name="source_doc_type" value="supplier_credit_balance"')

    def test_supplier_credit_balance_print_shows_amount_with_permission_and_logs(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        balance = SupplierCreditBalance.objects.create(
            supplier=self.supplier,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL-SB-PRINT",
            balance_amount=Decimal("123.45"),
            remaining_amount=Decimal("123.45"),
            status=SupplierCreditBalance.Status.PENDING,
        )
        SupplierCreditBalanceTransaction.objects.create(
            transaction_no="SB-TXN-PRINT",
            credit_balance=balance,
            action_type=SupplierCreditBalanceTransaction.ActionType.CLOSE,
            amount=Decimal("23.45"),
            target_doc_no="TARGET-SB",
            reason="打印测试",
            idempotency_key="sb-print",
            created_by=self.user,
        )

        detail_response = self.client.get(f"/finance/supplier-balances/{balance.id}/")
        response = self.client.get(f"/finance/supplier-balances/{balance.id}/print/")

        self.assertContains(detail_response, f"/finance/supplier-balances/{balance.id}/print/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "供应商待处理余额单")
        self.assertContains(response, balance.source_doc_no)
        self.assertContains(response, "123.45")
        self.assertContains(response, "23.45")
        print_log = PrintLog.objects.get(source_doc_type="supplier_credit_balance", source_doc_id=balance.id)
        self.assertEqual(print_log.template_type, "supplier_credit_balance")
        self.assertEqual(print_log.source_doc_no, balance.source_doc_no)

    def test_supplier_credit_balance_export_masks_amount_and_filter_matches_list(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        SupplierCreditBalance.objects.create(
            supplier=self.supplier,
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="SB-FILTER-KEEP",
            balance_amount=Decimal("123.45"),
            remaining_amount=Decimal("123.45"),
            status=SupplierCreditBalance.Status.PENDING,
        )
        SupplierCreditBalance.objects.create(
            supplier=self.supplier,
            source_doc_type="manual",
            source_doc_id=2,
            source_doc_no="SB-FILTER-HIDE",
            balance_amount=Decimal("99.99"),
            remaining_amount=Decimal("99.99"),
            status=SupplierCreditBalance.Status.CLOSED,
        )

        list_response = self.client.get("/finance/supplier-balances/?q=KEEP&status=pending")
        export_response = self.client.get("/finance/supplier-balances/export/?q=KEEP&status=pending")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "SB-FILTER-KEEP")
        self.assertNotContains(list_response, "SB-FILTER-HIDE")
        self.assertContains(list_response, "/finance/supplier-balances/export/?q=KEEP&amp;status=pending")
        self.assertIn("供应商,来源单号,余额,状态,创建时间", content)
        self.assertIn("SB-FILTER-KEEP", content)
        self.assertNotIn("SB-FILTER-HIDE", content)
        self.assertIn("123.45", content)
        export_log = ExportLog.objects.get(module="supplier_credit_balances")
        self.assertEqual(export_log.row_count, 1)

    def test_opening_receivable_create_page_and_receipt_detail_target(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()

        response = self.client.post(
            "/finance/opening-receivables/new/",
            {
                "customer": str(self.customer.id),
                "source_doc_no": "OLD-SO-PAGE",
                "opening_date": timezone.localdate().isoformat(),
                "opening_amount": "88.00",
                "remark": "期初导入",
            },
        )

        opening = OpeningReceivable.objects.get(source_doc_no="OLD-SO-PAGE")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/opening-receivables/{opening.id}/")
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-OPEN-PAGE",
            customer=self.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("88.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        detail_response = self.client.get(f"/finance/customer-receipts/{receipt.id}/")
        self.assertContains(detail_response, "期初应收")
        self.assertContains(detail_response, opening.opening_no)

    def test_opening_payable_create_page_and_payment_detail_target(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()

        response = self.client.post(
            "/finance/opening-payables/new/",
            {
                "supplier": str(self.supplier.id),
                "source_doc_no": "OLD-GR-PAGE",
                "opening_date": timezone.localdate().isoformat(),
                "opening_amount": "66.00",
                "remark": "期初导入",
            },
        )

        opening = OpeningPayable.objects.get(source_doc_no="OLD-GR-PAGE")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/finance/opening-payables/{opening.id}/")
        payment = SupplierPayment.objects.create(
            payment_no="PY-OPEN-PAGE",
            supplier=self.supplier,
            payment_date=timezone.localdate(),
            payment_amount=Decimal("66.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        detail_response = self.client.get(f"/finance/supplier-payments/{payment.id}/")
        self.assertContains(detail_response, "期初应付")
        self.assertContains(detail_response, opening.opening_no)

    def test_expense_record_create_confirm_and_void(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        create_response = self.client.post(
            "/finance/expenses/new/",
            {
                "expense_date": timezone.localdate().isoformat(),
                "category": ExpenseRecord.ExpenseCategory.FREIGHT,
                "amount": "32.50",
                "payment_method": ExpenseRecord.PaymentMethod.CASH,
                "payee": "物流公司",
                "invoice_no": "INV-001",
                "remark": "运费",
            },
        )
        expense = ExpenseRecord.objects.get(payee="物流公司")
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(expense.status, ExpenseRecord.Status.DRAFT)

        confirm_response = self.client.post(f"/finance/expenses/{expense.id}/confirm/", {"current_password": "x"})
        self.assertEqual(confirm_response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, ExpenseRecord.Status.CONFIRMED)
        self.assertEqual(expense.confirmed_by, self.user)

        void_response = self.client.post(
            f"/finance/expenses/{expense.id}/void/",
            {"current_password": "x", "void_reason": "录错"},
        )
        self.assertEqual(void_response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.status, ExpenseRecord.Status.VOIDED)

    def test_operations_dashboard_summarizes_cash_and_balances(self):
        self.client.force_login(self.user)
        self._grant_finance_process_permissions()
        today = timezone.localdate()
        sales_order = self._sales_order("SO-DASH-001", Decimal("300.00"))
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-DASH-001",
            customer=self.customer,
            receipt_date=today,
            receipt_amount=Decimal("120.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )
        confirm_customer_receipt(
            receipt.id,
            [{"sales_order_id": sales_order.id, "allocated_amount": "120.00"}],
            self.user.id,
            "rc-dashboard",
        )
        purchase_receipt = self._purchase_receipt(Decimal("80.00"))
        payment = SupplierPayment.objects.create(
            payment_no="PY-DASH-001",
            supplier=self.supplier,
            payment_date=today,
            payment_amount=Decimal("50.00"),
            status=SupplierPayment.Status.PENDING_APPROVAL,
        )
        confirm_supplier_payment(
            payment.id,
            [{"purchase_receipt_id": purchase_receipt.id, "allocated_amount": "50.00"}],
            self.user.id,
            "py-dashboard",
        )
        OpeningReceivable.objects.create(
            opening_no="OR-DASH-001",
            customer=self.customer,
            opening_date=today,
            opening_amount=Decimal("40.00"),
            remaining_amount=Decimal("40.00"),
            status=OpeningReceivable.Status.OPEN,
        )
        OpeningPayable.objects.create(
            opening_no="OP-DASH-001",
            supplier=self.supplier,
            opening_date=today,
            opening_amount=Decimal("25.00"),
            remaining_amount=Decimal("25.00"),
            status=OpeningPayable.Status.OPEN,
        )
        ExpenseRecord.objects.create(
            expense_no="EX-DASH-001",
            expense_date=today,
            category=ExpenseRecord.ExpenseCategory.FREIGHT,
            amount=Decimal("10.00"),
            payment_method=ExpenseRecord.PaymentMethod.CASH,
            payee="物流公司",
            status=ExpenseRecord.Status.CONFIRMED,
            confirmed_by=self.user,
            confirmed_at=timezone.now(),
        )

        response = self.client.get("/finance/operations/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "经营看板")
        self.assertContains(response, "现金净额")
        self.assertContains(response, "60.00")
        self.assertContains(response, "当前应收")
        self.assertContains(response, "220.00")
        self.assertContains(response, "当前应付")
        self.assertContains(response, "75.00")
        self.assertContains(response, "EX-DASH-001")

    def test_operations_dashboard_requires_amount_permission(self):
        self.client.force_login(self.user)

        response = self.client.get("/finance/operations/")

        self.assertEqual(response.status_code, 403)


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
