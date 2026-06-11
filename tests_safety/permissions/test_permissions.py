from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.test import Client
from django.contrib.auth import get_user_model

User = get_user_model()


class PermissionMatrixTest(TestCase):
    """Verify role-based access control (7 roles x key actions)."""

    @classmethod
    def setUpTestData(cls):
        from accounts.models import Role, Permission
        cls.admin = User.objects.create_superuser("pm_admin", "pm@t.com", "Admin@2026!")
        cls.sales = User.objects.create_user("pm_sales", password="Sales@2026!", display_name="Sales", security_level="L1")
        cls.sales_mgr = User.objects.create_user("pm_salesmgr", password="Sm@2026!", display_name="Sales Mgr", security_level="L2")
        cls.purchase = User.objects.create_user("pm_purch", password="Purch@2026!", display_name="Purch", security_level="L1")
        cls.warehouse = User.objects.create_user("pm_wh", password="Wh@2026!", display_name="WH", security_level="L1")
        cls.finance = User.objects.create_user("pm_fin", password="Fin@2026!", display_name="Fin", security_level="L2")
        cls.production = User.objects.create_user("pm_prod", password="Prod@2026!", display_name="Prod", security_level="L1")
        from masterdata.models import Material, Customer, CustomerProduct
        cls.fg = Material.objects.create(material_code="PM_FG", material_name="PM FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.cust = Customer.objects.create(customer_no="PM_C01", customer_name="PM Customer", sales_owner=cls.sales, status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="PM_CP01", customer_product_name="PM CP", finished_material=cls.fg, status="active")

    def test_01_warehouse_cannot_access_finance_pages(self):
        self.client.force_login(self.warehouse)
        resp = self.client.get("/finance/customer-receipts/")
        self.assertIn(resp.status_code, [302, 403])

    def test_02_purchase_cannot_access_sales_create(self):
        self.client.force_login(self.purchase)
        resp = self.client.get("/sales/orders/new/")
        self.assertIn(resp.status_code, [302, 403])

    def test_03_sales_A_cannot_see_sales_B_customer_data(self):
        sales_b = User.objects.create_user("pm_sales_b", password="SB@2026!", display_name="Sales B", security_level="L1")
        cust_b = Customer.objects.create(customer_no="PM_CB01", customer_name="PM Cust B", sales_owner=sales_b, status="active")
        from sales.models import SalesOrder, SalesOrderItem
        so_b = SalesOrder.objects.create(sales_order_no="PM_SOB01", customer=cust_b, order_date="2026-06-10", status="confirmed", created_by=sales_b)
        soi_b = SalesOrderItem.objects.create(sales_order=so_b, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("1"), unit_price=Decimal("100.00"), line_amount=Decimal("100.00"), line_status="confirmed", inventory_check_status="sufficient")
        self.client.force_login(self.sales)
        resp = self.client.get(reverse("sales:sales_order_detail", args=[so_b.id]))
        self.assertIn(resp.status_code, [302, 403, 404])

    def test_04_warehouse_cannot_see_sales_price_on_order(self):
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(sales_order_no="PM_SO01", customer=self.cust, order_date="2026-06-10", status="confirmed", total_amount=Decimal("1000.00"), created_by=self.sales)
        soi = SalesOrderItem.objects.create(sales_order=so, line_no=1, customer_product=self.cp, finished_material=self.fg, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="confirmed", inventory_check_status="sufficient")
        self.client.force_login(self.warehouse)
        resp = self.client.get(reverse("sales:sales_order_detail", args=[so.id]))
        content = resp.content.decode()
        self.assertNotIn("100.00", content)

    def test_05_production_cannot_access_finance_module(self):
        self.client.force_login(self.production)
        resp = self.client.get("/finance/")
        self.assertIn(resp.status_code, [302, 403])

    def test_06_unauthenticated_cannot_access_any_page(self):
        c = Client()
        resp = c.get("/sales/orders/")
        self.assertEqual(resp.status_code, 302)

    def test_07_disabled_user_cannot_login(self):
        u = User.objects.create_user("pm_disabled", password="Dis@2026!", status="inactive")
        c = Client()
        resp = c.post(reverse("login"), {"username": "pm_disabled", "password": "Dis@2026!"})
        self.assertNotEqual(resp.status_code, 302)

    def test_08_locked_user_cannot_login(self):
        u = User.objects.create_user("pm_locked", password="Lk@2026!", status="locked")
        c = Client()
        resp = c.post(reverse("login"), {"username": "pm_locked", "password": "Lk@2026!"})
        self.assertNotEqual(resp.status_code, 302)

    def test_09_admin_can_view_health_check(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)

    def test_10_non_admin_rejected_at_health_check(self):
        self.client.force_login(self.sales)
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 403)
