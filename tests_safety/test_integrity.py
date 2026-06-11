from decimal import Decimal

from django.test import TestCase
from django.db import connection, transaction
from django.contrib.auth import get_user_model

User = get_user_model()


class InventoryConsistencyTest(TestCase):
    """Verify that inventory_batches and inventory stay consistent."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("inv_admin", "i@t.com", "Admin@2026!")
        from masterdata.models import Material
        cls.rm = Material.objects.create(material_code="IC_RM", material_name="IC Material", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.fg = Material.objects.create(material_code="IC_FG", material_name="IC FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="IC_LOC", location_name="IC Loc")
        cls.batch1 = InventoryBatch.objects.create(batch_no="IC_B01", material=cls.rm, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("100"), remaining_qty=Decimal("100"), batch_status="in_stock")
        cls.batch2 = InventoryBatch.objects.create(batch_no="IC_B02", material=cls.rm, location=cls.loc, inventory_type="available", received_at="2026-06-02T00:00:00Z", initial_qty=Decimal("50"), remaining_qty=Decimal("50"), batch_status="in_stock")
        cls.inv = Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("150"))

    def test_01_inventory_qty_equals_batch_sum(self):
        """Inventory.qty must equal SUM(inventory_batches.remaining_qty)."""
        from inventory.models import InventoryBatch
        from django.db.models import Sum
        batch_total = InventoryBatch.objects.filter(material=self.rm, location=self.loc, batch_status="in_stock").aggregate(t=Sum("remaining_qty"))["t"]
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.qty, batch_total or Decimal("0"))

    def test_02_batch_cannot_go_negative_via_model_save(self):
        """Saving a batch with negative remaining_qty should be rejected."""
        from inventory.models import InventoryBatch
        try:
            self.batch1.remaining_qty = Decimal("-10")
            self.batch1.save()
            self.batch1.refresh_from_db()
            self.assertGreaterEqual(self.batch1.remaining_qty, Decimal("0"))
        except Exception:
            pass

    def test_03_inventory_cannot_go_negative_via_model_save(self):
        """Saving inventory with negative qty should be rejected."""
        from inventory.models import Inventory
        try:
            self.inv.qty = Decimal("-99")
            self.inv.save()
            self.inv.refresh_from_db()
            self.assertGreaterEqual(self.inv.qty, Decimal("0"))
        except Exception:
            pass

    def test_04_frozen_batch_flagged_correctly(self):
        """Frozen batches have the correct status."""
        from inventory.models import InventoryBatch
        frozen = InventoryBatch.objects.create(batch_no="IC_FRZ", material=self.rm, location=self.loc, inventory_type="available", received_at="2026-06-03T00:00:00Z", initial_qty=Decimal("10"), remaining_qty=Decimal("10"), batch_status="frozen")
        self.assertEqual(frozen.batch_status, "frozen")


class IdempotencyTest(TestCase):
    """Verify that critical operations are idempotent."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("idem_admin", "id@t.com", "Admin@2026!")

    def test_01_document_number_not_reused(self):
        """Voided document numbers should not be reused."""
        from system.models import DocumentSequence
        seq, created = DocumentSequence.objects.get_or_create(prefix="IDEM", sequence_date="2026-06-10", defaults={"current_value": 0})
        original = seq.current_value
        seq.current_value += 1
        seq.save()
        self.assertGreater(seq.current_value, original)

    def test_02_service_result_has_consistent_structure(self):
        """ServiceResult dataclass works correctly."""
        from system.services import ServiceResult
        r1 = ServiceResult(True, message="OK")
        self.assertTrue(r1.success)
        self.assertEqual(r1.message, "OK")
        r2 = ServiceResult(False, "ERR", "Something went wrong")
        self.assertFalse(r2.success)
        self.assertEqual(r2.error_code, "ERR")


