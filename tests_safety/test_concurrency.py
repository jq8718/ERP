import threading
import time
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.test import TransactionTestCase
from django.db import connection
from django.contrib.auth import get_user_model

User = get_user_model()


class ConcurrentDocumentNumberTest(TransactionTestCase):
    """Verify single number generation is safe under concurrency."""

    def test_no_duplicate_numbers_under_concurrency(self):
        if connection.vendor == "sqlite":
            self.skipTest("SQLite uses database-level write locks; row-lock concurrency is verified on PostgreSQL.")
        from system.services import next_document_no
        from system.models import DocumentSequence
        from datetime import date

        prefix = f"CNC_{int(time.time()) % 100000}"
        today = date.today()
        generated = set()
        errors = []
        lock = threading.Lock()

        def generate_one(idx):
            try:
                doc_no = next_document_no(prefix, today)
                with lock:
                    if doc_no in generated:
                        errors.append(f"Duplicate: {doc_no}")
                    generated.add(doc_no)
            except Exception as e:
                with lock:
                    errors.append(str(e))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(generate_one, i) for i in range(50)]
            for f in as_completed(futures):
                f.result()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(generated), 50)


class ConcurrentInventoryDeductionTest(TransactionTestCase):
    """Verify inventory is safe under concurrent deductions."""

    def setUp(self):
        from masterdata.models import Material
        self.rm = Material.objects.create(material_code=f"CDC_RM_{int(time.time()) % 100000}", material_name="CDC Material", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        self.loc = WarehouseLocation.objects.create(location_code=f"CDC_LOC_{int(time.time()) % 100000}", location_name="CDC Loc")
        self.batch = InventoryBatch.objects.create(batch_no=f"CDC_B_{int(time.time()) % 100000}", material=self.rm, location=self.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("100"), remaining_qty=Decimal("100"), batch_status="in_stock")
        self.inv = Inventory.objects.create(material=self.rm, location=self.loc, inventory_type="available", qty=Decimal("100"))

    def test_concurrent_deductions_do_not_exceed_available(self):
        if connection.vendor == "sqlite":
            self.skipTest("SQLite does not implement SELECT FOR UPDATE row locks.")
        from inventory.services import deduct_batch_inventory
        from inventory.exceptions import InventoryError

        errors = []
        successes = []
        lock = threading.Lock()

        def deduct_one(idx):
            try:
                result = deduct_batch_inventory(batch_id=self.batch.id, material_id=self.rm.id, location_id=self.loc.id, qty=Decimal("30"))
                with lock:
                    successes.append(idx)
            except InventoryError as e:
                with lock:
                    errors.append(str(e))
            except Exception as e:
                with lock:
                    errors.append(f"Unexpected: {e}")

        threads = []
        for i in range(5):
            t = threading.Thread(target=deduct_one, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        self.batch.refresh_from_db()
        self.inv.refresh_from_db()
        total_deducted = Decimal("30") * len(successes)
        self.assertLessEqual(total_deducted, Decimal("100"))
        self.assertGreaterEqual(self.batch.remaining_qty, Decimal("0"))
        self.assertGreaterEqual(self.inv.qty, Decimal("0"))


class ConcurrentPaymentAllocationTest(TransactionTestCase):
    """Verify payment allocations are safe under concurrency."""

    def setUp(self):
        from masterdata.models import Customer, CustomerProduct, Material
        self.cust = Customer.objects.create(customer_no=f"CPAY_C_{int(time.time()) % 100000}", customer_name="CPAY Customer", status="active")
        self.mat = Material.objects.create(material_code=f"CPAY_MAT_{int(time.time()) % 100000}", material_name="CPAY Material", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        self.cp = CustomerProduct.objects.create(customer=self.cust, customer_product_no=f"CPAY_CP_{int(time.time()) % 100000}", customer_product_name="CPAY CP", finished_material=self.mat, status="active")
        from sales.models import SalesOrder, SalesOrderItem
        self.so = SalesOrder.objects.create(sales_order_no=f"CPAY_SO_{int(time.time()) % 100000}", customer=self.cust, order_date="2026-06-10", status="confirmed", total_amount=Decimal("1000.00"))
        self.soi = SalesOrderItem.objects.create(sales_order=self.so, line_no=1, customer_product=self.cp, finished_material=self.mat, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="confirmed", inventory_check_status="sufficient")
        from finance.models import CustomerReceipt
        self.receipt = CustomerReceipt.objects.create(receipt_no=f"CPAY_RC_{int(time.time()) % 100000}", customer=self.cust, receipt_date="2026-06-10", receipt_amount=Decimal("1000.00"), unallocated_amount=Decimal("1000.00"), status="pending_approval")

    def test_direct_allocation_save_guard_prevents_over_allocation(self):
        from finance.models import CustomerReceiptAllocation
        from django.db.models import Sum

        CustomerReceiptAllocation.objects.create(
            customer_receipt=self.receipt,
            sales_order=self.so,
            allocated_amount=Decimal("500"),
            allocation_type="sales_order",
        )
        CustomerReceiptAllocation.objects.create(
            customer_receipt=self.receipt,
            sales_order=self.so,
            allocated_amount=Decimal("500"),
            allocation_type="sales_order",
        )
        try:
            CustomerReceiptAllocation.objects.create(
                customer_receipt=self.receipt,
                sales_order=self.so,
                allocated_amount=Decimal("1"),
                allocation_type="sales_order",
            )
        except Exception:
            pass

        total = CustomerReceiptAllocation.objects.filter(customer_receipt=self.receipt).aggregate(t=Sum("allocated_amount"))["t"] or Decimal("0")
        self.assertLessEqual(total, self.soi.line_amount)

    def test_concurrent_service_allocations_do_not_exceed_receivable(self):
        if connection.vendor == "sqlite":
            self.skipTest("SQLite does not implement SELECT FOR UPDATE row locks.")
        from finance.models import CustomerReceipt, CustomerReceiptAllocation
        from finance.services import confirm_customer_receipt
        from django.db.models import Sum

        receipts = [
            CustomerReceipt.objects.create(
                receipt_no=f"CPAY_RC_{int(time.time()) % 100000}_{idx}",
                customer=self.cust,
                receipt_date="2026-06-10",
                receipt_amount=Decimal("500.00"),
                unallocated_amount=Decimal("500.00"),
                status="pending_approval",
            )
            for idx in range(4)
        ]

        def allocate_receipt(receipt):
            confirm_customer_receipt(
                receipt.id,
                [{"sales_order_id": self.so.id, "allocated_amount": "500.00"}],
                operator_id=None,
                idempotency_key=f"cpay:{receipt.id}",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(allocate_receipt, receipt) for receipt in receipts]
            for future in as_completed(futures):
                future.result()

        total = CustomerReceiptAllocation.objects.filter(sales_order=self.so).aggregate(t=Sum("allocated_amount"))["t"] or Decimal("0")
        self.assertLessEqual(total, self.soi.line_amount)
