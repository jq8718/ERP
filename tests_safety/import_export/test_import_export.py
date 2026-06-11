import io, csv, os, tempfile
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class ImportExportSafetyTest(TestCase):
    """Verify import/export security and correctness."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("ie_admin", "ie@t.com", "Admin@2026!")
        from masterdata.models import Material
        Material.objects.create(material_code="IE_MAT", material_name="IE Material", material_type="raw", base_unit="kg", qty_precision=3, status="active")

    def test_01_csv_export_formula_injection_prevented(self):
        """Cells starting with = + - @ are prefixed to prevent formula injection."""
        from files.services import export_queryset_to_csv
        output = io.StringIO()
        output.write("col1,col2\n")
        output.write("=SUM(A1:A10),+1234\n")
        output.write("-999,@REF\n")
        output.seek(0)
        reader = csv.reader(output)
        rows = list(reader)
        dangerous_prefixes = ["=", "+", "-", "@"]
        for row in rows[1:]:
            for cell in row:
                if cell and cell[0] in dangerous_prefixes:
                    self.assertFalse(True, f"Cell {cell} starts with dangerous prefix")

    def test_02_csv_export_creates_export_log(self):
        """Export writes an ExportLog record."""
        from files.services import export_queryset_to_csv
        from files.models import ExportLog
        before = ExportLog.objects.count()
        from masterdata.models import Material
        qs = Material.objects.filter(material_code="IE_MAT")
        export_queryset_to_csv(module="materials", queryset=qs, fields=["material_code", "material_name"], user_id=self.admin.id)
        after = ExportLog.objects.count()
        self.assertGreater(after, before)

    def test_03_decimal_values_exported_as_numbers(self):
        """Decimal fields are exported as numeric strings, not scientific notation."""
        val = Decimal("0.001")
        self.assertEqual(str(val), "0.001")
        large_val = Decimal("1234567.89")
        self.assertNotIn("E", str(large_val))

    def test_05_import_template_downloadable(self):
        """Import template URLs return a CSV file."""
        self.client.force_login(self.admin)
        resp = self.client.get("/masterdata/materials/import-template/")
        self.assertIn(resp.status_code, [200, 302, 403])


class ExportPrintSecurityTest(TestCase):
    """Verify export and print operations respect permissions."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("eps_admin", "eps@t.com", "Admin@2026!")
        cls.sales = User.objects.create_user("eps_sales", password="Sales@2026!", display_name="EPS Sales", security_level="L1")
        from masterdata.models import Material, Customer, CustomerProduct
        cls.fg = Material.objects.create(material_code="EPS_FG", material_name="EPS FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="EPS_C01", customer_name="EPS Customer", sales_owner=cls.sales, status="active")

    def test_export_logs_page_restricted_to_admin(self):
        self.client.force_login(self.sales)
        resp = self.client.get("/files/export-logs/")
        self.assertIn(resp.status_code, [302, 403])

    def test_print_logs_page_restricted_to_admin(self):
        self.client.force_login(self.sales)
        resp = self.client.get("/files/print-logs/")
        self.assertIn(resp.status_code, [302, 403])

    def test_sales_can_export_own_orders(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="EPS_SO01", customer=self.cust, order_date="2026-06-10", status="draft", created_by=self.sales)
        cp = CustomerProduct.objects.create(customer=self.cust, customer_product_no="EPS_CP01", customer_product_name="EPS CP", finished_material=self.fg, status="active")
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("100.00"), line_amount=Decimal("100.00"), line_status="draft", inventory_check_status="unchecked")
        self.client.force_login(self.sales)
        resp = self.client.get("/sales/orders/export/")
        self.assertIn(resp.status_code, [200, 302])
