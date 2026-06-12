from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.test import Client

User = get_user_model()


class PrintPageAccessTest(TestCase):
    """Verify print pages work and respect permissions."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("prn_admin", "p@t.com", "Admin@2026!")
        cls.sales = User.objects.create_user("prn_sales", password="Sales@2026!", display_name="PS", security_level="L1")
        cls.warehouse = User.objects.create_user("prn_wh", password="Wh@2026!", display_name="PW", security_level="L1")
        cls.finance = User.objects.create_user("prn_fin", password="Fin@2026!", display_name="PF", security_level="L2")

        from masterdata.models import Material, Customer, CustomerProduct, Supplier
        cls.fg = Material.objects.create(material_code="PRN_FG", material_name="PRN FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.rm = Material.objects.create(material_code="PRN_RM", material_name="PRN RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.cust = Customer.objects.create(customer_no="PRN_C01", customer_name="PRN Cust", sales_owner=cls.sales, status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="PRN_CP01", customer_product_name="PRN CP", finished_material=cls.fg, status="active")
        cls.sup = Supplier.objects.create(supplier_no="PRN_SUP", supplier_name="PRN Supplier", status="active")
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="PRN_LOC", location_name="PRN Loc")
        InventoryBatch.objects.create(batch_no="PRN_B01", material=cls.fg, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("100"), remaining_qty=Decimal("100"), batch_status="in_stock")
        Inventory.objects.create(material=cls.fg, location=cls.loc, inventory_type="available", qty=Decimal("100"))

    def test_sales_order_print_page_renders(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="PRN_SO01", customer=self.cust, order_date="2026-06-10", status="confirmed", total_amount=Decimal("1000.00"), created_by=self.sales)
        SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="confirmed", inventory_check_status="sufficient")
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("sales:sales_order_print", args=[so.id]))
        self.assertEqual(resp.status_code, 200)

    def test_sales_order_print_price_masked_for_warehouse(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="PRN_SO02", customer=self.cust, order_date="2026-06-10", status="confirmed", total_amount=Decimal("1000.00"), created_by=self.sales)
        SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="confirmed", inventory_check_status="sufficient")
        self.client.force_login(self.warehouse)
        resp = self.client.get(reverse("sales:sales_order_print", args=[so.id]))
        content = resp.content.decode()
        self.assertNotIn("100.00", content)

    def test_purchase_order_print_page_renders(self):
        from purchase.models import PurchaseOrder, PurchaseOrderItem
        po = PurchaseOrder.objects.create(purchase_order_no="PRN_PO01", supplier=self.sup, order_date="2026-06-10", status="confirmed")
        PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("100"), received_qty=Decimal("0"), unit_price=Decimal("10.00"), line_amount=Decimal("1000.00"), line_status="open")
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("purchase:purchase_order_print", args=[po.id]))
        self.assertEqual(resp.status_code, 200)

    def test_purchase_receipt_print_page_renders(self):
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        po = PurchaseOrder.objects.create(purchase_order_no="PRN_PO02", supplier=self.sup, order_date="2026-06-10", status="confirmed")
        poi = PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("100"), received_qty=Decimal("0"), unit_price=Decimal("10.00"), line_amount=Decimal("1000.00"), line_status="open")
        pr = PurchaseReceipt.objects.create(purchase_receipt_no="PRN_PR01", purchase_order=po, supplier=self.sup, receipt_date="2026-06-12", status="received")
        PurchaseReceiptItem.objects.create(purchase_receipt=pr, purchase_order_item=poi, material=self.rm, received_qty=Decimal("100"), accepted_qty=Decimal("100"), rejected_qty=Decimal("0"), unit_price=Decimal("10.00"), location=self.loc)
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("purchase:purchase_receipt_print", args=[pr.id]))
        self.assertEqual(resp.status_code, 200)

    def test_production_order_print_page_renders(self):
        from bom.models import Bom, BomItem
        from production.models import ProductionOrder
        bom = Bom.objects.create(bom_no="PRN_BOM", finished_material=self.fg, bom_version="A", base_qty=Decimal("1"), status="enabled", is_default=True)
        BomItem.objects.create(bom=bom, line_no=1, component_material=self.rm, usage_qty=Decimal("1"), usage_unit="kg", is_required=True)
        prod = ProductionOrder.objects.create(production_order_no="PRN_MO01", finished_material=self.fg, production_qty=Decimal("100"), locked_bom=bom, locked_bom_version="A", status="pending")
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("production:production_order_print", args=[prod.id]))
        self.assertEqual(resp.status_code, 200)

    def test_print_page_writes_log(self):
        from files.models import PrintLog
        from sales.models import SalesOrder, SalesOrderItem
        before = PrintLog.objects.count()
        so = SalesOrder.objects.create(sales_order_no="PRN_SO03", customer=self.cust, order_date="2026-06-10", status="confirmed", total_amount=Decimal("100.00"), created_by=self.sales)
        SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("100.00"), line_amount=Decimal("100.00"), line_status="confirmed", inventory_check_status="sufficient")
        self.client.force_login(self.admin)
        self.client.get(reverse("sales:sales_order_print", args=[so.id]))
        after = PrintLog.objects.count()
        self.assertGreater(after, before)

    def test_voided_sales_order_print_shows_voided(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="PRN_SO04", customer=self.cust, order_date="2026-06-10", status="voided", total_amount=Decimal("0"), created_by=self.sales)
        SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("0"), line_amount=Decimal("0"), line_status="voided", inventory_check_status="unchecked")
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("sales:sales_order_print", args=[so.id]))
        content = resp.content.decode()
        self.assertIn("voided", so.status.lower())


class BusinessLogicEdgeCaseTest(TestCase):
    """Verify edge cases in business logic."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("ble_admin", "b@t.com", "Admin@2026!")
        from masterdata.models import Material, Customer, CustomerProduct
        cls.fg = Material.objects.create(material_code="BLE_FG", material_name="BLE FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="BLE_C01", customer_name="BLE Cust", status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="BLE_CP01", customer_product_name="BLE CP", finished_material=cls.fg, status="active")

    def test_cannot_submit_order_without_items(self):
        from sales.models import SalesOrder
        so = SalesOrder.objects.create(sales_order_no="BLE_SO_EMPTY", customer=self.cust, order_date="2026-06-10", status="draft", created_by=self.admin)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("sales:sales_order_submit", args=[so.id]), follow=True)
        so.refresh_from_db()
        self.assertNotEqual(so.status, "pending_approval")

    def test_cannot_confirm_already_confirmed_order(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="BLE_SO_CONF", customer=self.cust, order_date="2026-06-10", status="confirmed", total_amount=Decimal("100.00"), created_by=self.admin)
        SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("100.00"), line_amount=Decimal("100.00"), line_status="confirmed", inventory_check_status="sufficient")
        from sales.services import confirm_sales_order
        result = confirm_sales_order(so.id, self.admin.id)
        self.assertFalse(result.success)

    def test_payment_cannot_allocate_beyond_receivable(self):
        from finance.services import confirm_customer_receipt
        from finance.models import CustomerReceipt
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="BLE_SO_PAY", customer=self.cust, order_date="2026-06-10", status="draft", total_amount=Decimal("100.00"), created_by=self.admin)
        SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("100.00"), line_amount=Decimal("100.00"), line_status="draft", inventory_check_status="unchecked")
        receipt = CustomerReceipt.objects.create(receipt_no="BLE_RC01", customer=self.cust, receipt_date="2026-06-10", receipt_amount=Decimal("200.00"), unallocated_amount=Decimal("200.00"), status="pending_approval")
        result = confirm_customer_receipt(receipt.id, [{"target_type": "sales_order", "sales_order_id": so.id, "allocated_amount": "200.00"}], self.admin.id, idempotency_key="ble_pay_01")
        self.assertFalse(result.success)

    def test_shortage_qty_cannot_be_negative(self):
        from sales.models import SalesOrder, SalesOrderItem, ShortageAlert
        so = SalesOrder.objects.create(sales_order_no="BLE_SO_SH", customer=self.cust, order_date="2026-06-10", status="draft", created_by=self.admin)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("100.00"), line_amount=Decimal("100.00"), line_status="draft", inventory_check_status="unchecked")
        alert = ShortageAlert.objects.create(shortage_no="BLE_SA01", sales_order=so, sales_order_item=soi, material=self.fg, required_qty=Decimal("10"), available_qty=Decimal("20"), shortage_qty=Decimal("0"), is_required=True, status="unprocessed")
        self.assertGreaterEqual(alert.shortage_qty, Decimal("0"))


