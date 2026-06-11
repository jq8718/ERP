import threading
import time
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.test import Client
from django.contrib.auth import get_user_model

User = get_user_model()


class PermissionBypassTest(TestCase):
    """Verify critical operations resist permission bypass via direct POST."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("perm_admin", "a@t.com", "Admin@2026!")
        cls.sales = User.objects.create_user("perm_sales", password="Sales@2026!", display_name="S", security_level="L1")
        cls.warehouse = User.objects.create_user("perm_wh", password="Wh@2026!", display_name="W", security_level="L1")
        cls.finance = User.objects.create_user("perm_fin", password="Fin@2026!", display_name="F", security_level="L2")

        from masterdata.models import Customer, CustomerProduct, Material
        cls.fg = Material.objects.create(material_code="PE_FG", material_name="PE FG", material_type="finished", base_unit="pcs", qty_precision=0, status="active")
        cls.rm = Material.objects.create(material_code="PE_RM", material_name="PE RM", material_type="raw", base_unit="kg", qty_precision=3, status="active")
        cls.cust = Customer.objects.create(customer_no="PE_C01", customer_name="PE Customer", sales_owner=cls.sales, status="active")
        cls.cp = CustomerProduct.objects.create(customer=cls.cust, customer_product_no="PE_CP01", customer_product_name="PE CP", finished_material=cls.fg, status="active")

        from bom.models import Bom, BomItem
        cls.bom = Bom.objects.create(bom_no="PE_BOM", finished_material=cls.fg, bom_version="A", base_qty=Decimal("1"), status="enabled", is_default=True)
        cls.bi = BomItem.objects.create(bom=cls.bom, line_no=1, component_material=cls.rm, usage_qty=Decimal("2.0"), usage_unit="kg", is_required=True)

        from inventory.models import WarehouseLocation, InventoryBatch, Inventory
        cls.loc = WarehouseLocation.objects.create(location_code="PE_LOC", location_name="PE Loc")
        cls.batch = InventoryBatch.objects.create(batch_no="PE_B01", material=cls.rm, location=cls.loc, inventory_type="available", received_at="2026-06-01T00:00:00Z", initial_qty=Decimal("10000"), remaining_qty=Decimal("10000"), batch_status="in_stock")
        Inventory.objects.create(material=cls.rm, location=cls.loc, inventory_type="available", qty=Decimal("10000"))
        Inventory.objects.create(material=cls.fg, location=cls.loc, inventory_type="available", qty=Decimal("100"))

        from sales.models import SalesOrder, SalesOrderItem
        cls.so = SalesOrder.objects.create(sales_order_no="PE_SO01", customer=cls.cust, order_date="2026-06-10", status="draft", created_by=cls.sales)
        cls.soi = SalesOrderItem.objects.create(sales_order=cls.so, line_no=1, customer_product=cls.cp, finished_material=cls.fg, order_qty=Decimal("10"), unit_price=Decimal("100.00"), line_amount=Decimal("1000.00"), line_status="draft", inventory_check_status="unchecked")

    def test_no_permission_user_cannot_access_admin_pages(self):
        """Regular users should not access user management pages."""
        self.client.force_login(self.sales)
        resp = self.client.get(reverse("account_user_list"))
        self.assertIn(resp.status_code, [302, 403])

    def test_unauth_cannot_access_health_check(self):
        """Health check page restricted to admins."""
        self.client.force_login(self.sales)
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 403)

    def test_unauth_cannot_access_audit_logs(self):
        """Audit logs restricted to admins."""
        self.client.force_login(self.sales)
        resp = self.client.get("/audit-logs/")
        self.assertIn(resp.status_code, [302, 403])

    def test_disabled_user_login_rejected(self):
        """Users with status=inactive cannot log in."""
        u = User.objects.create_user("disabled_u", password="Test@2026!", status="inactive")
        c = Client()
        resp = c.post(reverse("login"), {"username": "disabled_u", "password": "Test@2026!"})
        self.assertNotEqual(resp.status_code, 302)

    def test_locked_user_login_rejected(self):
        """Users with status=locked cannot log in."""
        u = User.objects.create_user("locked_u", password="Test@2026!", status="locked")
        c = Client()
        resp = c.post(reverse("login"), {"username": "locked_u", "password": "Test@2026!"})
        self.assertNotEqual(resp.status_code, 302)

    def test_deleted_user_login_rejected(self):
        """Users with is_deleted=true cannot log in."""
        u = User.objects.create_user("deleted_u", password="Test@2026!", status="active", is_deleted=True)
        c = Client()
        resp = c.post(reverse("login"), {"username": "deleted_u", "password": "Test@2026!"})
        self.assertNotEqual(resp.status_code, 302)

    def test_blank_password_login_rejected(self):
        """Empty passwords should be rejected."""
        c = Client()
        resp = c.post(reverse("login"), {"username": "perm_sales", "password": ""})
        self.assertNotEqual(resp.status_code, 302)

    def test_404_page_renders_for_nonexistent_url(self):
        """Nonexistent URLs show custom 404, not debug traceback."""
        self.client.force_login(self.admin)
        resp = self.client.get("/this/does/not/exist/")
        self.assertEqual(resp.status_code, 404)

    def test_form_injection_in_remark_field_escaped(self):
        """XSS payloads in remark fields are HTML-escaped."""
        from masterdata.models import Material
        mat = Material.objects.create(material_code="XSS_01", material_name="XSS", material_type="raw", base_unit="kg", qty_precision=0, status="active", remark='<script>alert(1)</script>')
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("masterdata:material_detail", args=[mat.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("<script>", resp.content.decode())

    def test_sql_injection_in_search_safe(self):
        """Search filters are ORM-safe from SQL injection."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("masterdata:material_list") + "?q=' OR 1=1 --")
        self.assertEqual(resp.status_code, 200)

    def test_direct_admin_url_restricted_to_superusers(self):
        """Django /admin/ is restricted to superusers."""
        self.client.force_login(self.sales)
        resp = self.client.get("/admin/")
        self.assertNotEqual(resp.status_code, 200)

    def test_price_field_cannot_be_negative_via_form(self):
        """Negative price in form should be rejected or corrected."""
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("sales:sales_order_edit", args=[self.so.id]),
            {"customer": self.cust.id, "order_date": "2026-06-10",
             "items-0-id": self.soi.id, "items-0-customer_product": self.cp.id,
             "items-0-order_qty": "10", "items-0-unit_price": "-500.00",
             "action": "save", "edit_reason": "test"},
            follow=True,
        )
        self.soi.refresh_from_db()
        self.assertGreaterEqual(self.soi.unit_price, Decimal("0"))

    def test_status_cannot_be_set_to_completed_directly(self):
        """Users cannot set order to 'completed' via form tampering."""
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("sales:sales_order_edit", args=[self.so.id]),
            {"customer": self.cust.id, "order_date": "2026-06-10",
             "items-0-id": self.soi.id, "items-0-customer_product": self.cp.id,
             "items-0-order_qty": "10", "items-0-unit_price": "100.00",
             "status": "completed", "action": "save", "edit_reason": "test"},
            follow=True,
        )
        self.so.refresh_from_db()
        self.assertNotEqual(self.so.status, "completed")

    def test_csrf_protection_on_post(self):
        """POST without CSRF token rejected."""
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        resp = c.post(reverse("sales:sales_order_create"), {})
        self.assertEqual(resp.status_code, 403)

    def test_wrong_password_rejected(self):
        """Incorrect passwords are rejected."""
        c = Client()
        resp = c.post(reverse("login"), {"username": "perm_sales", "password": "WrongPassword123!"})
        self.assertNotEqual(resp.status_code, 302)
