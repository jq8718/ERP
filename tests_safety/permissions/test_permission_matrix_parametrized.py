from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode, ensure_default_permissions
from finance.models import CustomerReceipt
from inventory.models import Inventory, InventoryBatch, LocationTransfer, WarehouseLocation
from masterdata.models import Customer, CustomerProduct, Material, Supplier
from purchase.models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt
from sales.models import SalesOrder, SalesOrderItem


User = get_user_model()


class ParameterizedPermissionMatrixTest(TestCase):
    """Parameterized coverage for the high-risk role/action permission matrix."""

    @classmethod
    def setUpTestData(cls):
        ensure_default_permissions()
        cls.password = "Matrix@2026!"
        cls.no_perm_user = User.objects.create_user("matrix_no_perm", password=cls.password)
        cls.sales_user = cls._user_with_permissions("matrix_sales", [PermissionCode.SALES_PROCESS])
        cls.purchase_user = cls._user_with_permissions("matrix_purchase", [PermissionCode.PURCHASE_PROCESS])
        cls.inventory_user = cls._user_with_permissions("matrix_inventory", [PermissionCode.INVENTORY_PROCESS])
        cls.production_user = cls._user_with_permissions("matrix_production", [PermissionCode.PRODUCTION_PROCESS])
        cls.finance_user = cls._user_with_permissions(
            "matrix_finance",
            [PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS, PermissionCode.SALES_VIEW_ALL],
        )
        cls.amount_only_user = cls._user_with_permissions("matrix_amount", [PermissionCode.FINANCE_VIEW_AMOUNT])
        cls.admin_user = cls._user_with_permissions("matrix_admin", [PermissionCode.ADMIN_PERMISSION_MANAGE])

        cls.customer = Customer.objects.create(
            customer_no="MAT-C001",
            customer_name="Matrix Customer",
            sales_owner=cls.sales_user,
            status=Customer.CustomerStatus.ACTIVE,
        )
        cls.finished = Material.objects.create(
            material_code="MAT-FG001",
            material_name="Matrix Finished",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
            qty_precision=0,
            status=Material.MaterialStatus.ACTIVE,
        )
        cls.raw = Material.objects.create(
            material_code="MAT-RM001",
            material_name="Matrix Raw",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
            qty_precision=3,
            status=Material.MaterialStatus.ACTIVE,
        )
        cls.customer_product = CustomerProduct.objects.create(
            customer=cls.customer,
            customer_product_no="MAT-CP001",
            customer_product_name="Matrix CP",
            finished_material=cls.finished,
            default_sale_price=Decimal("10.00"),
            status=CustomerProduct.ProductStatus.ACTIVE,
        )
        cls.location_a = WarehouseLocation.objects.create(location_code="MAT-A01", location_name="Matrix A01")
        cls.location_b = WarehouseLocation.objects.create(location_code="MAT-B01", location_name="Matrix B01")
        cls.batch = InventoryBatch.objects.create(
            batch_no="MAT-BATCH-001",
            material=cls.raw,
            location=cls.location_a,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at="2026-06-01T00:00:00Z",
            initial_qty=Decimal("10"),
            remaining_qty=Decimal("10"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=cls.raw,
            location=cls.location_a,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("10"),
        )
        Inventory.objects.create(
            material=cls.raw,
            location=cls.location_b,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("0"),
        )
        cls.sales_order = SalesOrder.objects.create(
            sales_order_no="MAT-SO001",
            customer=cls.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.DRAFT,
            total_amount=Decimal("10.00"),
            created_by=cls.sales_user,
        )
        cls.sales_order_item = SalesOrderItem.objects.create(
            sales_order=cls.sales_order,
            line_no=1,
            customer_product=cls.customer_product,
            finished_material=cls.finished,
            order_qty=Decimal("1"),
            unit_price=Decimal("10.00"),
            line_amount=Decimal("10.00"),
            line_status=SalesOrderItem.LineStatus.DRAFT,
            inventory_check_status=SalesOrderItem.InventoryCheckStatus.UNCHECKED,
        )
        cls.supplier = Supplier.objects.create(
            supplier_no="MAT-SUP001",
            supplier_name="Matrix Supplier",
            status=Supplier.SupplierStatus.ACTIVE,
        )
        cls.purchase_order = PurchaseOrder.objects.create(
            purchase_order_no="MAT-PO001",
            supplier=cls.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("5.00"),
        )
        cls.purchase_order_item = PurchaseOrderItem.objects.create(
            purchase_order=cls.purchase_order,
            line_no=1,
            material=cls.raw,
            order_qty=Decimal("1"),
            received_qty=Decimal("0"),
            unit_price=Decimal("5.00"),
            line_amount=Decimal("5.00"),
        )
        cls.purchase_receipt = PurchaseReceipt.objects.create(
            purchase_receipt_no="MAT-PR001",
            purchase_order=cls.purchase_order,
            supplier=cls.supplier,
            receipt_date=timezone.localdate(),
            status=PurchaseReceipt.Status.PENDING_RECEIVE,
        )
        cls.transfer = LocationTransfer.objects.create(
            transfer_no="MAT-LT001",
            material=cls.raw,
            batch=cls.batch,
            from_location=cls.location_a,
            to_location=cls.location_b,
            transfer_qty=Decimal("1"),
            status=LocationTransfer.TransferStatus.DRAFT,
        )
        cls.customer_receipt = CustomerReceipt.objects.create(
            receipt_no="MAT-RC001",
            customer=cls.customer,
            receipt_date=timezone.localdate(),
            receipt_amount=Decimal("10.00"),
            unallocated_amount=Decimal("10.00"),
            status=CustomerReceipt.Status.PENDING_APPROVAL,
        )

    @classmethod
    def _user_with_permissions(cls, username, permission_codes):
        user = User.objects.create_user(username, password=cls.password, security_level="L2")
        role = Role.objects.create(role_code=f"{username}-role", role_name=f"{username} role")
        role.permissions.add(*Permission.objects.filter(permission_code__in=permission_codes))
        user.roles.add(role)
        return user

    def test_action_endpoints_reject_users_without_required_permission(self):
        cases = [
            (
                "sales submit",
                self.no_perm_user,
                "sales:sales_order_submit",
                [self.sales_order.id],
                {},
            ),
            (
                "purchase receipt confirm",
                self.no_perm_user,
                "purchase:purchase_receipt_confirm",
                [self.purchase_receipt.id],
                {"current_password": self.password},
            ),
            (
                "inventory transfer confirm",
                self.no_perm_user,
                "inventory:location_transfer_confirm",
                [self.transfer.id],
                {"current_password": self.password},
            ),
            (
                "finance receipt confirm",
                self.amount_only_user,
                "finance:customer_receipt_confirm",
                [self.customer_receipt.id],
                {"current_password": self.password},
            ),
        ]

        for label, user, url_name, args, payload in cases:
            with self.subTest(label=label):
                self.client.force_login(user)
                response = self.client.post(reverse(url_name, args=args), payload)
                self.assertEqual(response.status_code, 403)

    def test_action_endpoints_accept_users_with_required_permission(self):
        cases = [
            (
                "sales create page",
                self.sales_user,
                "sales:sales_order_create",
                [],
                200,
            ),
            (
                "purchase receipt detail page",
                self.purchase_user,
                "purchase:purchase_receipt_detail",
                [self.purchase_receipt.id],
                200,
            ),
            (
                "inventory transfer detail page",
                self.inventory_user,
                "inventory:location_transfer_detail",
                [self.transfer.id],
                200,
            ),
            (
                "finance receipt detail page",
                self.finance_user,
                "finance:customer_receipt_detail",
                [self.customer_receipt.id],
                200,
            ),
            (
                "role admin page",
                self.admin_user,
                "role_list",
                [],
                200,
            ),
        ]

        for label, user, url_name, args, expected_status in cases:
            with self.subTest(label=label):
                self.client.force_login(user)
                response = self.client.get(reverse(url_name, args=args))
                self.assertEqual(response.status_code, expected_status)

    def test_field_level_amount_permission_masks_sales_amounts(self):
        self.client.force_login(self.sales_user)
        response = self.client.get(reverse("sales:sales_order_detail", args=[self.sales_order.id]))
        self.assertContains(response, "******")
        self.assertNotContains(response, "10.0000")

        self.client.force_login(self.finance_user)
        response = self.client.get(reverse("sales:sales_order_detail", args=[self.sales_order.id]))
        self.assertContains(response, "10.0000")

    def test_data_scope_sales_user_cannot_view_another_sales_order(self):
        other_user = User.objects.create_user("matrix_other_sales", password=self.password)
        other_customer = Customer.objects.create(
            customer_no="MAT-C999",
            customer_name="Other Matrix Customer",
            sales_owner=other_user,
            status=Customer.CustomerStatus.ACTIVE,
        )
        other_order = SalesOrder.objects.create(
            sales_order_no="MAT-SO999",
            customer=other_customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.CONFIRMED,
        )

        self.client.force_login(self.sales_user)
        response = self.client.get(reverse("sales:sales_order_detail", args=[other_order.id]))
        self.assertEqual(response.status_code, 404)