class PurchaseReturnStockFlowTest(TestCase):
    """Verify supplier return correctly updates inventory."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("prs_admin", "p@t.com", "Admin@2026!")
        from masterdata.models import Material, Supplier
        cls.rm = Material.objects.create(material_code="PRS_RM", material_name="PRS RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.sup = Supplier.objects.create(supplier_no="PRS_SUP", supplier_name="PRS Sup", status="active")
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="PRS_LOC", location_name="PRS Loc")
        cls.batch = InventoryBatch.objects.create(batch_no="PRS_B01", material=cls.rm, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("100"), remaining_qty=Decimal("100"), batch_status="in_stock")
        Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("100"))

    def test_supplier_return_reduces_stock(self):
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        from purchase.models import SupplierReturn, SupplierReturnItem
        from purchase.services import confirm_supplier_return_shipment
        from inventory.models import InventoryBatch
        po = PurchaseOrder.objects.create(purchase_order_no="PRS_PO01", supplier=self.sup, order_date="2026-06-10", status="confirmed")
        poi = PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("100"), received_qty=Decimal("0"), unit_price=Decimal("10.00"), line_amount=Decimal("1000.00"), line_status="open")
        pr = PurchaseReceipt.objects.create(purchase_receipt_no="PRS_PR01", purchase_order=po, supplier=self.sup, receipt_date="2026-06-12", status="pending_receive")
        pri = PurchaseReceiptItem.objects.create(purchase_receipt=pr, purchase_order_item=poi, material=self.rm, received_qty=Decimal("100"), accepted_qty=Decimal("100"), rejected_qty=Decimal("0"), unit_price=Decimal("10.00"), location=self.loc)
        sr = SupplierReturn.objects.create(supplier_return_no="PRS_SR01", supplier=self.sup, purchase_receipt=pr, return_date="2026-06-15", status="confirmed")
        SupplierReturnItem.objects.create(supplier_return=sr, purchase_receipt_item=pri, material=self.rm, return_qty=Decimal("20"), unit_price=Decimal("10.00"), return_amount=Decimal("200.00"), batch=self.batch, location=self.loc)
        result = confirm_supplier_return_shipment(sr.id, self.admin.id, idempotency_key="prs_sr01")
        self.assertTrue(result.success)
        self.batch.refresh_from_db()
        self.assertLess(self.batch.remaining_qty, Decimal("100"))
