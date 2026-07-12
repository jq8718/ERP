from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from bom.models import Bom, BomItem
from files.models import Attachment, ExportLog, ImportJob, PrintLog
from inventory.models import Inventory, InventoryBatch, InventoryTransaction, WarehouseLocation
from masterdata.models import Customer, CustomerProduct, Material, MaterialSupplierPrice, Supplier
from purchase.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseRequest,
    PurchaseRequestItem,
    SupplierReturn,
    SupplierReturnItem,
)
from purchase.services import confirm_purchase_receipt, confirm_supplier_return_shipment, create_purchase_order_from_request
from sales.models import SalesOrder, SalesOrderItem, ShortageAlert
from sales.services import confirm_sales_order
from system.models import AuditLog, PendingEvent
from system.services import process_pending_events


class PurchaseReceiptServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="buyer", password="x")
        self.customer = Customer.objects.create(customer_no="C001", customer_name="测试客户")
        self.supplier = Supplier.objects.create(supplier_no="S001", supplier_name="测试供应商")
        self.location = WarehouseLocation.objects.create(location_code="A01", location_name="A01")
        self.finished = Material.objects.create(
            material_code="FG001",
            material_name="成品 1",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
            qty_precision=0,
        )
        self.raw = Material.objects.create(
            material_code="RM001",
            material_name="原料 1",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            qty_precision=0,
        )
        self.customer_product = CustomerProduct.objects.create(
            customer=self.customer,
            customer_product_no="CP001",
            customer_product_name="客户产品 1",
            finished_material=self.finished,
        )
        self.bom = Bom.objects.create(
            bom_no="BOM001",
            finished_material=self.finished,
            bom_version="V1",
            status=Bom.BomStatus.ENABLED,
            enabled_at=timezone.now(),
        )
        BomItem.objects.create(
            bom=self.bom,
            line_no=1,
            component_material=self.raw,
            usage_qty=Decimal("2.000000"),
            usage_unit="pcs",
            loss_rate=Decimal("0"),
            is_required=True,
        )
        self._grant_permission(PermissionCode.PURCHASE_VIEW)

    def _grant_permission(self, permission_code: str):
        permission_types = {
            PermissionCode.FINANCE_VIEW_AMOUNT: Permission.PermissionType.FIELD,
            PermissionCode.PURCHASE_VIEW: Permission.PermissionType.MODULE,
            PermissionCode.PURCHASE_PROCESS: Permission.PermissionType.ACTION,
        }
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_types.get(permission_code, Permission.PermissionType.ACTION),
            },
        )
        role = Role.objects.create(role_code=f"purchase-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def _grant_permission_to(self, user, permission_code: str):
        permission_types = {
            PermissionCode.FINANCE_VIEW_AMOUNT: Permission.PermissionType.FIELD,
            PermissionCode.PURCHASE_VIEW: Permission.PermissionType.MODULE,
            PermissionCode.PURCHASE_PROCESS: Permission.PermissionType.ACTION,
        }
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_types.get(permission_code, Permission.PermissionType.ACTION),
            },
        )
        role = Role.objects.create(role_code=f"purchase-role-{permission_code}-{user.id}", role_name=permission_code)
        role.permissions.add(permission)
        user.roles.add(role)
        return role

    def _purchase_receipt(self, accepted_qty=Decimal("20.0000")):
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO001",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("20.00"),
        )
        order_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=accepted_qty,
            received_qty=Decimal("0"),
            unit_price=Decimal("1.000000"),
            line_amount=Decimal("20.00"),
        )
        receipt = PurchaseReceipt.objects.create(
            purchase_receipt_no="GR001",
            purchase_order=order,
            supplier=self.supplier,
            receipt_date=timezone.localdate(),
            status=PurchaseReceipt.Status.PENDING_RECEIVE,
        )
        receipt_item = PurchaseReceiptItem.objects.create(
            purchase_receipt=receipt,
            purchase_order_item=order_item,
            material=self.raw,
            received_qty=accepted_qty,
            accepted_qty=accepted_qty,
            unit_price=Decimal("1.000000"),
            location=self.location,
        )
        return order, order_item, receipt, receipt_item

    def _supplier_return(self, qty=Decimal("4.0000")):
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))
        batch = InventoryBatch.objects.create(
            batch_no="B001",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("20.0000"),
            remaining_qty=Decimal("20.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("20.0000"),
        )
        supplier_return = SupplierReturn.objects.create(
            supplier_return_no="SR001",
            supplier=self.supplier,
            purchase_receipt=receipt,
            return_date=timezone.localdate(),
            status=SupplierReturn.Status.CONFIRMED,
            return_amount=(qty * Decimal("1.000000")).quantize(Decimal("0.01")),
            created_by=self.user,
        )
        SupplierReturnItem.objects.create(
            supplier_return=supplier_return,
            purchase_receipt_item=receipt_item,
            material=self.raw,
            return_qty=qty,
            unit_price=Decimal("1.000000"),
            return_amount=(qty * Decimal("1.000000")).quantize(Decimal("0.01")),
            batch=batch,
            location=self.location,
            return_reason="质量问题",
        )
        return supplier_return, batch

    def _received_purchase_receipt_for_return(self, batch_no="B-RETURN-IMPORT", remaining_qty=Decimal("20.0000")):
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))
        order.status = PurchaseOrder.Status.RECEIVED
        order_item.received_qty = Decimal("20.0000")
        order_item.line_status = PurchaseOrderItem.LineStatus.RECEIVED
        receipt.status = PurchaseReceipt.Status.RECEIVED
        order.save(update_fields=["status"])
        order_item.save(update_fields=["received_qty", "line_status"])
        receipt.save(update_fields=["status"])
        batch = InventoryBatch.objects.create(
            batch_no=batch_no,
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=remaining_qty,
            remaining_qty=remaining_qty,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        receipt_item.batch = batch
        receipt_item.save(update_fields=["batch"])
        Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=remaining_qty,
        )
        return order, order_item, receipt, receipt_item, batch

    def _sales_order_with_shortage(self):
        sales_order = SalesOrder.objects.create(
            sales_order_no="SO001",
            customer=self.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.PENDING_APPROVAL,
            total_amount=Decimal("100.00"),
        )
        item = SalesOrderItem.objects.create(
            sales_order=sales_order,
            line_no=1,
            customer_product=self.customer_product,
            finished_material=self.finished,
            order_qty=Decimal("10.0000"),
            unit_price=Decimal("10.0000"),
            line_amount=Decimal("100.00"),
            line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
        )
        confirm_sales_order(sales_order.id, self.user.id)
        return sales_order, item

    def test_confirm_purchase_receipt_creates_batch_inventory_and_transaction(self):
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        result = confirm_purchase_receipt(receipt.id, self.user.id, "receipt-1")

        self.assertTrue(result.success)
        receipt.refresh_from_db()
        order.refresh_from_db()
        order_item.refresh_from_db()
        receipt_item.refresh_from_db()
        inventory = Inventory.objects.get(material=self.raw, location=self.location)
        self.assertEqual(receipt.status, PurchaseReceipt.Status.RECEIVED)
        self.assertEqual(order.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(order_item.received_qty, Decimal("20.0000"))
        self.assertEqual(order_item.line_status, PurchaseOrderItem.LineStatus.RECEIVED)
        self.assertIsNotNone(receipt_item.batch)
        self.assertEqual(inventory.qty, Decimal("20.0000"))
        self.assertEqual(InventoryBatch.objects.get().remaining_qty, Decimal("20.0000"))
        self.assertEqual(InventoryTransaction.objects.get().transaction_type, InventoryTransaction.TransactionType.PURCHASE_IN)
        self.assertTrue(PendingEvent.objects.filter(event_type="purchase_received").exists())

    def test_confirm_purchase_receipt_enqueues_async_shortage_recheck(self):
        sales_order, sales_item = self._sales_order_with_shortage()
        alert = ShortageAlert.objects.get()
        self.assertEqual(alert.shortage_qty, Decimal("20.0000"))
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        result = confirm_purchase_receipt(receipt.id, self.user.id, "receipt-2")

        self.assertTrue(result.success)
        sales_item.refresh_from_db()
        alert.refresh_from_db()
        purchase_event = PendingEvent.objects.get(event_type="purchase_received")
        self.assertEqual(purchase_event.payload["sales_order_item_ids"], [sales_item.id])
        self.assertEqual(sales_item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SHORTAGE)
        self.assertEqual(alert.status, ShortageAlert.Status.UNPROCESSED)
        self.assertFalse(PendingEvent.objects.filter(event_type="shortage_kitted").exists())

        event_result = process_pending_events(event_type="purchase_received")

        self.assertTrue(event_result.success)
        sales_item.refresh_from_db()
        alert.refresh_from_db()
        self.assertEqual(sales_item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.KITTED)
        self.assertEqual(alert.status, ShortageAlert.Status.KITTED)
        self.assertTrue(PendingEvent.objects.filter(event_type="shortage_kitted").exists())

    def test_purchase_receipt_detail_renders_confirm_action(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        response = self.client.get(f"/purchase/receipts/{receipt.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, receipt.purchase_receipt_no)
        self.assertContains(response, "打印")
        self.assertContains(response, "确认入库")
        self.assertContains(response, self.raw.material_code)
        self.assertContains(response, "返回采购单")
        self.assertContains(response, f'href="/purchase/orders/{order.id}/"')

    def test_purchase_receipt_print_masks_price_and_records_log(self):
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item = self._purchase_receipt()
        receipt_item.unit_price = Decimal("2.345678")
        receipt_item.save(update_fields=["unit_price"])

        response = self.client.get(f"/purchase/receipts/{receipt.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "进货入库单")
        self.assertContains(response, receipt.purchase_receipt_no)
        self.assertContains(response, "******")
        self.assertNotContains(response, "2.345678")
        print_log = PrintLog.objects.get(source_doc_type="purchase_receipt", source_doc_id=receipt.id)
        self.assertEqual(print_log.template_type, "purchase_receipt")
        self.assertEqual(print_log.source_doc_no, receipt.purchase_receipt_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_purchase_receipt_print_shows_price_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item = self._purchase_receipt()
        receipt_item.unit_price = Decimal("2.345678")
        receipt_item.save(update_fields=["unit_price"])

        response = self.client.get(f"/purchase/receipts/{receipt.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2.345678")

    def test_purchase_receipt_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        list_response = self.client.get("/purchase/receipts/")
        response = self.client.get("/purchase/receipts/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("进货单号,采购单,供应商,进货日期,状态", content)
        self.assertIn(receipt.purchase_receipt_no, content)
        export_log = ExportLog.objects.get(module="purchase_receipts")
        self.assertEqual(export_log.row_count, 1)

    def test_purchase_request_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        request_row = PurchaseRequest.objects.create(
            purchase_request_no="PR-EXPORT",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.APPROVED,
            requested_by=self.user,
            needed_date=timezone.localdate(),
        )

        list_response = self.client.get("/purchase/requests/")
        response = self.client.get("/purchase/requests/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("需求单号,来源,状态,需求日期,创建时间", content)
        self.assertIn(request_row.purchase_request_no, content)
        export_log = ExportLog.objects.get(module="purchase_requests")
        self.assertEqual(export_log.row_count, 1)

    def test_purchase_request_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/purchase/requests/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("采购需求单号,需求日期,物料编码,需求数量", content)
        self.assertIn("PR-INIT-001", content)

    def test_purchase_request_import_creates_draft_request_with_multiple_lines(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)
        raw_2 = Material.objects.create(
            material_code="RM002",
            material_name="原料 2",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        upload = SimpleUploadedFile(
            "purchase_requests.csv",
            (
                "采购需求单号,需求日期,物料编码,需求数量,建议供应商编号,明细需求日期,备注\n"
                "PR-IMP-001,2026-06-20,RM001,100,S001,2026-06-20,导入需求\n"
                "PR-IMP-001,2026-06-20,RM002,50,,2026-06-22,\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/requests/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/purchase/requests/")
        purchase_request = PurchaseRequest.objects.get(purchase_request_no="PR-IMP-001")
        self.assertEqual(purchase_request.status, PurchaseRequest.Status.DRAFT)
        self.assertEqual(purchase_request.source_type, PurchaseRequest.SourceType.MANUAL)
        self.assertEqual(purchase_request.requested_by, self.user)
        self.assertEqual(purchase_request.items.count(), 2)
        first_item = purchase_request.items.get(material=self.raw)
        second_item = purchase_request.items.get(material=raw_2)
        self.assertEqual(first_item.request_qty, Decimal("100"))
        self.assertEqual(first_item.suggested_supplier, self.supplier)
        self.assertEqual(second_item.needed_date.isoformat(), "2026-06-22")
        job = ImportJob.objects.get(template_type="purchase_requests")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_purchase_request_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "purchase_requests.csv",
            (
                "采购需求单号,需求日期,物料编码,需求数量,建议供应商编号,明细需求日期,备注\n"
                "PR-BAD,bad-date,RM001,-1,S-MISSING,,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/requests/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "需求日期格式错误")
        self.assertContains(response, "需求数量必须大于 0")
        self.assertContains(response, "建议供应商不存在或未启用")
        job = ImportJob.objects.get(template_type="purchase_requests")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)
        self.assertFalse(PurchaseRequest.objects.filter(purchase_request_no="PR-BAD").exists())

    def test_purchase_request_import_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/purchase/requests/import-template/")
        import_response = self.client.get("/purchase/requests/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_purchase_order_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/purchase/orders/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("采购单号,供应商编号,订单日期,物料编码,订单数量,单价", content)
        self.assertIn("PO-INIT-001", content)

    def test_purchase_order_import_creates_draft_order_with_csv_prices(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        raw_2 = Material.objects.create(
            material_code="RM002",
            material_name="原料 2",
            material_type=Material.MaterialType.RAW,
            base_unit="kg",
        )
        upload = SimpleUploadedFile(
            "purchase_orders.csv",
            (
                "采购单号,供应商编号,订单日期,物料编码,订单数量,单价,需求日期,备注\n"
                "PO-IMP-001,S001,2026-06-20,RM001,10,2.500000,2026-06-25,导入采购\n"
                "PO-IMP-001,S001,2026-06-20,RM002,4,3.200000,2026-06-28,\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/purchase/orders/")
        order = PurchaseOrder.objects.get(purchase_order_no="PO-IMP-001")
        self.assertEqual(order.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(order.supplier, self.supplier)
        self.assertEqual(order.created_by, self.user)
        self.assertEqual(order.total_amount, Decimal("37.80"))
        first_item = order.items.get(material=self.raw)
        second_item = order.items.get(material=raw_2)
        self.assertEqual(first_item.unit_price, Decimal("2.500000"))
        self.assertEqual(first_item.line_amount, Decimal("25.00"))
        self.assertEqual(second_item.needed_date.isoformat(), "2026-06-28")
        job = ImportJob.objects.get(template_type="purchase_orders")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_purchase_order_import_without_amount_permission_uses_default_price(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)
        self.raw.latest_purchase_price = Decimal("1.750000")
        self.raw.save(update_fields=["latest_purchase_price"])
        MaterialSupplierPrice.objects.create(
            material=self.raw,
            supplier=self.supplier,
            purchase_price=Decimal("2.250000"),
            is_default=True,
        )
        upload = SimpleUploadedFile(
            "purchase_orders.csv",
            (
                "采购单号,供应商编号,订单日期,物料编码,订单数量,单价,需求日期,备注\n"
                "PO-IMP-NO-AMOUNT,S001,2026-06-20,RM001,10,99.990000,,无金额权限\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        order = PurchaseOrder.objects.get(purchase_order_no="PO-IMP-NO-AMOUNT")
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("2.250000"))
        self.assertEqual(item.line_amount, Decimal("22.50"))
        self.assertEqual(order.total_amount, Decimal("22.50"))

    def test_purchase_order_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "purchase_orders.csv",
            (
                "采购单号,供应商编号,订单日期,物料编码,订单数量,单价,需求日期,备注\n"
                "PO-BAD,S-MISSING,bad-date,RM001,-1,-2,bad-needed,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "供应商不存在或未启用")
        self.assertContains(response, "采购日期格式错误")
        self.assertContains(response, "采购数量必须大于 0")
        self.assertContains(response, "采购单价不能小于 0")
        self.assertContains(response, "需求日期格式错误")
        job = ImportJob.objects.get(template_type="purchase_orders")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)
        self.assertFalse(PurchaseOrder.objects.filter(purchase_order_no="PO-BAD").exists())

    def test_purchase_order_import_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/purchase/orders/import-template/")
        import_response = self.client.get("/purchase/orders/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_purchase_receipt_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/purchase/receipts/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("进货单号,采购单号,单据日期", content)
        self.assertIn("GR-INIT-001", content)

    def test_purchase_receipt_import_creates_pending_receipt_without_inventory_change(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-GR-IMPORT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        order_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("10.0000"),
            received_qty=Decimal("2.0000"),
            unit_price=Decimal("2.500000"),
            line_amount=Decimal("25.00"),
        )
        upload = SimpleUploadedFile(
            "purchase_receipts.csv",
            (
                "进货单号,采购单号,单据日期,采购订单行号,物料编码,到货数量,合格数量,不合格数量,库位编码,备注\n"
                "GR-IMP-001,PO-GR-IMPORT,2026-06-10,1,RM001,5,4,1,A01,导入进货\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/receipts/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/purchase/receipts/")
        receipt = PurchaseReceipt.objects.get(purchase_receipt_no="GR-IMP-001")
        receipt_item = receipt.items.get()
        self.assertEqual(receipt.status, PurchaseReceipt.Status.PENDING_RECEIVE)
        self.assertEqual(receipt.purchase_order, order)
        self.assertEqual(receipt.supplier, self.supplier)
        self.assertEqual(receipt.created_by, self.user)
        self.assertEqual(receipt_item.purchase_order_item, order_item)
        self.assertEqual(receipt_item.received_qty, Decimal("5"))
        self.assertEqual(receipt_item.accepted_qty, Decimal("4"))
        self.assertEqual(receipt_item.rejected_qty, Decimal("1"))
        self.assertEqual(receipt_item.unit_price, Decimal("2.500000"))
        self.assertEqual(receipt_item.location, self.location)
        self.assertIsNone(receipt_item.batch)
        order_item.refresh_from_db()
        self.assertEqual(order_item.received_qty, Decimal("2.0000"))
        self.assertFalse(InventoryBatch.objects.exists())
        job = ImportJob.objects.get(template_type="purchase_receipts")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_purchase_receipt_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-GR-BAD",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("3.0000"),
            received_qty=Decimal("0.0000"),
            unit_price=Decimal("2.500000"),
            line_amount=Decimal("7.50"),
        )
        upload = SimpleUploadedFile(
            "purchase_receipts.csv",
            (
                "进货单号,采购单号,单据日期,采购订单行号,物料编码,到货数量,合格数量,不合格数量,库位编码,备注\n"
                "GR-BAD,PO-GR-BAD,bad-date,1,RM001,-1,5,1,L-MISSING,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/receipts/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "进货日期格式错误")
        self.assertContains(response, "到货数量必须大于 0")
        self.assertContains(response, "合格数量与不合格数量之和不能超过到货数量")
        self.assertContains(response, "合格数量不能超过采购行剩余未到货数量")
        self.assertContains(response, "库位不存在或未启用")
        job = ImportJob.objects.get(template_type="purchase_receipts")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertFalse(PurchaseReceipt.objects.filter(purchase_receipt_no="GR-BAD").exists())

    def test_purchase_receipt_import_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/purchase/receipts/import-template/")
        import_response = self.client.get("/purchase/receipts/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_supplier_return_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get("/purchase/supplier-returns/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("供应商退货单号,供应商编号,进货单号,退货日期", content)
        self.assertIn("SR-INIT-001", content)

    def test_supplier_return_import_creates_draft_return_with_source_receipt_item(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item, batch = self._received_purchase_receipt_for_return()
        upload = SimpleUploadedFile(
            "supplier_returns.csv",
            (
                "供应商退货单号,供应商编号,进货单号,退货日期,采购订单行号,物料编码,退货数量,单价,批次号,库位编码,退货原因,备注\n"
                "SR-IMP-001,S001,GR001,2026-06-10,1,RM001,3,2.500000,B-RETURN-IMPORT,A01,质量问题,导入供应商退货\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/supplier-returns/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/purchase/supplier-returns/")
        supplier_return = SupplierReturn.objects.get(supplier_return_no="SR-IMP-001")
        return_item = supplier_return.items.get()
        batch.refresh_from_db()
        self.assertEqual(supplier_return.status, SupplierReturn.Status.DRAFT)
        self.assertEqual(supplier_return.supplier, self.supplier)
        self.assertEqual(supplier_return.purchase_receipt, receipt)
        self.assertEqual(supplier_return.created_by, self.user)
        self.assertEqual(supplier_return.return_amount, Decimal("7.50"))
        self.assertEqual(return_item.purchase_receipt_item, receipt_item)
        self.assertEqual(return_item.material, self.raw)
        self.assertEqual(return_item.unit_price, Decimal("2.500000"))
        self.assertEqual(return_item.return_amount, Decimal("7.50"))
        self.assertEqual(return_item.batch, batch)
        self.assertEqual(return_item.location, self.location)
        self.assertEqual(batch.remaining_qty, Decimal("20.0000"))
        job = ImportJob.objects.get(template_type="supplier_returns")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_supplier_return_import_without_amount_permission_uses_receipt_item_price(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item, batch = self._received_purchase_receipt_for_return(batch_no="B-RETURN-NO-AMOUNT")
        upload = SimpleUploadedFile(
            "supplier_returns.csv",
            (
                "供应商退货单号,供应商编号,进货单号,退货日期,采购订单行号,物料编码,退货数量,单价,批次号,库位编码,退货原因,备注\n"
                "SR-IMP-NO-AMOUNT,S001,GR001,2026-06-10,1,RM001,3,99.990000,B-RETURN-NO-AMOUNT,A01,无金额权限,\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/supplier-returns/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        supplier_return = SupplierReturn.objects.get(supplier_return_no="SR-IMP-NO-AMOUNT")
        return_item = supplier_return.items.get()
        self.assertEqual(return_item.unit_price, Decimal("1.000000"))
        self.assertEqual(return_item.return_amount, Decimal("3.00"))
        self.assertEqual(supplier_return.return_amount, Decimal("3.00"))

    def test_supplier_return_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item, batch = self._received_purchase_receipt_for_return(
            batch_no="B-RETURN-BAD",
            remaining_qty=Decimal("3.0000"),
        )
        upload = SimpleUploadedFile(
            "supplier_returns.csv",
            (
                "供应商退货单号,供应商编号,进货单号,退货日期,采购订单行号,物料编码,退货数量,单价,批次号,库位编码,退货原因,备注\n"
                "SR-BAD,S001,GR001,bad-date,1,RM001,99,-2,B-RETURN-BAD,A01,错误,\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/purchase/supplier-returns/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "退货日期格式错误")
        self.assertContains(response, "退货数量不能超过来源进货行可退数量")
        self.assertContains(response, "退货单价不能小于 0")
        self.assertContains(response, "退货数量不能超过批次剩余数量")
        job = ImportJob.objects.get(template_type="supplier_returns")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)
        self.assertFalse(SupplierReturn.objects.filter(supplier_return_no="SR-BAD").exists())

    def test_supplier_return_import_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/purchase/supplier-returns/import-template/")
        import_response = self.client.get("/purchase/supplier-returns/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_supplier_return_export_masks_amount_and_logs(self):
        self.client.force_login(self.user)
        supplier_return, batch = self._supplier_return()
        supplier_return.supplier_return_no = "SRT-EXPORT"
        supplier_return.return_amount = Decimal("4.00")
        supplier_return.save(update_fields=["supplier_return_no", "return_amount"])

        list_response = self.client.get("/purchase/supplier-returns/")
        response = self.client.get("/purchase/supplier-returns/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("退货单号,供应商,退货日期,状态,金额", content)
        self.assertIn("SRT-EXPORT", content)
        self.assertIn("******", content)
        self.assertNotIn("4.00", content)
        export_log = ExportLog.objects.get(module="supplier_returns")
        self.assertEqual(export_log.row_count, 1)

    def test_purchase_receipt_confirm_view_updates_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        response = self.client.post(f"/purchase/receipts/{receipt.id}/confirm/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/receipts/{receipt.id}/")
        receipt.refresh_from_db()
        order.refresh_from_db()
        order_item.refresh_from_db()
        receipt_item.refresh_from_db()
        inventory = Inventory.objects.get(material=self.raw, location=self.location)
        transaction_row = InventoryTransaction.objects.get()
        self.assertEqual(receipt.status, PurchaseReceipt.Status.RECEIVED)
        self.assertEqual(order.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(order_item.line_status, PurchaseOrderItem.LineStatus.RECEIVED)
        self.assertIsNotNone(receipt_item.batch)
        self.assertEqual(inventory.qty, Decimal("20.0000"))
        self.assertEqual(transaction_row.transaction_type, InventoryTransaction.TransactionType.PURCHASE_IN)

    def test_purchase_receipt_confirm_view_enqueues_async_shortage_recheck(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        sales_order, sales_item = self._sales_order_with_shortage()
        alert = ShortageAlert.objects.get()
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        response = self.client.post(f"/purchase/receipts/{receipt.id}/confirm/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        sales_item.refresh_from_db()
        alert.refresh_from_db()
        purchase_event = PendingEvent.objects.get(event_type="purchase_received")
        self.assertEqual(purchase_event.payload["sales_order_item_ids"], [sales_item.id])
        self.assertEqual(sales_item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SHORTAGE)
        self.assertEqual(alert.status, ShortageAlert.Status.UNPROCESSED)

        event_result = process_pending_events(event_type="purchase_received")

        self.assertTrue(event_result.success)
        sales_item.refresh_from_db()
        alert.refresh_from_db()
        self.assertEqual(sales_item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.KITTED)
        self.assertEqual(alert.status, ShortageAlert.Status.KITTED)
        self.assertTrue(PendingEvent.objects.filter(event_type="shortage_kitted").exists())

    def test_purchase_receipt_confirm_requires_purchase_process_permission(self):
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        response = self.client.post(f"/purchase/receipts/{receipt.id}/confirm/")

        self.assertEqual(response.status_code, 403)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PurchaseReceipt.Status.PENDING_RECEIVE)
        self.assertFalse(Inventory.objects.exists())

    def test_purchase_receipt_confirm_requires_second_verify_password(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order, order_item, receipt, receipt_item = self._purchase_receipt()

        response = self.client.post(
            f"/purchase/receipts/{receipt.id}/confirm/",
            {"current_password": "wrong-password"},
            follow=True,
        )

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PurchaseReceipt.Status.PENDING_RECEIVE)
        self.assertContains(response, "二次验证失败")
        self.assertFalse(Inventory.objects.exists())

    def test_purchase_process_actions_require_purchase_process_permission(self):
        self.client.force_login(self.user)
        purchase_request = PurchaseRequest.objects.create(
            purchase_request_no="PR-NOPERM",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.DRAFT,
            requested_by=self.user,
        )
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            line_no=1,
            material=self.raw,
            request_qty=Decimal("3.0000"),
        )
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-NOPERM",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("5.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("10.00"),
        )
        _, _, receipt, receipt_item = self._purchase_receipt()
        batch = InventoryBatch.objects.create(
            batch_no="B-NOPERM",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("20.0000"),
            remaining_qty=Decimal("20.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("20.0000"),
        )
        supplier_return = SupplierReturn.objects.create(
            supplier_return_no="SR-NOPERM",
            supplier=self.supplier,
            purchase_receipt=receipt,
            return_date=timezone.localdate(),
            status=SupplierReturn.Status.DRAFT,
            created_by=self.user,
        )
        SupplierReturnItem.objects.create(
            supplier_return=supplier_return,
            purchase_receipt_item=receipt_item,
            material=self.raw,
            return_qty=Decimal("2.0000"),
            unit_price=Decimal("1.000000"),
            return_amount=Decimal("2.00"),
            batch=batch,
            location=self.location,
            return_reason="测试权限",
        )

        request_detail = self.client.get(f"/purchase/requests/{purchase_request.id}/")
        order_detail = self.client.get(f"/purchase/orders/{order.id}/")
        receipt_detail = self.client.get(f"/purchase/receipts/{receipt.id}/")
        return_detail = self.client.get(f"/purchase/supplier-returns/{supplier_return.id}/")

        self.assertNotContains(request_detail, f"/purchase/requests/{purchase_request.id}/edit/")
        self.assertNotContains(request_detail, "生成采购单")
        self.assertNotContains(order_detail, f"/purchase/orders/{order.id}/edit/")
        self.assertNotContains(order_detail, "新增明细")
        self.assertNotContains(receipt_detail, f"/purchase/receipts/{receipt.id}/edit/")
        self.assertNotContains(receipt_detail, "确认入库")
        self.assertNotContains(return_detail, f"/purchase/supplier-returns/{supplier_return.id}/edit/")
        self.assertNotContains(return_detail, "确认退货出库")

        blocked_responses = [
            self.client.get(f"/purchase/requests/{purchase_request.id}/edit/"),
            self.client.post(f"/purchase/requests/{purchase_request.id}/void/"),
            self.client.get(f"/purchase/orders/{order.id}/edit/"),
            self.client.post(f"/purchase/orders/{order.id}/void/"),
            self.client.post(f"/purchase/orders/{order.id}/items/new/"),
            self.client.get(f"/purchase/receipts/{receipt.id}/edit/"),
            self.client.post(f"/purchase/receipts/{receipt.id}/confirm/"),
            self.client.get(f"/purchase/supplier-returns/{supplier_return.id}/edit/"),
            self.client.post(f"/purchase/supplier-returns/{supplier_return.id}/void/"),
            self.client.post(f"/purchase/supplier-returns/{supplier_return.id}/confirm-out/"),
        ]
        self.assertTrue(all(response.status_code == 403 for response in blocked_responses))

    def test_purchase_receipt_edit_updates_pending_receipt_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        other_location = WarehouseLocation.objects.create(location_code="B01", location_name="B01")
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))

        response = self.client.post(
            f"/purchase/receipts/{receipt.id}/edit/",
            {
                "receipt_date": timezone.localdate().isoformat(),
                "remark": "调整合格数",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": receipt_item.id,
                "items-0-received_qty": "12",
                "items-0-accepted_qty": "10",
                "items-0-rejected_qty": "2",
                "items-0-location": other_location.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/receipts/{receipt.id}/")
        receipt.refresh_from_db()
        receipt_item.refresh_from_db()
        self.assertEqual(receipt.remark, "调整合格数")
        self.assertEqual(receipt_item.received_qty, Decimal("12.0000"))
        self.assertEqual(receipt_item.accepted_qty, Decimal("10.0000"))
        self.assertEqual(receipt_item.rejected_qty, Decimal("2.0000"))
        self.assertEqual(receipt_item.location, other_location)
        audit_log = AuditLog.objects.get(action="purchase_receipt_update", source_doc_id=receipt.id)
        self.assertEqual(audit_log.before_snapshot["items"][0]["accepted_qty"], "20.0000")
        self.assertEqual(audit_log.after_snapshot["items"][0]["accepted_qty"], "10.0000")

    def test_purchase_receipt_edit_rejects_received_receipt(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order, order_item, receipt, receipt_item = self._purchase_receipt()
        receipt.status = PurchaseReceipt.Status.RECEIVED
        receipt.save(update_fields=["status"])

        response = self.client.get(f"/purchase/receipts/{receipt.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/receipts/{receipt.id}/")

    def test_purchase_receipt_edit_rejects_qty_over_remaining(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))
        order_item.received_qty = Decimal("15.0000")
        order_item.save(update_fields=["received_qty"])

        response = self.client.post(
            f"/purchase/receipts/{receipt.id}/edit/",
            {
                "receipt_date": timezone.localdate().isoformat(),
                "remark": "超量",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": receipt_item.id,
                "items-0-received_qty": "8",
                "items-0-accepted_qty": "6",
                "items-0-rejected_qty": "0",
                "items-0-location": self.location.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "合格数量不能超过采购行剩余未到货数量")
        receipt_item.refresh_from_db()
        self.assertEqual(receipt_item.accepted_qty, Decimal("20.0000"))

    def test_create_purchase_order_from_request_creates_items_and_closes_request(self):
        request = PurchaseRequest.objects.create(
            purchase_request_no="PR001",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.APPROVED,
            requested_by=self.user,
            needed_date=timezone.localdate(),
        )
        request_item = PurchaseRequestItem.objects.create(
            purchase_request=request,
            line_no=1,
            material=self.raw,
            request_qty=Decimal("12.0000"),
            suggested_supplier=self.supplier,
        )

        result = create_purchase_order_from_request(request.id, self.supplier.id, self.user.id, "pr-po-1")

        self.assertTrue(result.success)
        order = PurchaseOrder.objects.get(id=result.data["purchase_order_id"])
        order_item = PurchaseOrderItem.objects.get(purchase_order=order)
        request.refresh_from_db()
        request_item.refresh_from_db()
        self.assertEqual(order.supplier, self.supplier)
        self.assertEqual(order.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(order_item.purchase_request_item, request_item)
        self.assertEqual(order_item.order_qty, Decimal("12.0000"))
        self.assertEqual(request_item.line_status, PurchaseRequestItem.LineStatus.ORDERED)
        self.assertEqual(request.status, PurchaseRequest.Status.CLOSED)

    def test_purchase_order_create_and_detail_views(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.raw.spec = "10K 1%"
        self.raw.save(update_fields=["spec"])

        page_response = self.client.get("/purchase/orders/new/")
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context["form"].fields["order_date"].widget.attrs["data-erp-date"], "1")
        self.assertEqual(
            page_response.context["item_formset"].forms[0].fields["needed_date"].widget.attrs["data-erp-date"],
            "1",
        )
        self.assertContains(page_response, 'name="items-0-needed_date"')
        self.assertContains(page_response, 'type="date"')
        self.assertContains(page_response, "RM001｜原料 1｜规格型号：10K 1%｜单位：pcs")

        response = self.client.post(
            "/purchase/orders/new/",
            {
                "supplier": self.supplier.id,
                "order_date": timezone.localdate().isoformat(),
                "remark": "页面创建",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-material": self.raw.id,
                "items-0-order_qty": "8",
                "items-0-unit_price": "1.25",
                "items-0-needed_date": "2026/7/5",
                "items-1-material": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-1-needed_date": "",
                "items-2-material": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "items-2-needed_date": "",
                "action": "draft",
            },
        )

        order = PurchaseOrder.objects.order_by("-id").first()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/orders/{order.id}/")
        self.assertEqual(order.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(order.total_amount, Decimal("10.00"))
        item = order.items.get()
        self.assertEqual(item.line_amount, Decimal("10.00"))
        self.assertEqual(item.needed_date, date(2026, 7, 5))
        detail_response = self.client.get(f"/purchase/orders/{order.id}/")
        self.assertContains(detail_response, order.purchase_order_no)
        self.assertContains(detail_response, self.raw.material_code)
        self.assertContains(detail_response, "原料 1")
        self.assertContains(detail_response, "10K 1%")
        self.assertContains(detail_response, "pcs")

    def test_purchase_order_submit_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        get_response = self.client.get("/purchase/orders/new/")
        post_response = self.client.post(
            "/purchase/orders/new/",
            {
                "supplier": self.supplier.id,
                "order_date": timezone.localdate().isoformat(),
                "remark": "无权限提交采购单",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-material": self.raw.id,
                "items-0-order_qty": "8",
                "items-0-unit_price": "1.25",
                "items-0-needed_date": "",
                "items-1-material": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-1-needed_date": "",
                "items-2-material": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "items-2-needed_date": "",
                "action": "submit",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertNotContains(get_response, "保存并提交审核")
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_purchase_order_edit_updates_draft_order_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-EDIT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            total_amount=Decimal("8.00"),
            created_by=self.user,
            remark="编辑前",
        )
        item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("4.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("8.00"),
        )

        response = self.client.post(
            f"/purchase/orders/{order.id}/edit/",
            {
                "supplier": self.supplier.id,
                "order_date": timezone.localdate().isoformat(),
                "remark": "编辑后备注",
                "items-TOTAL_FORMS": "4",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-material": self.raw.id,
                "items-0-order_qty": "6",
                "items-0-unit_price": "3.25",
                "items-0-needed_date": "",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-material": "",
                "items-1-order_qty": "",
                "items-1-unit_price": "",
                "items-1-needed_date": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-material": "",
                "items-2-order_qty": "",
                "items-2-unit_price": "",
                "items-2-needed_date": "",
                "items-2-DELETE": "",
                "items-3-id": "",
                "items-3-material": "",
                "items-3-order_qty": "",
                "items-3-unit_price": "",
                "items-3-needed_date": "",
                "items-3-DELETE": "",
                "action": "draft",
                "operation_reason": "供应商报价调整",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/orders/{order.id}/")
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(order.remark, "编辑后备注")
        self.assertEqual(order.total_amount, Decimal("19.50"))
        self.assertEqual(item.line_no, 1)
        self.assertEqual(item.line_amount, Decimal("19.50"))
        audit_log = AuditLog.objects.get(action="purchase_order_update", source_doc_id=order.id)
        self.assertEqual(audit_log.operator, self.user)
        self.assertEqual(audit_log.before_snapshot["total_amount"], "8.00")
        self.assertEqual(audit_log.after_snapshot["total_amount"], "19.50")
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "供应商报价调整")

    def test_purchase_order_edit_rejects_approved_order(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-NOEDIT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            created_by=self.user,
        )

        response = self.client.get(f"/purchase/orders/{order.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/orders/{order.id}/")

    def test_purchase_order_voids_pending_order_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-VOID",
            supplier=self.supplier,
            status=PurchaseOrder.Status.PENDING_APPROVAL,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("5.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("10.00"),
        )

        response = self.client.post(f"/purchase/orders/{order.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/orders/{order.id}/")
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.Status.VOIDED)
        audit_log = AuditLog.objects.get(action="purchase_order_void", source_doc_id=order.id)
        self.assertEqual(audit_log.operator, self.user)
        self.assertEqual(audit_log.before_snapshot["status"], PurchaseOrder.Status.PENDING_APPROVAL)
        self.assertEqual(audit_log.after_snapshot["status"], PurchaseOrder.Status.VOIDED)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "测试作废")

    def test_purchase_order_void_requires_reason(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-VOID-NO-REASON",
            supplier=self.supplier,
            status=PurchaseOrder.Status.PENDING_APPROVAL,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("5.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("10.00"),
        )

        response = self.client.post(
            f"/purchase/orders/{order.id}/void/",
            {"current_password": "x", "void_reason": ""},
            follow=True,
        )

        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.Status.PENDING_APPROVAL)
        self.assertContains(response, "请填写采购单作废原因")
        self.assertFalse(AuditLog.objects.filter(action="purchase_order_void", source_doc_id=order.id).exists())

    def test_purchase_order_amounts_mask_without_finance_permission(self):
        self.client.force_login(self.user)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-MASK",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            total_amount=Decimal("88.00"),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("8.0000"),
            unit_price=Decimal("11.000000"),
            line_amount=Decimal("88.00"),
        )

        list_response = self.client.get("/purchase/orders/")
        detail_response = self.client.get(f"/purchase/orders/{order.id}/")

        self.assertContains(list_response, "******")
        self.assertNotContains(list_response, "88.00")
        self.assertContains(detail_response, "******")
        self.assertNotContains(detail_response, "11.000000")

    def test_purchase_order_amounts_visible_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-VIEW",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            total_amount=Decimal("88.00"),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("8.0000"),
            unit_price=Decimal("11.000000"),
            line_amount=Decimal("88.00"),
        )

        response = self.client.get(f"/purchase/orders/{order.id}/")

        self.assertContains(response, "88.00")
        self.assertContains(response, "11.000000")

    def test_purchase_order_detail_adds_item_without_amount_permission_uses_default_price(self):
        self.raw.latest_purchase_price = Decimal("3.500000")
        self.raw.save(update_fields=["latest_purchase_price"])
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO001",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            created_by=self.user,
        )

        response = self.client.post(
            f"/purchase/orders/{order.id}/items/new/",
            {
                "material": self.raw.id,
                "order_qty": "4",
                "unit_price": "99.99",
                "needed_date": "2026/7/5",
            },
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("3.500000"))
        self.assertEqual(item.line_amount, Decimal("14.00"))
        self.assertEqual(order.total_amount, Decimal("14.00"))

    def test_purchase_order_detail_adds_item(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO001",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            created_by=self.user,
        )

        response = self.client.post(
            f"/purchase/orders/{order.id}/items/new/",
            {
                "material": self.raw.id,
                "order_qty": "5",
                "unit_price": "2",
                "needed_date": "2026/7/5",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/orders/{order.id}/")
        order.refresh_from_db()
        item = order.items.get()
        self.assertEqual(item.line_no, 1)
        self.assertEqual(item.needed_date, date(2026, 7, 5))
        self.assertEqual(item.line_amount, Decimal("10.00"))
        self.assertEqual(order.total_amount, Decimal("10.00"))

    def test_purchase_order_detail_creates_receipt_for_remaining_items(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-RC",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        order_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("8.0000"),
            received_qty=Decimal("3.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("16.00"),
        )

        page_response = self.client.get(f"/purchase/orders/{order.id}/")
        self.assertContains(page_response, "生成进货单")

        response = self.client.post(
            f"/purchase/orders/{order.id}/create-receipt/",
            {
                "receipt_date": "2026.07.04",
                "location": self.location.id,
                "remark": "本次到货",
            },
        )

        receipt = PurchaseReceipt.objects.get()
        receipt_item = receipt.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/receipts/{receipt.id}/")
        self.assertEqual(receipt.purchase_order, order)
        self.assertEqual(receipt.receipt_date, date(2026, 7, 4))
        self.assertEqual(receipt.status, PurchaseReceipt.Status.PENDING_RECEIVE)
        self.assertEqual(receipt.created_by, self.user)
        self.assertEqual(receipt_item.purchase_order_item, order_item)
        self.assertEqual(receipt_item.received_qty, Decimal("5.0000"))
        self.assertEqual(receipt_item.accepted_qty, Decimal("5.0000"))
        self.assertEqual(receipt_item.location, self.location)

    def test_purchase_order_print_masks_amount_and_records_log(self):
        self.client.force_login(self.user)
        self.raw.spec = "10K 1%"
        self.raw.save(update_fields=["spec"])
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-PRINT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("55.55"),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("5.0000"),
            received_qty=Decimal("0.0000"),
            unit_price=Decimal("11.110000"),
            line_amount=Decimal("55.55"),
        )

        detail_response = self.client.get(f"/purchase/orders/{order.id}/")
        response = self.client.get(f"/purchase/orders/{order.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "采购单")
        self.assertContains(response, order.purchase_order_no)
        self.assertContains(response, "10K 1%")
        self.assertContains(response, "pcs")
        self.assertContains(response, "******")
        self.assertNotContains(response, "11.110000")
        print_log = PrintLog.objects.get(source_doc_type="purchase_order", source_doc_id=order.id)
        self.assertEqual(print_log.template_type, "purchase_order")
        self.assertEqual(print_log.source_doc_no, order.purchase_order_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_purchase_order_export_masks_amount_and_logs(self):
        self.client.force_login(self.user)
        PurchaseOrder.objects.create(
            purchase_order_no="PO-EXPORT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("55.55"),
        )

        list_response = self.client.get("/purchase/orders/")
        response = self.client.get("/purchase/orders/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("采购单号,供应商,订单日期,负责人,状态,金额", content)
        self.assertIn("PO-EXPORT", content)
        self.assertIn("******", content)
        self.assertNotIn("55.55", content)
        export_log = ExportLog.objects.get(module="purchase_orders")
        self.assertEqual(export_log.row_count, 1)

    def test_purchase_order_list_scope_defaults_to_mine_without_global_permission(self):
        self.user.roles.clear()
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        other_user = get_user_model().objects.create_user(username="other-purchase-owner", password="x")
        PurchaseOrder.objects.create(
            purchase_order_no="PO-MINE-SCOPE",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            purchase_owner=self.user,
            total_amount=Decimal("10.00"),
        )
        PurchaseOrder.objects.create(
            purchase_order_no="PO-OTHER-SCOPE",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            purchase_owner=other_user,
            total_amount=Decimal("20.00"),
        )
        PurchaseOrder.objects.create(
            purchase_order_no="PO-UNASSIGNED-SCOPE",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("30.00"),
        )
        self.client.force_login(self.user)

        response = self.client.get("/purchase/orders/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PO-MINE-SCOPE")
        self.assertNotContains(response, "PO-OTHER-SCOPE")
        self.assertNotContains(response, "PO-UNASSIGNED-SCOPE")
        self.assertContains(response, "scope=mine")
        self.assertNotContains(response, "scope=all")
        self.assertNotContains(response, "scope=unassigned")

    def test_purchase_order_list_global_permission_can_filter_unassigned(self):
        other_user = get_user_model().objects.create_user(username="scope-other-global", password="x")
        PurchaseOrder.objects.create(
            purchase_order_no="PO-GLOBAL-MINE",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            purchase_owner=self.user,
            total_amount=Decimal("10.00"),
        )
        PurchaseOrder.objects.create(
            purchase_order_no="PO-GLOBAL-OTHER",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            purchase_owner=other_user,
            total_amount=Decimal("20.00"),
        )
        PurchaseOrder.objects.create(
            purchase_order_no="PO-GLOBAL-UNASSIGNED",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("30.00"),
        )
        self.client.force_login(self.user)

        all_response = self.client.get("/purchase/orders/")
        unassigned_response = self.client.get("/purchase/orders/?scope=unassigned")

        self.assertContains(all_response, "scope=all")
        self.assertContains(all_response, "scope=mine")
        self.assertContains(all_response, "scope=unassigned")
        self.assertContains(all_response, "PO-GLOBAL-MINE")
        self.assertContains(all_response, "PO-GLOBAL-OTHER")
        self.assertContains(all_response, "PO-GLOBAL-UNASSIGNED")
        self.assertContains(unassigned_response, "PO-GLOBAL-UNASSIGNED")
        self.assertNotContains(unassigned_response, "PO-GLOBAL-MINE")
        self.assertNotContains(unassigned_response, "PO-GLOBAL-OTHER")

    def test_purchase_order_export_shows_amount_with_finance_permission(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        PurchaseOrder.objects.create(
            purchase_order_no="PO-EXPORT-AMOUNT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("55.55"),
        )

        response = self.client.get("/purchase/orders/export/")
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("55.55", content)

    def test_purchase_order_list_filter_and_export_share_query(self):
        self.client.force_login(self.user)
        PurchaseOrder.objects.create(
            purchase_order_no="PO-FILTER-KEEP",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            total_amount=Decimal("55.55"),
        )
        PurchaseOrder.objects.create(
            purchase_order_no="PO-FILTER-HIDE",
            supplier=self.supplier,
            status=PurchaseOrder.Status.DRAFT,
            order_date=timezone.localdate(),
            total_amount=Decimal("66.66"),
        )

        list_response = self.client.get("/purchase/orders/?q=KEEP&status=approved")
        export_response = self.client.get("/purchase/orders/export/?q=KEEP&status=approved")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "PO-FILTER-KEEP")
        self.assertNotContains(list_response, "PO-FILTER-HIDE")
        self.assertContains(list_response, "/purchase/orders/export/?q=KEEP&amp;status=approved")
        self.assertIn("PO-FILTER-KEEP", content)
        self.assertNotIn("PO-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="purchase_orders")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "approved")

    def test_purchase_order_create_receipt_requires_purchase_process_permission(self):
        self.client.force_login(self.user)
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-RC-DENIED",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("8.0000"),
            received_qty=Decimal("0.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("16.00"),
        )

        page_response = self.client.get(f"/purchase/orders/{order.id}/")
        response = self.client.post(
            f"/purchase/orders/{order.id}/create-receipt/",
            {"receipt_date": timezone.localdate().isoformat(), "location": self.location.id},
        )

        self.assertNotContains(page_response, "生成进货单")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PurchaseReceipt.objects.exists())

    def test_purchase_receipt_confirm_denies_other_purchase_owner(self):
        self.user.roles.clear()
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        other_user = get_user_model().objects.create_user(username="other-receipt-owner", password="x")
        order = PurchaseOrder.objects.create(
            purchase_order_no="PO-OTHER-RECEIPT",
            supplier=self.supplier,
            status=PurchaseOrder.Status.APPROVED,
            order_date=timezone.localdate(),
            purchase_owner=other_user,
        )
        order_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=1,
            material=self.raw,
            order_qty=Decimal("5.0000"),
            received_qty=Decimal("0.0000"),
            unit_price=Decimal("2.000000"),
            line_amount=Decimal("10.00"),
        )
        receipt = PurchaseReceipt.objects.create(
            purchase_receipt_no="GR-OTHER-RECEIPT",
            purchase_order=order,
            supplier=self.supplier,
            receipt_date=timezone.localdate(),
            status=PurchaseReceipt.Status.PENDING_RECEIVE,
        )
        PurchaseReceiptItem.objects.create(
            purchase_receipt=receipt,
            purchase_order_item=order_item,
            material=self.raw,
            received_qty=Decimal("5.0000"),
            accepted_qty=Decimal("5.0000"),
            unit_price=Decimal("2.000000"),
            location=self.location,
        )
        self.client.force_login(self.user)

        response = self.client.post(f"/purchase/receipts/{receipt.id}/confirm/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/purchase/receipts/")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, PurchaseReceipt.Status.PENDING_RECEIVE)

    def test_purchase_request_detail_generates_purchase_order(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        request = PurchaseRequest.objects.create(
            purchase_request_no="PR001",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.APPROVED,
            requested_by=self.user,
        )
        PurchaseRequestItem.objects.create(
            purchase_request=request,
            line_no=1,
            material=self.raw,
            request_qty=Decimal("6.0000"),
            suggested_supplier=self.supplier,
        )

        page_response = self.client.get(f"/purchase/requests/{request.id}/")
        self.assertContains(page_response, "生成采购单")

        response = self.client.post(
            f"/purchase/requests/{request.id}/create-order/",
            {"supplier": self.supplier.id},
        )

        order = PurchaseOrder.objects.get()
        request.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/orders/{order.id}/")
        self.assertEqual(order.items.get().order_qty, Decimal("6.0000"))
        self.assertEqual(request.status, PurchaseRequest.Status.CLOSED)
        detail_response = self.client.get(f"/purchase/orders/{order.id}/")
        self.assertContains(detail_response, f'href="/purchase/requests/{request.id}/"')

    def test_purchase_request_detail_shows_attachment_panel(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        request = PurchaseRequest.objects.create(
            purchase_request_no="PR-ATTACH",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.DRAFT,
            requested_by=self.user,
        )
        Attachment.objects.create(
            attachment_no="ATT-PR-001",
            source_doc_type="purchase_request",
            source_doc_id=request.id,
            source_doc_no=request.purchase_request_no,
            original_filename="purchase-request.pdf",
            stored_filename="purchase-request.pdf",
            file_path="attachments/purchase-request.pdf",
            file_size=100,
            uploaded_by=self.user,
        )

        response = self.client.get(f"/purchase/requests/{request.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "purchase-request.pdf")
        self.assertContains(response, 'name="source_doc_type" value="purchase_request"')

    def test_purchase_request_create_view_creates_manual_request(self):
        self.client.force_login(self.user)

        page_response = self.client.get("/purchase/requests/new/")
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context["form"].fields["needed_date"].widget.attrs["data-erp-date"], "1")
        self.assertEqual(
            page_response.context["item_formset"].forms[0].fields["needed_date"].widget.attrs["data-erp-date"],
            "1",
        )
        self.assertContains(page_response, 'name="needed_date"')
        self.assertContains(page_response, 'name="items-0-needed_date"')
        self.assertContains(page_response, 'type="date"')

        response = self.client.post(
            "/purchase/requests/new/",
            {
                "needed_date": "2026/7/6",
                "remark": "人工需求",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-material": self.raw.id,
                "items-0-request_qty": "7",
                "items-0-suggested_supplier": self.supplier.id,
                "items-0-needed_date": "2026.07.08",
                "items-1-material": "",
                "items-1-request_qty": "",
                "items-1-suggested_supplier": "",
                "items-1-needed_date": "",
                "items-2-material": "",
                "items-2-request_qty": "",
                "items-2-suggested_supplier": "",
                "items-2-needed_date": "",
                "action": "draft",
            },
        )

        purchase_request = PurchaseRequest.objects.order_by("-id").first()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/requests/{purchase_request.id}/")
        self.assertEqual(purchase_request.source_type, PurchaseRequest.SourceType.MANUAL)
        self.assertEqual(purchase_request.status, PurchaseRequest.Status.DRAFT)
        self.assertEqual(purchase_request.requested_by, self.user)
        self.assertEqual(purchase_request.needed_date, date(2026, 7, 6))
        item = purchase_request.items.get()
        self.assertEqual(item.request_qty, Decimal("7.0000"))
        self.assertEqual(item.suggested_supplier, self.supplier)
        self.assertEqual(item.needed_date, date(2026, 7, 8))

    def test_purchase_request_create_material_options_show_spec(self):
        self.client.force_login(self.user)
        self.raw.spec = "10K 1%"
        self.raw.save(update_fields=["spec"])

        response = self.client.get("/purchase/requests/new/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RM001｜原料 1｜规格型号：10K 1%")

    def test_purchase_request_submit_requires_purchase_process_permission(self):
        self.client.force_login(self.user)

        get_response = self.client.get("/purchase/requests/new/")
        post_response = self.client.post(
            "/purchase/requests/new/",
            {
                "needed_date": timezone.localdate().isoformat(),
                "remark": "无权限提交采购需求",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-material": self.raw.id,
                "items-0-request_qty": "7",
                "items-0-suggested_supplier": self.supplier.id,
                "items-0-needed_date": "",
                "items-1-material": "",
                "items-1-request_qty": "",
                "items-1-suggested_supplier": "",
                "items-1-needed_date": "",
                "items-2-material": "",
                "items-2-request_qty": "",
                "items-2-suggested_supplier": "",
                "items-2-needed_date": "",
                "action": "submit",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertNotContains(get_response, "保存并提交审核")
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(PurchaseRequest.objects.exists())

    def test_purchase_request_edit_updates_draft_request_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        purchase_request = PurchaseRequest.objects.create(
            purchase_request_no="PR-EDIT",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.DRAFT,
            requested_by=self.user,
            needed_date=timezone.localdate(),
            remark="编辑前",
        )
        item = PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            line_no=1,
            material=self.raw,
            request_qty=Decimal("3.0000"),
        )

        response = self.client.post(
            f"/purchase/requests/{purchase_request.id}/edit/",
            {
                "needed_date": timezone.localdate().isoformat(),
                "remark": "编辑后",
                "items-TOTAL_FORMS": "4",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-material": self.raw.id,
                "items-0-request_qty": "9",
                "items-0-suggested_supplier": self.supplier.id,
                "items-0-needed_date": "",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-material": "",
                "items-1-request_qty": "",
                "items-1-suggested_supplier": "",
                "items-1-needed_date": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-material": "",
                "items-2-request_qty": "",
                "items-2-suggested_supplier": "",
                "items-2-needed_date": "",
                "items-2-DELETE": "",
                "items-3-id": "",
                "items-3-material": "",
                "items-3-request_qty": "",
                "items-3-suggested_supplier": "",
                "items-3-needed_date": "",
                "items-3-DELETE": "",
                "action": "draft",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/requests/{purchase_request.id}/")
        purchase_request.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(purchase_request.remark, "编辑后")
        self.assertEqual(purchase_request.status, PurchaseRequest.Status.DRAFT)
        self.assertEqual(item.request_qty, Decimal("9.0000"))
        self.assertEqual(item.line_no, 1)
        audit_log = AuditLog.objects.get(action="purchase_request_update", source_doc_id=purchase_request.id)
        self.assertEqual(audit_log.before_snapshot["items"][0]["request_qty"], "3.0000")
        self.assertEqual(audit_log.after_snapshot["items"][0]["request_qty"], "9.0000")

    def test_purchase_request_edit_rejects_ordered_request(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        purchase_request = PurchaseRequest.objects.create(
            purchase_request_no="PR-ORDERED",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.APPROVED,
            requested_by=self.user,
        )
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            line_no=1,
            material=self.raw,
            request_qty=Decimal("3.0000"),
            line_status=PurchaseRequestItem.LineStatus.ORDERED,
        )

        response = self.client.get(f"/purchase/requests/{purchase_request.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/requests/{purchase_request.id}/")

    def test_purchase_request_voids_pending_request_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        purchase_request = PurchaseRequest.objects.create(
            purchase_request_no="PR-VOID",
            source_type=PurchaseRequest.SourceType.MANUAL,
            status=PurchaseRequest.Status.PENDING_APPROVAL,
            requested_by=self.user,
        )
        item = PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            line_no=1,
            material=self.raw,
            request_qty=Decimal("3.0000"),
        )

        response = self.client.post(f"/purchase/requests/{purchase_request.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/requests/{purchase_request.id}/")
        purchase_request.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(purchase_request.status, PurchaseRequest.Status.VOIDED)
        self.assertEqual(item.line_status, PurchaseRequestItem.LineStatus.CLOSED)
        audit_log = AuditLog.objects.get(action="purchase_request_void", source_doc_id=purchase_request.id)
        self.assertEqual(audit_log.before_snapshot["status"], PurchaseRequest.Status.PENDING_APPROVAL)
        self.assertEqual(audit_log.after_snapshot["status"], PurchaseRequest.Status.VOIDED)

    def test_confirm_supplier_return_shipment_deducts_inventory(self):
        supplier_return, batch = self._supplier_return()

        result = confirm_supplier_return_shipment(supplier_return.id, self.user.id, "supplier-return-1")

        self.assertTrue(result.success)
        supplier_return.refresh_from_db()
        batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.raw, location=self.location)
        transaction = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.SUPPLIER_RETURN_OUT)
        self.assertEqual(supplier_return.status, SupplierReturn.Status.SHIPPED)
        self.assertEqual(batch.remaining_qty, Decimal("16.0000"))
        self.assertEqual(inventory.qty, Decimal("16.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("-4.0000"))
        self.assertTrue(PendingEvent.objects.filter(event_type="supplier_return_out").exists())

    def test_supplier_return_detail_and_confirm_view(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        supplier_return, batch = self._supplier_return()

        page_response = self.client.get(f"/purchase/supplier-returns/{supplier_return.id}/")
        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, supplier_return.supplier_return_no)
        self.assertContains(page_response, "确认退货出库")
        self.assertContains(page_response, self.raw.material_code)
        self.assertContains(page_response, "返回进货单")
        self.assertContains(page_response, f'href="/purchase/receipts/{supplier_return.purchase_receipt.id}/"')
        self.assertContains(page_response, 'name="source_doc_type" value="supplier_return"', html=False)
        self.assertContains(page_response, f'name="source_doc_id" value="{supplier_return.id}"', html=False)
        self.assertContains(page_response, f'name="source_doc_no" value="{supplier_return.supplier_return_no}"', html=False)

        response = self.client.post(f"/purchase/supplier-returns/{supplier_return.id}/confirm-out/", {"current_password": "x"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/supplier-returns/{supplier_return.id}/")
        supplier_return.refresh_from_db()
        batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.raw, location=self.location)
        self.assertEqual(supplier_return.status, SupplierReturn.Status.SHIPPED)
        self.assertEqual(batch.remaining_qty, Decimal("16.0000"))
        self.assertEqual(inventory.qty, Decimal("16.0000"))

    def test_supplier_return_create_view_saves_header_and_items(self):
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))
        receipt.status = PurchaseReceipt.Status.RECEIVED
        receipt.save(update_fields=["status"])
        batch = InventoryBatch.objects.create(
            batch_no="B-SR-CREATE",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("20.0000"),
            remaining_qty=Decimal("20.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        receipt_item.batch = batch
        receipt_item.save(update_fields=["batch"])
        Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("20.0000"),
        )

        response = self.client.post(
            "/purchase/supplier-returns/new/",
            {
                "supplier": self.supplier.id,
                "purchase_receipt": receipt.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "页面创建供应商退货",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-purchase_receipt_item": receipt_item.id,
                "items-0-material": "",
                "items-0-return_qty": "4",
                "items-0-unit_price": "",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
                "items-0-return_reason": "质量问题",
                "items-1-purchase_receipt_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-batch": "",
                "items-1-location": "",
                "items-1-return_reason": "",
                "items-2-purchase_receipt_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-batch": "",
                "items-2-location": "",
                "items-2-return_reason": "",
                "action": "draft",
            },
        )

        supplier_return = SupplierReturn.objects.order_by("-id").first()
        return_item = supplier_return.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/supplier-returns/{supplier_return.id}/")
        self.assertEqual(supplier_return.status, SupplierReturn.Status.DRAFT)
        self.assertEqual(supplier_return.supplier, self.supplier)
        self.assertEqual(supplier_return.created_by, self.user)
        self.assertEqual(supplier_return.return_amount, Decimal("4.00"))
        self.assertEqual(return_item.material, self.raw)
        self.assertEqual(return_item.unit_price, Decimal("1.000000"))
        self.assertEqual(return_item.batch, batch)

    def test_supplier_return_create_view_filters_recent_receipts_by_default(self):
        self.client.force_login(self.user)

        def create_received_receipt(suffix, receipt_date):
            order = PurchaseOrder.objects.create(
                purchase_order_no=f"PO-SR-{suffix}",
                supplier=self.supplier,
                status=PurchaseOrder.Status.RECEIVED,
                order_date=receipt_date,
                total_amount=Decimal("3.00"),
            )
            order_item = PurchaseOrderItem.objects.create(
                purchase_order=order,
                line_no=1,
                material=self.raw,
                order_qty=Decimal("3.0000"),
                received_qty=Decimal("3.0000"),
                unit_price=Decimal("1.000000"),
                line_amount=Decimal("3.00"),
                line_status=PurchaseOrderItem.LineStatus.RECEIVED,
            )
            receipt = PurchaseReceipt.objects.create(
                purchase_receipt_no=f"GR-SR-{suffix}",
                purchase_order=order,
                supplier=self.supplier,
                receipt_date=receipt_date,
                status=PurchaseReceipt.Status.RECEIVED,
            )
            PurchaseReceiptItem.objects.create(
                purchase_receipt=receipt,
                purchase_order_item=order_item,
                material=self.raw,
                received_qty=Decimal("3.0000"),
                accepted_qty=Decimal("3.0000"),
                unit_price=Decimal("1.000000"),
                location=self.location,
            )
            return receipt

        recent_receipt = create_received_receipt("RECENT", timezone.localdate())
        old_receipt = create_received_receipt("OLD", timezone.localdate() - timedelta(days=10))

        response = self.client.get("/purchase/supplier-returns/new/")
        show_all_response = self.client.get("/purchase/supplier-returns/new/?show_all_receipts=1")

        self.assertContains(response, recent_receipt.purchase_receipt_no)
        self.assertNotContains(response, old_receipt.purchase_receipt_no)
        self.assertContains(show_all_response, recent_receipt.purchase_receipt_no)
        self.assertContains(show_all_response, old_receipt.purchase_receipt_no)

    def test_supplier_return_receipt_items_endpoint_returns_returnable_materials(self):
        self.client.force_login(self.user)
        self.raw.spec = "测试规格 10A"
        self.raw.save(update_fields=["spec"])
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))
        receipt.status = PurchaseReceipt.Status.RECEIVED
        receipt.save(update_fields=["status"])
        batch = InventoryBatch.objects.create(
            batch_no="B-SR-ENDPOINT",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("20.0000"),
            remaining_qty=Decimal("20.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        receipt_item.batch = batch
        receipt_item.save(update_fields=["batch"])

        response = self.client.get(f"/purchase/supplier-returns/receipt-items/?purchase_receipt={receipt.id}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["supplier"]["id"], self.supplier.id)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], receipt_item.id)
        self.assertEqual(item["material_id"], self.raw.id)
        self.assertEqual(item["batch_id"], batch.id)
        self.assertEqual(item["location_id"], self.location.id)
        self.assertEqual(item["unit_price"], "1.000000")
        self.assertEqual(item["returnable_qty"], "20.0000")
        self.assertIn(self.raw.material_code, item["label"])
        self.assertIn("测试规格 10A", item["label"])

    def test_supplier_return_submit_requires_purchase_process_permission(self):
        self.client.force_login(self.user)
        order, order_item, receipt, receipt_item = self._purchase_receipt(accepted_qty=Decimal("20.0000"))
        receipt.status = PurchaseReceipt.Status.RECEIVED
        receipt.save(update_fields=["status"])
        batch = InventoryBatch.objects.create(
            batch_no="B-SR-DENIED",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("20.0000"),
            remaining_qty=Decimal("20.0000"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        receipt_item.batch = batch
        receipt_item.save(update_fields=["batch"])
        Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("20.0000"),
        )

        get_response = self.client.get("/purchase/supplier-returns/new/")
        post_response = self.client.post(
            "/purchase/supplier-returns/new/",
            {
                "supplier": self.supplier.id,
                "purchase_receipt": receipt.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "无权限提交供应商退货",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-purchase_receipt_item": receipt_item.id,
                "items-0-material": "",
                "items-0-return_qty": "4",
                "items-0-unit_price": "",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
                "items-0-return_reason": "质量问题",
                "items-1-purchase_receipt_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-batch": "",
                "items-1-location": "",
                "items-1-return_reason": "",
                "items-2-purchase_receipt_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-batch": "",
                "items-2-location": "",
                "items-2-return_reason": "",
                "action": "submit",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertNotContains(get_response, "保存并提交审核")
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(SupplierReturn.objects.exists())

    def test_supplier_return_edit_updates_draft_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        supplier_return, batch = self._supplier_return(qty=Decimal("2.0000"))
        supplier_return.status = SupplierReturn.Status.DRAFT
        supplier_return.return_amount = Decimal("2.00")
        supplier_return.save(update_fields=["status", "return_amount"])
        return_item = supplier_return.items.get()
        receipt_item = return_item.purchase_receipt_item

        response = self.client.post(
            f"/purchase/supplier-returns/{supplier_return.id}/edit/",
            {
                "supplier": self.supplier.id,
                "purchase_receipt": supplier_return.purchase_receipt.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "编辑供应商退货",
                "items-TOTAL_FORMS": "4",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": return_item.id,
                "items-0-purchase_receipt_item": receipt_item.id,
                "items-0-material": self.raw.id,
                "items-0-return_qty": "5",
                "items-0-unit_price": "1.5",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
                "items-0-return_reason": "改数量",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-purchase_receipt_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-batch": "",
                "items-1-location": "",
                "items-1-return_reason": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-purchase_receipt_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-batch": "",
                "items-2-location": "",
                "items-2-return_reason": "",
                "items-2-DELETE": "",
                "items-3-id": "",
                "items-3-purchase_receipt_item": "",
                "items-3-material": "",
                "items-3-return_qty": "",
                "items-3-unit_price": "",
                "items-3-batch": "",
                "items-3-location": "",
                "items-3-return_reason": "",
                "items-3-DELETE": "",
                "action": "submit",
            },
        )

        self.assertEqual(response.status_code, 302)
        supplier_return.refresh_from_db()
        return_item.refresh_from_db()
        self.assertEqual(supplier_return.status, SupplierReturn.Status.PENDING_APPROVAL)
        self.assertEqual(supplier_return.return_amount, Decimal("7.50"))
        self.assertEqual(return_item.return_qty, Decimal("5"))
        self.assertEqual(return_item.return_amount, Decimal("7.50"))
        self.assertTrue(AuditLog.objects.filter(action="supplier_return_update", source_doc_id=supplier_return.id).exists())

    def test_supplier_return_print_masks_amount_and_records_log(self):
        self.client.force_login(self.user)
        supplier_return, batch = self._supplier_return()
        supplier_return.supplier_return_no = "SR-PRINT"
        supplier_return.return_amount = Decimal("4.00")
        supplier_return.save(update_fields=["supplier_return_no", "return_amount"])

        detail_response = self.client.get(f"/purchase/supplier-returns/{supplier_return.id}/")
        response = self.client.get(f"/purchase/supplier-returns/{supplier_return.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "供应商退货单")
        self.assertContains(response, "SR-PRINT")
        self.assertContains(response, "******")
        self.assertNotContains(response, "1.000000")
        print_log = PrintLog.objects.get(source_doc_type="supplier_return", source_doc_id=supplier_return.id)
        self.assertEqual(print_log.template_type, "supplier_return")
        self.assertEqual(print_log.source_doc_no, supplier_return.supplier_return_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_supplier_return_edit_rejects_over_return_qty(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        supplier_return, batch = self._supplier_return(qty=Decimal("18.0000"))
        supplier_return.status = SupplierReturn.Status.CONFIRMED
        supplier_return.save(update_fields=["status"])
        receipt = supplier_return.purchase_receipt
        receipt.status = PurchaseReceipt.Status.RECEIVED
        receipt.save(update_fields=["status"])
        receipt_item = supplier_return.items.get().purchase_receipt_item
        draft_return = SupplierReturn.objects.create(
            supplier_return_no="SR-DRAFT-OVER",
            supplier=self.supplier,
            purchase_receipt=receipt,
            return_date=timezone.localdate(),
            status=SupplierReturn.Status.DRAFT,
            created_by=self.user,
        )

        response = self.client.post(
            f"/purchase/supplier-returns/{draft_return.id}/edit/",
            {
                "supplier": self.supplier.id,
                "purchase_receipt": receipt.id,
                "return_date": timezone.localdate().isoformat(),
                "remark": "",
                "items-TOTAL_FORMS": "3",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-purchase_receipt_item": receipt_item.id,
                "items-0-material": "",
                "items-0-return_qty": "3",
                "items-0-unit_price": "",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
                "items-0-return_reason": "",
                "items-0-DELETE": "",
                "items-1-id": "",
                "items-1-purchase_receipt_item": "",
                "items-1-material": "",
                "items-1-return_qty": "",
                "items-1-unit_price": "",
                "items-1-batch": "",
                "items-1-location": "",
                "items-1-return_reason": "",
                "items-1-DELETE": "",
                "items-2-id": "",
                "items-2-purchase_receipt_item": "",
                "items-2-material": "",
                "items-2-return_qty": "",
                "items-2-unit_price": "",
                "items-2-batch": "",
                "items-2-location": "",
                "items-2-return_reason": "",
                "items-2-DELETE": "",
                "action": "draft",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "退货数量不能超过可退数量")
        self.assertEqual(draft_return.items.count(), 0)

    def test_supplier_return_voids_pending_order_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        supplier_return, batch = self._supplier_return(qty=Decimal("2.0000"))
        supplier_return.status = SupplierReturn.Status.PENDING_APPROVAL
        supplier_return.save(update_fields=["status"])

        response = self.client.post(f"/purchase/supplier-returns/{supplier_return.id}/void/", {"current_password": "x", "void_reason": "测试作废"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/purchase/supplier-returns/{supplier_return.id}/")
        supplier_return.refresh_from_db()
        self.assertEqual(supplier_return.status, SupplierReturn.Status.VOIDED)
        self.assertTrue(AuditLog.objects.filter(action="supplier_return_void", source_doc_id=supplier_return.id).exists())


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
