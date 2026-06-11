from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class EndToEndScenarioATest(TestCase):
    """Scenario A: New customer full order-to-cash flow."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("e2ea_admin", "ea@t.com", "Admin@2026!")
        cls.sales = User.objects.create_user("e2ea_sales", password="Sales@2026!", display_name="Sales A", security_level="L1")
        cls.purchase_user = User.objects.create_user("e2ea_purch", password="Purch@2026!", display_name="Purch A", security_level="L1")
        cls.warehouse = User.objects.create_user("e2ea_wh", password="Wh@2026!", display_name="WH A", security_level="L1")
        cls.finance = User.objects.create_user("e2ea_fin", password="Fin@2026!", display_name="Fin A", security_level="L2")
        from masterdata.models import Material, Customer, CustomerProduct, Supplier, MaterialUnitConversion
        cls.rm = Material.objects.create(material_code="E2EA_RM", material_name="E2EA Raw", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.fg = Material.objects.create(material_code="E2EA_FG", material_name="E2EA Finished", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        MaterialUnitConversion.objects.create(material=cls.rm, source_unit="g", target_unit="kg", ratio=Decimal("0.001"), status="active")
        cls.cust = Customer.objects.create(customer_no="E2EA_C01", customer_name="E2EA Cust", sales_owner=cls.sales, settlement_method="monthly", status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="E2EA_CP01", customer_product_name="E2EA Product", finished_material=cls.fg, default_sale_price=Decimal("100.00"), status="active")
        cls.sup = Supplier.objects.create(supplier_no="E2EA_SUP", supplier_name="E2EA Supplier", supplier_type="raw", payment_method="monthly", status="active")
        from bom.models import Bom, BomItem
        cls.bom = Bom.objects.create(bom_no="E2EA_BOM", finished_material=cls.fg, bom_version="A", base_qty=Decimal("1"), effective_date="2026-06-01", status="enabled", is_default=True)
        cls.bi = BomItem.objects.create(bom=cls.bom, line_no=1, component_material=cls.rm, usage_qty=Decimal("250"), usage_unit="g", loss_rate=Decimal("0.02"), is_required=True)
        from inventory.models import WarehouseLocation, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="E2EA_LOC", location_name="E2EA Location")
        Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("0"))
        Inventory.objects.create(material=cls.fg, location=cls.loc, inventory_type="available", qty=Decimal("0"))

    def test_01_create_draft_sales_order(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="E2EA_SO01", customer=self.cust, order_date="2026-06-10", delivery_date="2026-06-30", status="draft", created_by=self.sales)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("100"), unit_price=Decimal("100.00"), line_amount=Decimal("10000.00"), line_status="draft", inventory_check_status="unchecked")
        self.assertEqual(so.status, "draft")
        self.assertEqual(soi.order_qty, Decimal("100"))

    def test_02_approve_order_creates_shortage(self):
        from sales.models import SalesOrder, SalesOrderItem, ShortageAlert
        from sales.services import confirm_sales_order
        so = SalesOrder.objects.create(sales_order_no="E2EA_SO02", customer=self.cust, order_date="2026-06-10", status="pending_approval", created_by=self.sales)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("100"), unit_price=Decimal("100.00"), line_amount=Decimal("10000.00"), line_status="pending_approval", inventory_check_status="unchecked")
        result = confirm_sales_order(so.id, self.admin.id)
        self.assertTrue(result.success)
        soi.refresh_from_db()
        self.assertEqual(soi.inventory_check_status, "shortage")
        alerts = ShortageAlert.objects.filter(sales_order_item=soi, status="unprocessed")
        self.assertGreater(alerts.count(), 0)

    def test_03_shortage_to_purchase_request(self):
        from sales.models import SalesOrder, SalesOrderItem, ShortageAlert
        from sales.services import confirm_sales_order
        from purchase.services import create_purchase_request_from_shortages
        so = SalesOrder.objects.create(sales_order_no="E2EA_SO03", customer=self.cust, order_date="2026-06-10", status="pending_approval", created_by=self.sales)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("100"), unit_price=Decimal("100.00"), line_amount=Decimal("10000.00"), line_status="pending_approval", inventory_check_status="unchecked")
        r1 = confirm_sales_order(so.id, self.admin.id)
        self.assertTrue(r1.success)
        alerts = list(ShortageAlert.objects.filter(sales_order_item=soi, status="unprocessed"))
        self.assertGreater(len(alerts), 0)
        r2 = create_purchase_request_from_shortages([a.id for a in alerts], self.purchase_user.id, merge_mode="by_material", idempotency_key=f"e2ea_s03_{so.id}")
        self.assertTrue(r2.success)

    def test_05_purchase_to_kitted_and_production(self):
        from sales.models import SalesOrder, SalesOrderItem, ShortageAlert
        from sales.services import confirm_sales_order, confirm_sales_shipment
        from purchase.services import create_purchase_request_from_shortages, confirm_purchase_receipt
        from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem
        from production.models import ProductionOrder, ProductionMaterialRequisition, ProductionMaterialRequisitionItem, ProductionReceipt, ProductionReceiptItem
        from production.services import confirm_material_requisition, confirm_production_receipt
        from finance.models import CustomerReceipt, CustomerReceiptAllocation
        from finance.services import confirm_customer_receipt
        from inventory.models import Inventory, InventoryBatch
        from sales.models import SalesShipment, SalesShipmentItem
        import django.db.models as models
        Inventory.objects.get_or_create(material=self.fg, location=self.loc, inventory_type="available", defaults={"qty": Decimal("0")})
        so = SalesOrder.objects.create(sales_order_no="E2EA_SO05", customer=self.cust, order_date="2026-06-10", status="pending_approval", created_by=self.sales)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("100"), unit_price=Decimal("100.00"), line_amount=Decimal("10000.00"), line_status="pending_approval", inventory_check_status="unchecked")
        confirm_sales_order(so.id, self.admin.id)
        alerts = list(ShortageAlert.objects.filter(sales_order_item=soi, status="unprocessed"))
        create_purchase_request_from_shortages([a.id for a in alerts], self.purchase_user.id)
        po = PurchaseOrder.objects.create(purchase_order_no="E2EA_PO05", supplier=self.sup, order_date="2026-06-11", status="confirmed")
        poi = PurchaseOrderItem.objects.create(purchase_order=po, line_no=1, material=self.rm, order_qty=Decimal("300"), received_qty=Decimal("0"), unit_price=Decimal("5.00"), line_amount=Decimal("1500.00"), line_status="open")
        pr = PurchaseReceipt.objects.create(purchase_receipt_no="E2EA_PR05", purchase_order=po, supplier=self.sup, receipt_date="2026-06-12", status="pending_receive")
        pri = PurchaseReceiptItem.objects.create(purchase_receipt=pr, purchase_order_item=poi, material=self.rm, received_qty=Decimal("300"), accepted_qty=Decimal("300"), rejected_qty=Decimal("0"), unit_price=Decimal("5.00"), location=self.loc)
        r_pur = confirm_purchase_receipt(pr.id, self.warehouse.id, idempotency_key=f"e2ea_pr05_{pr.id}")
        self.assertTrue(r_pur.success)
        soi.refresh_from_db()
        # After-commit recheck may not fire under TestCase's transaction wrapping;
        # manually trigger it to verify the full chain.
        from sales.services import recheck_sales_order_inventory
        recheck_sales_order_inventory([soi.id], trigger=f"test_e2ea_pr05_{pr.id}")
        soi.refresh_from_db()
        self.assertEqual(soi.inventory_check_status, "kitted")
        prod = ProductionOrder.objects.create(production_order_no="E2EA_MO05", sales_order_item=soi, finished_material=self.fg, production_qty=Decimal("100"), locked_bom=self.bom, locked_bom_version="A", status="pending")
        batch_rm = InventoryBatch.objects.filter(material=self.rm, batch_status="in_stock", remaining_qty__gt=0).first()
        self.assertIsNotNone(batch_rm)
        req = ProductionMaterialRequisition.objects.create(requisition_no="E2EA_MR05", production_order=prod, requisition_date="2026-06-15", status="pending_confirm")
        ProductionMaterialRequisitionItem.objects.create(requisition=req, production_order=prod, material=self.rm, required_qty=Decimal("300"), issued_qty=Decimal("300"), batch=batch_rm, location=self.loc, line_no=1)
        r_mat = confirm_material_requisition(req.id, self.warehouse.id, idempotency_key=f"e2ea_mr05_{req.id}")
        self.assertTrue(r_mat.success)
        prod_rec = ProductionReceipt.objects.create(production_receipt_no="E2EA_PI05", production_order=prod, receipt_date="2026-06-18", status="pending_confirm")
        ProductionReceiptItem.objects.create(production_receipt=prod_rec, production_order=prod, finished_material=self.fg, receipt_qty=Decimal("100"), location=self.loc, quality_status="qualified", line_no=1)
        r_prod = confirm_production_receipt(prod_rec.id, self.warehouse.id, idempotency_key=f"e2ea_pi05_{prod_rec.id}")
        self.assertTrue(r_prod.success)
        batch_fg = InventoryBatch.objects.filter(material=self.fg, batch_status="in_stock", remaining_qty__gt=0).first()
        self.assertIsNotNone(batch_fg)
        ship = SalesShipment.objects.create(shipment_no="E2EA_SD05", sales_order=so, customer=self.cust, shipment_date="2026-06-20", status="pending_confirm")
        SalesShipmentItem.objects.create(shipment=ship, sales_order_item=soi, material=self.fg, shipment_qty=Decimal("100"), batch=batch_fg, location=self.loc)
        r_ship = confirm_sales_shipment(ship.id, self.warehouse.id, idempotency_key=f"e2ea_sd05_{ship.id}")
        self.assertTrue(r_ship.success)
        soi.refresh_from_db()
        self.assertEqual(soi.line_status, "shipped")
        receipt = CustomerReceipt.objects.create(receipt_no="E2EA_RC05", customer=self.cust, receipt_date="2026-06-25", receipt_amount=Decimal("10000.00"), unallocated_amount=Decimal("10000.00"), status="pending_approval")
        r_fin = confirm_customer_receipt(receipt.id, [{"sales_order_id": so.id, "allocated_amount": "10000.00"}], self.finance.id, idempotency_key=f"e2ea_rc05_{receipt.id}")
        self.assertTrue(r_fin.success)
        allocated = CustomerReceiptAllocation.objects.filter(sales_order=so).aggregate(total=models.Sum("allocated_amount"))["total"] or Decimal("0")
        self.assertEqual(allocated, Decimal("10000.00"))
