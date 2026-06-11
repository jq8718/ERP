import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Sum
from django.test import TransactionTestCase
from django.utils import timezone

from finance.models import CustomerReceipt, CustomerReceiptAllocation
from finance.services import confirm_customer_receipt
from inventory.exceptions import InventoryError
from inventory.models import Inventory, InventoryBatch, WarehouseLocation
from inventory.services import deduct_batch_inventory
from masterdata.models import Customer, CustomerProduct, Material
from sales.models import SalesOrder, SalesOrderItem
from system.services import next_document_no


User = get_user_model()


class PostgresRowLockGateTest(TransactionTestCase):
    """Production-equivalent row-lock tests. These are meaningful only on PostgreSQL."""

    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock gate requires a PostgreSQL test database.")
        suffix = int(time.time() * 1000) % 1000000
        self.customer = Customer.objects.create(customer_no=f"PGL-C-{suffix}", customer_name="PGL Customer")
        self.finished = Material.objects.create(
            material_code=f"PGL-FG-{suffix}",
            material_name="PGL Finished",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
            qty_precision=0,
            status=Material.MaterialStatus.ACTIVE,
        )
        self.raw = Material.objects.create(
            material_code=f"PGL-RM-{suffix}",
            material_name="PGL Raw",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
            qty_precision=3,
            status=Material.MaterialStatus.ACTIVE,
        )
        self.customer_product = CustomerProduct.objects.create(
            customer=self.customer,
            customer_product_no=f"PGL-CP-{suffix}",
            customer_product_name="PGL CP",
            finished_material=self.finished,
            status=CustomerProduct.ProductStatus.ACTIVE,
        )
        self.sales_order = SalesOrder.objects.create(
            sales_order_no=f"PGL-SO-{suffix}",
            customer=self.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.CONFIRMED,
            total_amount=Decimal("1000.00"),
        )
        SalesOrderItem.objects.create(
            sales_order=self.sales_order,
            line_no=1,
            customer_product=self.customer_product,
            finished_material=self.finished,
            order_qty=Decimal("10"),
            unit_price=Decimal("100.00"),
            line_amount=Decimal("1000.00"),
            line_status=SalesOrderItem.LineStatus.CONFIRMED,
            inventory_check_status=SalesOrderItem.InventoryCheckStatus.SUFFICIENT,
        )
        self.location = WarehouseLocation.objects.create(location_code=f"PGL-A-{suffix}", location_name="PGL A")
        self.batch = InventoryBatch.objects.create(
            batch_no=f"PGL-B-{suffix}",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at="2026-06-01T00:00:00Z",
            initial_qty=Decimal("100"),
            remaining_qty=Decimal("100"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        self.inventory = Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("100"),
        )

    def test_document_numbers_remain_unique_under_parallel_requests(self):
        prefix = f"PGL{int(time.time()) % 10000}"

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_run_and_close_connection, next_document_no, prefix) for _ in range(80)]
            generated = [future.result() for future in as_completed(futures)]

        self.assertEqual(len(generated), 80)
        self.assertEqual(len(set(generated)), 80)

    def test_inventory_deduction_row_locks_prevent_negative_stock(self):
        successes = []
        failures = []
        lock = threading.Lock()

        def deduct(idx):
            try:
                deduct_batch_inventory(
                    batch_id=self.batch.id,
                    material_id=self.raw.id,
                    location_id=self.location.id,
                    qty=Decimal("30"),
                )
                with lock:
                    successes.append(idx)
            except InventoryError as exc:
                with lock:
                    failures.append(exc.error_code)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(deduct, idx) for idx in range(5)]
            for future in as_completed(futures):
                future.result()

        self.batch.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertEqual(len(successes), 3)
        self.assertEqual(self.batch.remaining_qty, Decimal("10.0000"))
        self.assertEqual(self.inventory.qty, Decimal("10.0000"))
        self.assertTrue(all(code == "STOCK_NOT_ENOUGH" for code in failures))

    def test_customer_receipt_allocations_do_not_exceed_receivable_under_parallel_confirm(self):
        receipts = [
            CustomerReceipt.objects.create(
                receipt_no=f"PGL-RC-{int(time.time() * 1000) % 1000000}-{idx}",
                customer=self.customer,
                receipt_date=timezone.localdate(),
                receipt_amount=Decimal("500.00"),
                unallocated_amount=Decimal("500.00"),
                status=CustomerReceipt.Status.PENDING_APPROVAL,
            )
            for idx in range(4)
        ]

        def confirm(receipt):
            try:
                return confirm_customer_receipt(
                    receipt.id,
                    [{"sales_order_id": self.sales_order.id, "allocated_amount": "500.00"}],
                    operator_id=None,
                    idempotency_key=f"pgl-receipt-{receipt.id}",
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(confirm, receipt) for receipt in receipts]
            results = [future.result() for future in as_completed(futures)]

        allocated = (
            CustomerReceiptAllocation.objects.filter(sales_order=self.sales_order).aggregate(total=Sum("allocated_amount"))[
                "total"
            ]
            or Decimal("0")
        )
        self.assertEqual(allocated, Decimal("1000.00"))
        self.assertEqual(sum(1 for result in results if result.success), 2)
        self.assertTrue(
            all(result.success or result.error_code == "PAYMENT_ALLOCATION_OVER" for result in results),
            [result.error_code for result in results],
        )


def _run_and_close_connection(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    finally:
        connection.close()
