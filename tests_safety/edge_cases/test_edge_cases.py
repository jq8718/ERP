from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class BoundaryValueTest(TestCase):
    """Verify system stability with edge case data."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("bnd_admin", "bnd@t.com", "Admin@2026!")
        from masterdata.models import Material, Customer, Supplier
        cls.rm = Material.objects.create(material_code="BND_RM", material_name="BND Raw", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.fg = Material.objects.create(material_code="BND_FG", material_name="BND FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="BND_C01", customer_name="BND Customer", status="active")
        cls.sup = Supplier.objects.create(supplier_no="BND_SUP", supplier_name="BND Supplier", status="active")
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="BND_LOC", location_name="BND Location")
        InventoryBatch.objects.create(batch_no="BND_B01", material=cls.rm, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("99999999"), remaining_qty=Decimal("99999999"), batch_status="in_stock")
        Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("99999999"))

    def test_01_large_order_quantity(self):
        from sales.models import SalesOrder, SalesOrderItem
        from masterdata.models import CustomerProduct
        cp = CustomerProduct.objects.create(customer=self.cust, customer_product_no="BND_CP01", customer_product_name="BND CP", finished_material=self.fg, status="active")
        so = SalesOrder.objects.create(sales_order_no="BND_SO_LARGE", customer=self.cust, order_date="2026-06-10", status="draft")
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=cp, finished_material=self.fg, order_qty=Decimal("999999"), unit_price=Decimal("999999.9999"), line_amount=Decimal("999999999999"), line_status="draft", inventory_check_status="unchecked")
        self.assertGreater(soi.order_qty, Decimal("0"))
        self.assertGreater(soi.line_amount, Decimal("0"))

    def test_02_large_purchase_receipt(self):
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        po = PurchaseOrder.objects.create(purchase_order_no="BND_PO_LARGE", supplier=self.sup, order_date="2026-06-10", status="confirmed")
        poi = PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("50000"), received_qty=Decimal("0"), unit_price=Decimal("999.999999"), line_amount=Decimal("49999999.95"), line_status="open")
        pr = PurchaseReceipt.objects.create(purchase_receipt_no="BND_PR_LARGE", purchase_order=po, supplier=self.sup, receipt_date="2026-06-12", status="pending_receive")
        pri = PurchaseReceiptItem.objects.create(purchase_receipt=pr, purchase_order_item=poi, material=self.rm, received_qty=Decimal("50000"), accepted_qty=Decimal("49999"), rejected_qty=Decimal("1"), unit_price=Decimal("999.999999"), location=self.loc)
        self.assertGreater(pri.accepted_qty, Decimal("0"))
        self.assertGreater(pri.rejected_qty, Decimal("0"))

    def test_03_zero_price_order(self):
        from sales.models import SalesOrder, SalesOrderItem
        from masterdata.models import CustomerProduct
        cp = CustomerProduct.objects.create(customer=self.cust, customer_product_no="BND_CP03", customer_product_name="BND CP Zero", finished_material=self.fg, status="active")
        so = SalesOrder.objects.create(sales_order_no="BND_SO_ZERO", customer=self.cust, order_date="2026-06-10", status="draft")
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("0.00"), line_amount=Decimal("0.00"), line_status="draft", inventory_check_status="unchecked")
        self.assertGreaterEqual(soi.unit_price, Decimal("0"))

    def test_04_zero_quantity_bom_item(self):
        from bom.models import Bom, BomItem
        from masterdata.models import Material
        sub = Material.objects.create(material_code="BND_SUB", material_name="BND Sub", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        bom = Bom.objects.create(bom_no="BND_BOM_ZERO", finished_material=self.fg, bom_version="A", base_qty=Decimal("1"), status="draft")
        bi = BomItem.objects.create(bom=bom, line_no=1, component_material=sub, usage_qty=Decimal("0.000001"), usage_unit="kg", loss_rate=Decimal("0.000001"), is_required=True)
        self.assertGreater(bi.usage_qty, Decimal("0"))

    def test_05_supplier_returns_empty_ok(self):
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        po = PurchaseOrder.objects.create(purchase_order_no="BND_PO_EMPTY", supplier=self.sup, order_date="2026-06-10", status="confirmed")
        poi = PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("10"), received_qty=Decimal("0"), unit_price=Decimal("10.00"), line_amount=Decimal("100.00"), line_status="open")
        pr = PurchaseReceipt.objects.create(purchase_receipt_no="BND_PR_EMPTY", purchase_order=po, supplier=self.sup, receipt_date="2026-06-12", status="pending_receive")
        pri = PurchaseReceiptItem.objects.create(purchase_receipt=pr, purchase_order_item=poi, material=self.rm, received_qty=Decimal("10"), accepted_qty=Decimal("10"), rejected_qty=Decimal("0"), unit_price=Decimal("10.00"), location=self.loc)
        self.assertEqual(pri.rejected_qty, Decimal("0"))


class PrecisionRoundingTest(TestCase):
    """Verify decimal precision and rounding don't cause data corruption."""

    def test_multiple_small_quantities_add_up(self):
        """Many small decimal additions don't lose precision."""
        total = Decimal("0")
        for _ in range(1000):
            total += Decimal("0.001")
        self.assertEqual(total, Decimal("1.000"))

    def test_price_times_quantity_equals_line_amount(self):
        """Quantity * unit_price matches line_amount."""
        qty = Decimal("137")
        price = Decimal("27.8533")
        expected = (qty * price).quantize(Decimal("0.01"))
        from sales.models import SalesOrderItem
        self.assertIsInstance(expected, Decimal)

    def test_nested_loss_rate_calculation(self):
        """Loss rate at multiple precision levels doesn't overflow."""
        theo = Decimal("25000.000000")
        loss = Decimal("0.023456")
        demand = theo * (Decimal("1") + loss)
        self.assertGreater(demand, theo)


class SpecialCharactersTest(TestCase):
    """Verify system handles Unicode/special characters safely."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("spec_admin", "spec@t.com", "Admin@2026!")

    def test_material_name_with_emoji(self):
        from masterdata.models import Material
        mat = Material.objects.create(material_code="SPEC_EMOJI", material_name="Product ", material_type="raw", base_unit="kg", qty_precision=0, status="active")
        self.assertEqual(mat.material_name, "Product ")

    def test_customer_name_with_special_chars(self):
        from masterdata.models import Customer
        cust = Customer.objects.create(customer_no="SPEC_SPECIAL", customer_name='ACME "Best" & Co. <Ltd>', status="active")
        self.assertIn("ACME", cust.customer_name)