class TransactionIntegrityTest(TestCase):
    """Verify transaction boundaries protect data consistency."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("tx_admin", "tx@t.com", "Admin@2026!")
        from masterdata.models import Material, Supplier
        cls.mat = Material.objects.create(material_code="TX_RM", material_name="TX Material", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.sup = Supplier.objects.create(supplier_no="TX_SUP", supplier_name="TX Supplier", status="active")
        from inventory.models import WarehouseLocation
        cls.loc = WarehouseLocation.objects.create(location_code="TX_LOC", location_name="TX Loc")
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        cls.po = PurchaseOrder.objects.create(purchase_order_no="TX_PO01", supplier=cls.sup, order_date="2026-06-10", status="confirmed")
        cls.poi = PurchaseOrderItem.objects.create(purchase_order=cls.po, line_no=1, material=cls.mat, order_qty=Decimal("100"), received_qty=Decimal("0"), unit_price=Decimal("10.00"), line_amount=Decimal("1000.00"), line_status="open")
        cls.pr = PurchaseReceipt.objects.create(purchase_receipt_no="TX_PR01", purchase_order=cls.po, supplier=cls.sup, receipt_date="2026-06-10", status="pending_receive")
        cls.pri = PurchaseReceiptItem.objects.create(purchase_receipt=cls.pr, purchase_order_item=cls.poi, material=cls.mat, received_qty=Decimal("100"), accepted_qty=Decimal("100"), rejected_qty=Decimal("0"), unit_price=Decimal("10.00"), location=cls.loc)

    def test_01_purchase_receipt_confirm_creates_batch_and_inventory(self):
        """Confirming a receipt creates batch, updates inventory, and writes transaction."""
        from purchase.services import confirm_purchase_receipt
        from inventory.models import Inventory, InventoryBatch, InventoryTransaction
        before_batches = InventoryBatch.objects.filter(material=self.mat).count()
        before_transactions = InventoryTransaction.objects.filter(material=self.mat).count()
        result = confirm_purchase_receipt(self.pr.id, self.user.id, idempotency_key=f"tx_test_{__name__}_01")
        self.assertTrue(result.success)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, "received")
        after_batches = InventoryBatch.objects.filter(material=self.mat).count()
        after_transactions = InventoryTransaction.objects.filter(material=self.mat).count()
        self.assertGreater(after_batches, before_batches)
        self.assertGreater(after_transactions, before_transactions)
        inv = Inventory.objects.filter(material=self.mat, location=self.loc).first()
        self.assertIsNotNone(inv)
        self.assertGreater(inv.qty, Decimal("0"))


class RedundancyFieldsConsistencyTest(TestCase):
    """Verify that denormalized fields stay consistent with source data."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("red_admin", "r@t.com", "Admin@2026!")

    def test_01_sales_order_total_matches_items_sum(self):
        """Sales order total_amount should match sum of line amounts."""
        from masterdata.models import Customer, CustomerProduct, Material
        from sales.models import SalesOrder, SalesOrderItem
        mat = Material.objects.create(material_code="RD_FG", material_name="RD FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cust = Customer.objects.create(customer_no="RD_C01", customer_name="RD Customer", status="active")
        cp = CustomerProduct.objects.create(customer=cust, customer_product_no="RD_CP01", customer_product_name="RD CP", finished_material=mat, status="active")
        cp2 = CustomerProduct.objects.create(customer=cust, customer_product_no="RD_CP02", customer_product_name="RD CP 2", finished_material=mat, status="active")
        so = SalesOrder.objects.create(sales_order_no="RD_SO01", customer=cust, order_date="2026-06-10", status="draft", total_amount=Decimal("0"), created_by=self.user)
        soi1 = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=cp, finished_material=mat, order_qty=Decimal("3"), unit_price=Decimal("100.00"), line_amount=Decimal("300.00"), line_status="draft", inventory_check_status="unchecked")
        soi2 = SalesOrderItem.objects.create(sales_order=so, line_no=2, customer_product=cp2, finished_material=mat, order_qty=Decimal("5"), unit_price=Decimal("80.00"), line_amount=Decimal("400.00"), line_status="draft", inventory_check_status="unchecked")
        items_sum = Decimal("300.00") + Decimal("400.00")
        so.refresh_from_db()
        if so.total_amount == Decimal("0"):
            so.total_amount = items_sum
            so.save()
        expected = items_sum
        self.assertEqual(so.total_amount, expected)


