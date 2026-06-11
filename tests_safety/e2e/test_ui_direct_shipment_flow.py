from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode, ensure_default_permissions
from bom.models import Bom
from finance.models import CustomerReceipt, CustomerReceiptAllocation
from inventory.models import Inventory, InventoryBatch, InventoryTransaction, WarehouseLocation
from masterdata.models import Customer, CustomerProduct, Material
from sales.models import SalesOrder, SalesOrderItem, SalesShipment


User = get_user_model()


class DirectShipmentUiJourneyTest(TestCase):
    """Browser-client level journey for stock-sufficient order-to-cash flow."""

    @classmethod
    def setUpTestData(cls):
        ensure_default_permissions()
        cls.user_password = "UiFlow@2026!"
        cls.operator = User.objects.create_user(
            "ui_flow_operator",
            password=cls.user_password,
            display_name="UI Flow Operator",
            security_level="L2",
        )
        role = Role.objects.create(role_code="ui-flow-role", role_name="UI Flow Role")
        role.permissions.add(
            Permission.objects.get(permission_code=PermissionCode.SALES_PROCESS),
            Permission.objects.get(permission_code=PermissionCode.FINANCE_VIEW_AMOUNT),
            Permission.objects.get(permission_code=PermissionCode.FINANCE_PAYMENT_PROCESS),
        )
        cls.operator.roles.add(role)
        cls.customer = Customer.objects.create(
            customer_no="UI-C001",
            customer_name="UI Customer",
            sales_owner=cls.operator,
            status=Customer.CustomerStatus.ACTIVE,
        )
        cls.finished = Material.objects.create(
            material_code="UI-FG001",
            material_name="UI Finished Good",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
            qty_precision=0,
            status=Material.MaterialStatus.ACTIVE,
        )
        cls.customer_product = CustomerProduct.objects.create(
            customer=cls.customer,
            customer_product_no="UI-CP001",
            customer_product_name="UI Customer Product",
            finished_material=cls.finished,
            default_sale_price=Decimal("25.00"),
            status=CustomerProduct.ProductStatus.ACTIVE,
        )
        Bom.objects.create(
            bom_no="UI-BOM001",
            finished_material=cls.finished,
            bom_version="A",
            base_qty=Decimal("1"),
            status=Bom.BomStatus.ENABLED,
            is_default=True,
            effective_date="2026-06-01",
        )
        cls.location = WarehouseLocation.objects.create(
            location_code="UI-A01",
            location_name="UI Location A01",
        )
        cls.batch = InventoryBatch.objects.create(
            batch_no="UI-FG-B001",
            material=cls.finished,
            location=cls.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at="2026-06-01T00:00:00Z",
            initial_qty=Decimal("50"),
            remaining_qty=Decimal("50"),
            cost_price=Decimal("9.500000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=cls.finished,
            location=cls.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("50"),
        )

    def test_stock_sufficient_order_to_shipment_to_receipt_via_pages(self):
        self.client.force_login(self.operator)

        create_response = self.client.post(
            reverse("sales:sales_order_create"),
            {
                "customer": self.customer.id,
                "customer_address": "",
                "order_date": "2026-06-12",
                "delivery_date": "2026-06-20",
                "remark": "UI journey",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-customer_product": self.customer_product.id,
                "items-0-order_qty": "4",
                "items-0-unit_price": "25.00",
                "items-1-customer_product": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-2-customer_product": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "action": "submit",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        order = SalesOrder.objects.get()
        self.assertEqual(order.status, SalesOrder.Status.PENDING_APPROVAL)
        self.assertEqual(order.total_amount, Decimal("100.00"))

        confirm_page = self.client.get(reverse("sales:sales_order_detail", args=[order.id]))
        self.assertContains(confirm_page, "审核确认")
        confirm_response = self.client.post(
            reverse("sales:sales_order_confirm", args=[order.id]),
            {"current_password": self.user_password},
        )
        self.assertEqual(confirm_response.status_code, 302)
        order.refresh_from_db()
        item = order.items.get()
        self.assertEqual(order.status, SalesOrder.Status.CONFIRMED)
        self.assertEqual(item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SUFFICIENT)
        self.assertEqual(item.line_status, SalesOrderItem.LineStatus.CONFIRMED)

        shipment_page = self.client.get(reverse("sales:sales_order_detail", args=[order.id]))
        self.assertContains(shipment_page, "生成出库单")
        create_shipment_response = self.client.post(reverse("sales:sales_order_create_shipment", args=[order.id]))
        self.assertEqual(create_shipment_response.status_code, 302)
        shipment = SalesShipment.objects.get()
        self.assertEqual(shipment.status, SalesShipment.Status.PENDING_CONFIRM)
        self.assertEqual(shipment.items.get().batch, self.batch)

        confirm_shipment_response = self.client.post(
            reverse("sales:sales_shipment_confirm", args=[shipment.id]),
            {"current_password": self.user_password},
        )
        self.assertEqual(confirm_shipment_response.status_code, 302)
        shipment.refresh_from_db()
        item.refresh_from_db()
        self.batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.finished, location=self.location)
        self.assertEqual(shipment.status, SalesShipment.Status.SHIPPED)
        self.assertEqual(item.shipped_qty, Decimal("4.0000"))
        self.assertEqual(self.batch.remaining_qty, Decimal("46.0000"))
        self.assertEqual(inventory.qty, Decimal("46.0000"))
        self.assertTrue(
            InventoryTransaction.objects.filter(
                transaction_type=InventoryTransaction.TransactionType.SALES_OUT,
                source_doc_id=shipment.id,
            ).exists()
        )

        receipt_create_response = self.client.post(
            reverse("finance:customer_receipt_create"),
            {
                "customer": self.customer.id,
                "receipt_date": "2026-06-25",
                "receipt_amount": "100.00",
                "receipt_method": CustomerReceipt.ReceiptMethod.TRANSFER,
                "remark": "UI payment",
            },
        )
        self.assertEqual(receipt_create_response.status_code, 302)
        receipt = CustomerReceipt.objects.get()
        self.assertEqual(receipt.status, CustomerReceipt.Status.PENDING_APPROVAL)

        receipt_page = self.client.get(reverse("finance:customer_receipt_detail", args=[receipt.id]))
        self.assertContains(receipt_page, order.sales_order_no)
        confirm_receipt_response = self.client.post(
            reverse("finance:customer_receipt_confirm", args=[receipt.id]),
            {
                "sales_order_id": [str(order.id)],
                "sales_order_allocated_amount": ["100.00"],
                "current_password": self.user_password,
            },
        )
        self.assertEqual(confirm_receipt_response.status_code, 302)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CustomerReceipt.Status.CONFIRMED)
        self.assertEqual(receipt.unallocated_amount, Decimal("0.00"))
        self.assertEqual(
            CustomerReceiptAllocation.objects.get(customer_receipt=receipt, sales_order=order).allocated_amount,
            Decimal("100.00"),
        )
