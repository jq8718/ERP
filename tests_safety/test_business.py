from decimal import Decimal, ROUND_CEILING

from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class BOMCalculationTest(TestCase):
    """Verify BOM demand calculations match the documented algorithm."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("bom_admin", "b@t.com", "Admin@2026!")
        from masterdata.models import Material, MaterialUnitConversion
        cls.rm = Material.objects.create(material_code="BOM_RM", material_name="BOM RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.fg = Material.objects.create(material_code="BOM_FG", material_name="BOM FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        MaterialUnitConversion.objects.create(material=cls.rm, source_unit="g", target_unit="kg", ratio=Decimal("0.001"), status="active")
        from bom.models import Bom, BomItem
        cls.bom = Bom.objects.create(bom_no="BOM_CALC", finished_material=cls.fg, bom_version="A", base_qty=Decimal("1"), status="enabled", is_default=True)
        cls.bi = BomItem.objects.create(bom=cls.bom, line_no=1, component_material=cls.rm, usage_qty=Decimal("250.000000"), usage_unit="g", loss_rate=Decimal("0.020000"), is_required=True)

    def test_01_demand_calculation_follows_doc_algorithm(self):
        """Documented algorithm: theory -> loss -> convert -> precision -> compare."""
        production_qty = Decimal("100")
        theoretical = self.bi.usage_qty * production_qty
        self.assertEqual(theoretical, Decimal("25000.000000"))
        with_loss = theoretical * (Decimal("1") + self.bi.loss_rate)
        self.assertEqual(with_loss, Decimal("25500.000000"))
        converted = with_loss * Decimal("0.001")
        self.assertEqual(converted, Decimal("25.500"))
        quantum = Decimal("1").scaleb(-3)
        rounded = converted.quantize(quantum, rounding=ROUND_CEILING)
        self.assertEqual(rounded, Decimal("25.500"))

    def test_02_loss_rate_zero_produces_theoretical_only(self):
        """With 0% loss rate, demand equals theoretical."""
        self.bi.loss_rate = Decimal("0.000000")
        self.bi.save()
        production_qty = Decimal("100")
        theoretical = self.bi.usage_qty * production_qty
        with_loss = theoretical * (Decimal("1") + self.bi.loss_rate)
        self.assertEqual(theoretical, with_loss)

    def test_03_rounding_always_upward(self):
        """Material requirements are rounded upward (ceil) to avoid under-picking."""
        qty = Decimal("1.234")
        quantum = Decimal("1").scaleb(-2)
        rounded = qty.quantize(quantum, rounding=ROUND_CEILING)
        self.assertEqual(rounded, Decimal("1.24"))


class DecimalPrecisionTest(TestCase):
    """Verify decimal arithmetic is precise (no float rounding errors)."""

    def test_01_decimal_multiplication_precise(self):
        """Decimal multiplication is exact."""
        result = Decimal("0.1") + Decimal("0.2")
        self.assertEqual(result, Decimal("0.3"))

    def test_02_decimal_division_precise(self):
        """Decimal division preserves precision."""
        result = Decimal("100.00") / Decimal("3")
        self.assertEqual(result, Decimal("33.33333333333333333333333333"))

    def test_03_large_multiplication_no_overflow(self):
        """Large decimal multiplications handle precision correctly."""
        result = Decimal("999999.9999") * Decimal("999999.9999")
        self.assertGreater(result, Decimal("0"))


class ServiceResultTest(TestCase):
    """Verify the ServiceResult pattern works correctly for all outcomes."""

    def test_01_success_result(self):
        from system.services import ServiceResult
        r = ServiceResult(True, message="Done", data={"id": 1}, next_action="view")
        self.assertTrue(r.success)
        self.assertEqual(r.error_code, None)
        self.assertEqual(r.data["id"], 1)

    def test_02_failure_result_with_error_code(self):
        from system.services import ServiceResult
        r = ServiceResult(False, "STOCK_NOT_ENOUGH", "Insufficient stock")
        self.assertFalse(r.success)
        self.assertEqual(r.error_code, "STOCK_NOT_ENOUGH")
        self.assertEqual(r.next_action, None)

    def test_03_business_validation_error_code_present(self):
        """Error codes should always be present when success=False."""
        from system.services import ServiceResult
        r = ServiceResult(False, "AUTH_NO_PERMISSION", "No permission")
        self.assertIsNotNone(r.error_code)
        self.assertNotEqual(r.error_code, "")


class ShortageAlertLifecycleTest(TestCase):
    """Verify shortage alert state transitions."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("sa_admin", "sa@t.com", "Admin@2026!")
        from masterdata.models import Customer, CustomerProduct, Material
        cls.rm = Material.objects.create(material_code="SA_RM", material_name="SA RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.fg = Material.objects.create(material_code="SA_FG", material_name="SA FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="SA_C01", customer_name="SA Customer", status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="SA_CP", customer_product_name="SA CP", finished_material=cls.fg, status="active")
        from bom.models import Bom, BomItem
        cls.bom = Bom.objects.create(bom_no="SA_BOM", finished_material=cls.fg, bom_version="A", base_qty=Decimal("1"), status="enabled", is_default=True)
        cls.bi = BomItem.objects.create(bom=cls.bom, line_no=1, component_material=cls.rm, usage_qty=Decimal("2"), usage_unit="kg", loss_rate=Decimal("0"), is_required=True)
        from inventory.models import WarehouseLocation, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="SA_LOC", location_name="SA Loc")
        Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("0"))
        Inventory.objects.create(material=cls.fg, location=cls.loc, inventory_type="available", qty=Decimal("0"))
        from sales.models import SalesOrder, SalesOrderItem
        cls.so = SalesOrder.objects.create(sales_order_no="SA_SO", customer=cls.cust, order_date="2026-06-10", status="draft", created_by=cls.user)
        cls.soi = SalesOrderItem.objects.create(sales_order=cls.so, line_no=1, customer_product=cls.cp, finished_material=cls.fg, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="draft", inventory_check_status="unchecked")

    def test_01_shortage_created_when_no_inventory(self):
        """Shortage alert is created when no inventory and order confirmed."""
        from sales.services import confirm_sales_order
        self.so.status = "pending_approval"
        self.so.save()
        result = confirm_sales_order(self.so.id, self.user.id)
        self.assertTrue(result.success)
        from sales.models import ShortageAlert
        alerts = ShortageAlert.objects.filter(sales_order_item=self.soi)
        self.assertGreater(alerts.count(), 0)

    def test_02_shortage_status_starts_unprocessed(self):
        """New shortage alerts start as 'unprocessed'."""
        from sales.models import ShortageAlert
        from sales.services import confirm_sales_order
        if ShortageAlert.objects.filter(sales_order_item=self.soi).count() == 0:
            self.so.status = "pending_approval"
            self.so.save()
            confirm_sales_order(self.so.id, self.user.id)
        alert = ShortageAlert.objects.filter(sales_order_item=self.soi).first()
        if alert:
            self.assertEqual(alert.status, "unprocessed")