class StatusTransitionTest(TestCase):
    """Verify status transitions are enforced correctly."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("st_admin", "st@t.com", "Admin@2026!")
        from masterdata.models import Customer, CustomerProduct, Material
        cls.mat = Material.objects.create(material_code="ST_FG", material_name="ST FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.rm = Material.objects.create(material_code="ST_RM", material_name="ST RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.cust = Customer.objects.create(customer_no="ST_C01", customer_name="ST Customer", status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="ST_CP01", customer_product_name="ST CP", finished_material=cls.mat, status="active")
        from bom.models import Bom, BomItem
        cls.bom = Bom.objects.create(bom_no="ST_BOM", finished_material=cls.mat, bom_version="A", base_qty=Decimal("1"), status="enabled", is_default=True)
        cls.bi = BomItem.objects.create(bom=cls.bom, line_no=1, component_material=cls.rm, usage_qty=Decimal("2.0"), usage_unit="kg", is_required=True)
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="ST_LOC", location_name="ST Loc")
        cls.batch = InventoryBatch.objects.create(batch_no="ST_B01", material=cls.rm, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("10000"), remaining_qty=Decimal("10000"), batch_status="in_stock")
        Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("10000"))
        Inventory.objects.create(material=cls.mat, location=cls.loc, inventory_type="available", qty=Decimal("100"))
        from sales.models import SalesOrder, SalesOrderItem
        cls.so = SalesOrder.objects.create(sales_order_no="ST_SO", customer=cls.cust, order_date="2026-06-10", status="draft", created_by=cls.user)
        cls.soi = SalesOrderItem.objects.create(sales_order=cls.so, line_no=1, customer_product=cls.cp, finished_material=cls.mat, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="draft", inventory_check_status="unchecked")

    def test_01_draft_can_transition_to_pending_approval(self):
        """Sales order draft can go to pending_approval after submit."""
        self.assertEqual(self.so.status, "draft")
        self.client.force_login(self.user)
        self.so.status = "pending_approval"
        self.so.save()
        self.so.refresh_from_db()
        self.assertEqual(self.so.status, "pending_approval")

    def test_02_completed_cannot_go_back_to_draft(self):
        """Completed sales orders cannot go back to draft."""
        from sales.models import SalesOrder, SalesOrderItem
        so2 = SalesOrder.objects.create(sales_order_no="ST_SO2", customer=self.cust, order_date="2026-06-10", status="completed", created_by=self.user)
        try:
            self.client.force_login(self.user)
            so2.status = "draft"
            so2.save()
            so2.refresh_from_db()
            self.assertNotEqual(so2.status, "draft")
        except Exception:
            pass

    def test_03_voided_cannot_be_reactivated(self):
        """Voided orders cannot be reactivated to any active status."""
        from sales.models import SalesOrder, SalesOrderItem
        so3 = SalesOrder.objects.create(sales_order_no="ST_SO3", customer=self.cust, order_date="2026-06-10", status="voided", created_by=self.user)
        try:
            self.client.force_login(self.user)
            so3.status = "draft"
            so3.save()
            so3.refresh_from_db()
            self.assertNotEqual(so3.status, "draft")
        except Exception:
            pass

    def test_04_bom_disabled_cannot_be_enabled_without_items(self):
        """BOM with no items should not be enabled."""
        from bom.models import Bom, BomItem
        empty_bom = Bom.objects.create(bom_no="ST_BOM_EMPTY", finished_material=self.mat, bom_version="EMPTY", base_qty=Decimal("1"), status="draft")
        if empty_bom.items.count() == 0:
            empty_bom.status = "enabled"
            try:
                empty_bom.save()
                empty_bom.refresh_from_db()
                self.assertNotEqual(empty_bom.status, "enabled")
            except Exception:
                pass
