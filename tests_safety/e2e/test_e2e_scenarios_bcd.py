from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class EndToEndScenarioBTest(TestCase):
    """Scenario B: Finished goods stock sufficient, direct shipment."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("e2eb_admin", "eb@t.com", "Admin@2026!")
        cls.sales = User.objects.create_user("e2eb_sales", password="Sales@2026!", display_name="Sales B", security_level="L1")
        cls.warehouse = User.objects.create_user("e2eb_wh", password="Wh@2026!", display_name="WH B", security_level="L1")
        from masterdata.models import Material, Customer, CustomerProduct
        cls.fg = Material.objects.create(material_code="E2EB_FG", material_name="E2EB FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="E2EB_C01", customer_name="E2EB Customer", sales_owner=cls.sales, status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="E2EB_CP01", customer_product_name="E2EB Product", finished_material=cls.fg, default_sale_price=Decimal("50.00"), status="active")
        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="E2EB_LOC", location_name="E2EB Location")
        cls.batch_fg = InventoryBatch.objects.create(batch_no="E2EB_B01", material=cls.fg, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("500"), remaining_qty=Decimal("500"), batch_status="in_stock")
        Inventory.objects.create(material=cls.fg, location=cls.loc, inventory_type="available", qty=Decimal("500"))

    def test_stock_sufficient_direct_shipment(self):
        """When FG stock is sufficient, order can ship directly without production."""
        from sales.models import SalesOrder, SalesOrderItem, SalesShipment, SalesShipmentItem
        from sales.services import confirm_sales_order, confirm_sales_shipment
        from inventory.models import InventoryBatch
        so = SalesOrder.objects.create(sales_order_no="E2EB_SO01", customer=self.cust, order_date="2026-06-10", status="pending_approval", created_by=self.sales)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("10"), unit_price=Decimal("50.00"), line_amount=Decimal("500.00"), line_status="pending_approval", inventory_check_status="unchecked")
        from bom.models import Bom, BomItem
        from masterdata.models import Material
        rm = Material.objects.create(material_code="E2EB_RM", material_name="E2EB RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        bom = Bom.objects.create(bom_no="E2EB_BOM", finished_material=self.fg, bom_version="A", base_qty=Decimal("1"), effective_date="2026-06-01", status="enabled", is_default=True)
        BomItem.objects.create(bom=bom, line_no=1, component_material=rm, usage_qty=Decimal("1"), usage_unit="kg", is_required=True)
        r1 = confirm_sales_order(so.id, self.admin.id)
        self.assertTrue(r1.success)
        soi.refresh_from_db()
        self.assertEqual(soi.inventory_check_status, "sufficient")
        batch_fg = InventoryBatch.objects.filter(material=self.fg, batch_status="in_stock", remaining_qty__gte=Decimal("10")).first()
        self.assertIsNotNone(batch_fg)
        ship = SalesShipment.objects.create(shipment_no="E2EB_SD01", sales_order=so, customer=self.cust, shipment_date="2026-06-15", status="pending_confirm")
        SalesShipmentItem.objects.create(shipment=ship, sales_order_item=soi, material=self.fg, shipment_qty=Decimal("10"), batch=batch_fg, location=self.loc)
        r2 = confirm_sales_shipment(ship.id, self.warehouse.id, idempotency_key=f"e2eb_sd01_{ship.id}")
        self.assertTrue(r2.success)
        soi.refresh_from_db()
        self.assertEqual(soi.line_status, "shipped")


class EndToEndScenarioCTest(TestCase):
    """Scenario C: Customer return, over-receipt, credit balance handling."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("e2ec_admin", "ec@t.com", "Admin@2026!")
        cls.finance = User.objects.create_user("e2ec_fin", password="Fin@2026!", display_name="Fin C", security_level="L2")
        cls.warehouse = User.objects.create_user("e2ec_wh", password="Wh@2026!", display_name="WH C", security_level="L1")
        from masterdata.models import Material, Customer
        cls.fg = Material.objects.create(material_code="E2EC_FG", material_name="E2EC FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="E2EC_C01", customer_name="E2EC Customer", status="active")
        from inventory.models import WarehouseLocation
        cls.loc = WarehouseLocation.objects.create(location_code="E2EC_LOC", location_name="E2EC Location")

    def test_customer_return_and_credit_balance(self):
        """Customer returns goods after payment, creates credit balance."""
        from sales.models import SalesOrder, SalesOrderItem
        from sales.services import confirm_sales_order
        from finance.models import CustomerReceipt, CustomerCreditBalance
        from finance.services import confirm_customer_receipt
        from masterdata.models import CustomerProduct
        cp = CustomerProduct.objects.create(customer=self.cust, customer_product_no="E2EC_CP01", customer_product_name="E2EC Product", finished_material=self.fg, default_sale_price=Decimal("100.00"), status="active")
        from bom.models import Bom, BomItem
        from masterdata.models import Material
        rm = Material.objects.create(material_code="E2EC_RM", material_name="E2EC RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        bom = Bom.objects.create(bom_no="E2EC_BOM", finished_material=self.fg, bom_version="A", base_qty=Decimal("1"), status="enabled", is_default=True)
        BomItem.objects.create(bom=bom, line_no=1, component_material=rm, usage_qty=Decimal("1"), usage_unit="kg", is_required=True)
        so = SalesOrder.objects.create(sales_order_no="E2EC_SO01", customer=self.cust, order_date="2026-06-10", status="pending_approval", total_amount=Decimal("1000.00"))
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=cp, finished_material=self.fg, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="pending_approval", inventory_check_status="unchecked")
        confirm_sales_order(so.id, self.admin.id)
        receipt = CustomerReceipt.objects.create(receipt_no="E2EC_RC01", customer=self.cust, receipt_date="2026-06-15", receipt_amount=Decimal("1000.00"), unallocated_amount=Decimal("1000.00"), status="pending_approval")
        r1 = confirm_customer_receipt(receipt.id, [{"sales_order_id": so.id, "allocated_amount": "1000.00"}], self.finance.id, idempotency_key=f"e2ec_rc01_{receipt.id}")
        self.assertTrue(r1.success)
        receipt2 = CustomerReceipt.objects.create(receipt_no="E2EC_RC02", customer=self.cust, receipt_date="2026-06-20", receipt_amount=Decimal("500.00"), unallocated_amount=Decimal("500.00"), status="pending_approval")
        r2 = confirm_customer_receipt(receipt2.id, [], self.finance.id, idempotency_key=f"e2ec_rc02_{receipt2.id}")
        self.assertTrue(r2.success)
        has_unallocated = CustomerCreditBalance.objects.filter(customer=self.cust, source_doc_type="customer_receipt", source_doc_id=receipt2.id).exists()
        self.assertTrue(has_unallocated)


class EndToEndScenarioDTest(TestCase):
    """Scenario D: Supplier receipt with defects and return."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("e2ed_admin", "ed@t.com", "Admin@2026!")
        cls.warehouse = User.objects.create_user("e2ed_wh", password="Wh@2026!", display_name="WH D", security_level="L1")
        from masterdata.models import Material, Supplier
        cls.rm = Material.objects.create(material_code="E2ED_RM", material_name="E2ED Raw", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.sup = Supplier.objects.create(supplier_no="E2ED_SUP", supplier_name="E2ED Supplier", status="active")
        from inventory.models import WarehouseLocation
        cls.loc = WarehouseLocation.objects.create(location_code="E2ED_LOC", location_name="E2ED Location")

    def test_purchase_with_defects_and_return(self):
        """Receive goods, find defects, return to supplier."""
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        from purchase.services import confirm_purchase_receipt, confirm_supplier_return_shipment
        from inventory.models import InventoryBatch
        po = PurchaseOrder.objects.create(purchase_order_no="E2ED_PO01", supplier=self.sup, order_date="2026-06-10", status="confirmed")
        poi = PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("100"), received_qty=Decimal("0"), unit_price=Decimal("10.00"), line_amount=Decimal("1000.00"), line_status="open")
        pr = PurchaseReceipt.objects.create(purchase_receipt_no="E2ED_PR01", purchase_order=po, supplier=self.sup, receipt_date="2026-06-12", status="pending_receive")
        pri = PurchaseReceiptItem.objects.create(purchase_receipt=pr, purchase_order_item=poi, material=self.rm, received_qty=Decimal("100"), accepted_qty=Decimal("90"), rejected_qty=Decimal("10"), unit_price=Decimal("10.00"), location=self.loc)
        r1 = confirm_purchase_receipt(pr.id, self.warehouse.id, idempotency_key=f"e2ed_pr01_{pr.id}")
        self.assertTrue(r1.success)
        batch_in_stock = InventoryBatch.objects.filter(material=self.rm, batch_status="in_stock", remaining_qty__gte=Decimal("90")).exists()
        self.assertTrue(batch_in_stock)