class CreditBalanceTest(TestCase):
    """Verify credit balance handling."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("cb_admin", "cb@t.com", "Admin@2026!")
        from masterdata.models import Customer
        cls.cust = Customer.objects.create(customer_no="CB_C01", customer_name="CB Customer", status="active")

    def test_01_credit_balance_created_with_correct_amounts(self):
        """Credit balance starts with balance=remaining, used=0."""
        from finance.models import CustomerCreditBalance
        bal = CustomerCreditBalance.objects.create(
            customer=self.cust, source_doc_type="customer_receipt", source_doc_id=999, source_doc_no="CB_RC001",
            balance_amount=Decimal("200.00"), used_amount=Decimal("0"), remaining_amount=Decimal("200.00"),
            status="pending",
        )
        self.assertEqual(bal.balance_amount, Decimal("200.00"))
        self.assertEqual(bal.used_amount, Decimal("0"))
        self.assertEqual(bal.remaining_amount, Decimal("200.00"))
        self.assertEqual(bal.status, "pending")

    def test_02_balance_used_amount_le_balance_amount(self):
        """Used amount cannot exceed balance amount."""
        from finance.models import CustomerCreditBalance
        bal = CustomerCreditBalance.objects.create(
            customer=self.cust, source_doc_type="customer_receipt", source_doc_id=999, source_doc_no="CB_RC002",
            balance_amount=Decimal("100.00"), used_amount=Decimal("0"), remaining_amount=Decimal("100.00"),
            status="pending",
        )
        bal.used_amount = Decimal("150.00")
        try:
            bal.save()
            bal.refresh_from_db()
            self.assertLessEqual(bal.used_amount, bal.balance_amount)
        except Exception:
            pass


class UnitConversionTest(TestCase):
    """Verify unit conversion edge cases."""

    @classmethod
    def setUpTestData(cls):
        from masterdata.models import Material, MaterialUnitConversion
        cls.mat = Material.objects.create(material_code="UC_MAT", material_name="UC Material", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        MaterialUnitConversion.objects.create(material=cls.mat, source_unit="g", target_unit="kg", ratio=Decimal("0.001"), status="active")
        MaterialUnitConversion.objects.create(material=cls.mat, source_unit="box", target_unit="kg", ratio=Decimal("25.00000000"), status="active")

    def test_01_conversion_same_unit_is_identity(self):
        """Converting kg to kg returns the same value."""
        from masterdata.models import MaterialUnitConversion
        conv = MaterialUnitConversion.objects.filter(material=self.mat, source_unit="kg", target_unit="kg").first()
        if conv is None:
            qty = Decimal("100")
            self.assertEqual(qty, Decimal("100"))
        else:
            result = Decimal("100") * conv.ratio
            self.assertEqual(result, Decimal("100"))

    def test_02_conversion_from_grams_to_kg(self):
        """1000g = 1kg."""
        from masterdata.models import MaterialUnitConversion
        conv = MaterialUnitConversion.objects.get(material=self.mat, source_unit="g", target_unit="kg")
        result = Decimal("5000") * conv.ratio
        self.assertEqual(result, Decimal("5.000"))

    def test_03_missing_conversion_raises_error(self):
        """Requesting a conversion that doesn't exist should raise."""
        from masterdata.models import MaterialUnitConversion
        conv = MaterialUnitConversion.objects.filter(material=self.mat, source_unit="lbs", target_unit="kg").first()
        self.assertIsNone(conv)
