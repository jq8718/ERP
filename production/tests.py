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
from masterdata.models import Customer, CustomerProduct, Material
from production.models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)
from production.services import confirm_material_requisition, confirm_production_receipt
from sales.models import SalesOrder, SalesOrderItem
from system.models import AuditLog, PendingEvent


class ProductionServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="producer", password="x")
        self.customer = Customer.objects.create(customer_no="C001", customer_name="测试客户")
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
        self.sales_order = SalesOrder.objects.create(
            sales_order_no="SO001",
            customer=self.customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.CONFIRMED,
            total_amount=Decimal("100.00"),
        )
        self.sales_item = SalesOrderItem.objects.create(
            sales_order=self.sales_order,
            line_no=1,
            customer_product=self.customer_product,
            finished_material=self.finished,
            order_qty=Decimal("10.0000"),
            unit_price=Decimal("10.0000"),
            line_amount=Decimal("100.00"),
            locked_bom=self.bom,
            locked_bom_version=self.bom.bom_version,
            line_status=SalesOrderItem.LineStatus.CONFIRMED,
            inventory_check_status=SalesOrderItem.InventoryCheckStatus.KITTED,
        )
        self.production_order = ProductionOrder.objects.create(
            production_order_no="MO001",
            sales_order_item=self.sales_item,
            finished_material=self.finished,
            production_qty=Decimal("10.0000"),
            locked_bom=self.bom,
            locked_bom_version=self.bom.bom_version,
            status=ProductionOrder.Status.PENDING,
        )
        self._grant_permission(PermissionCode.PRODUCTION_VIEW)

    def _grant_permission(self, permission_code: str):
        permission_type = Permission.PermissionType.MODULE if permission_code == PermissionCode.PRODUCTION_VIEW else Permission.PermissionType.ACTION
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_type,
            },
        )
        role = Role.objects.create(role_code=f"production-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def _raw_stock(self, qty=Decimal("20.0000")):
        batch = InventoryBatch.objects.create(
            batch_no="BRM001",
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=qty,
            remaining_qty=qty,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=self.raw,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=qty,
        )
        return batch

    def _requisition(self, batch):
        requisition = ProductionMaterialRequisition.objects.create(
            requisition_no="MR001",
            production_order=self.production_order,
            requisition_date=timezone.localdate(),
            status=ProductionMaterialRequisition.Status.PENDING_CONFIRM,
        )
        ProductionMaterialRequisitionItem.objects.create(
            requisition=requisition,
            production_order=self.production_order,
            line_no=1,
            material=self.raw,
            required_qty=Decimal("20.0000"),
            issued_qty=Decimal("20.0000"),
            batch=batch,
            location=self.location,
        )
        return requisition

    def _production_receipt(self):
        receipt = ProductionReceipt.objects.create(
            production_receipt_no="PI001",
            production_order=self.production_order,
            receipt_date=timezone.localdate(),
            status=ProductionReceipt.Status.PENDING_CONFIRM,
        )
        ProductionReceiptItem.objects.create(
            production_receipt=receipt,
            production_order=self.production_order,
            line_no=1,
            finished_material=self.finished,
            receipt_qty=Decimal("10.0000"),
            location=self.location,
        )
        return receipt

    def test_confirm_material_requisition_deducts_raw_inventory(self):
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        result = confirm_material_requisition(requisition.id, self.user.id, "issue-1")

        self.assertTrue(result.success)
        batch.refresh_from_db()
        requisition.refresh_from_db()
        self.production_order.refresh_from_db()
        self.sales_item.refresh_from_db()
        inventory = Inventory.objects.get(material=self.raw, location=self.location)
        self.assertEqual(batch.remaining_qty, Decimal("0.0000"))
        self.assertEqual(batch.batch_status, InventoryBatch.BatchStatus.USED_UP)
        self.assertEqual(inventory.qty, Decimal("0.0000"))
        self.assertEqual(requisition.status, ProductionMaterialRequisition.Status.ISSUED)
        self.assertEqual(self.production_order.status, ProductionOrder.Status.IN_PROGRESS)
        self.assertEqual(self.sales_item.line_status, SalesOrderItem.LineStatus.IN_PRODUCTION)
        self.assertEqual(InventoryTransaction.objects.get().transaction_type, InventoryTransaction.TransactionType.PRODUCTION_ISSUE)

    def test_confirm_production_receipt_increases_finished_inventory(self):
        receipt = self._production_receipt()

        result = confirm_production_receipt(receipt.id, self.user.id, "receipt-1")

        self.assertTrue(result.success)
        receipt.refresh_from_db()
        self.production_order.refresh_from_db()
        self.sales_item.refresh_from_db()
        finished_inventory = Inventory.objects.get(material=self.finished, location=self.location)
        self.assertEqual(receipt.status, ProductionReceipt.Status.RECEIVED)
        self.assertEqual(self.production_order.status, ProductionOrder.Status.COMPLETED)
        self.assertEqual(self.production_order.received_qty, Decimal("10.0000"))
        self.assertEqual(finished_inventory.qty, Decimal("10.0000"))
        self.assertEqual(self.sales_item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SUFFICIENT)
        self.assertEqual(self.sales_item.line_status, SalesOrderItem.LineStatus.CONFIRMED)
        self.assertTrue(InventoryTransaction.objects.filter(transaction_type=InventoryTransaction.TransactionType.PRODUCTION_RECEIPT).exists())
        self.assertTrue(PendingEvent.objects.filter(event_type="production_received").exists())

    def test_production_order_detail_creates_material_requisition(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()

        page_response = self.client.get(f"/production/orders/{self.production_order.id}/")
        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, self.production_order.production_order_no)
        self.assertContains(page_response, "生成领料单")

        response = self.client.post(f"/production/orders/{self.production_order.id}/create-requisition/")

        requisition = ProductionMaterialRequisition.objects.get()
        requisition_item = requisition.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/requisitions/{requisition.id}/")
        self.assertEqual(requisition.production_order, self.production_order)
        self.assertEqual(requisition.status, ProductionMaterialRequisition.Status.PENDING_CONFIRM)
        self.assertEqual(requisition.created_by, self.user)
        self.assertEqual(requisition_item.material, self.raw)
        self.assertEqual(requisition_item.issued_qty, Decimal("20.0000"))
        self.assertEqual(requisition_item.batch, batch)

    def test_production_order_detail_creates_material_requisition_with_bom_base_qty_and_loss_rate(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        self.bom.base_qty = Decimal("2.0000")
        self.bom.save(update_fields=["base_qty"])
        bom_item = self.bom.items.get()
        bom_item.usage_qty = Decimal("2.000000")
        bom_item.loss_rate = Decimal("0.100000")
        bom_item.save(update_fields=["usage_qty", "loss_rate"])
        batch = self._raw_stock()

        response = self.client.post(f"/production/orders/{self.production_order.id}/create-requisition/")

        requisition = ProductionMaterialRequisition.objects.get()
        requisition_item = requisition.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(requisition_item.required_qty, Decimal("11.0000"))
        self.assertEqual(requisition_item.issued_qty, Decimal("11.0000"))
        self.assertEqual(requisition_item.batch, batch)

    def test_production_order_detail_shows_attachment_panel(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        Attachment.objects.create(
            attachment_no="ATT-MO-001",
            source_doc_type="production_order",
            source_doc_id=self.production_order.id,
            source_doc_no=self.production_order.production_order_no,
            original_filename="production-order.pdf",
            stored_filename="production-order.pdf",
            file_path="attachments/production-order.pdf",
            file_size=100,
            uploaded_by=self.user,
        )

        response = self.client.get(f"/production/orders/{self.production_order.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "production-order.pdf")
        self.assertContains(response, 'name="source_doc_type" value="production_order"')

    def test_production_order_create_view_creates_manual_order(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.post(
            "/production/orders/new/",
            {
                "finished_material": self.finished.id,
                "production_qty": "5",
                "locked_bom": self.bom.id,
                "planned_start_date": timezone.localdate().isoformat(),
                "planned_finish_date": "",
                "remark": "手工生产",
            },
        )

        order = ProductionOrder.objects.order_by("-id").first()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/orders/{order.id}/")
        self.assertEqual(order.finished_material, self.finished)
        self.assertEqual(order.production_qty, Decimal("5.0000"))
        self.assertEqual(order.locked_bom, self.bom)
        self.assertEqual(order.locked_bom_version, self.bom.bom_version)
        self.assertEqual(order.created_by, self.user)

    def test_production_order_create_requires_production_process_permission(self):
        self.client.force_login(self.user)

        list_response = self.client.get("/production/orders/")
        get_response = self.client.get("/production/orders/new/")
        post_response = self.client.post(
            "/production/orders/new/",
            {
                "finished_material": self.finished.id,
                "production_qty": "5",
                "locked_bom": self.bom.id,
                "planned_start_date": timezone.localdate().isoformat(),
                "planned_finish_date": "",
                "remark": "手工生产",
            },
        )

        self.assertNotContains(list_response, "/production/orders/new/")
        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(ProductionOrder.objects.filter(remark="手工生产").exists())

    def test_production_order_import_template_downloads_csv(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.get("/production/orders/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("生产指令号,销售订单号,销售订单行号,物料编码", content)
        self.assertIn("MO-INIT-001", content)

    def test_production_order_import_creates_pending_order_with_locked_sales_bom(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        self.production_order.delete()
        upload = SimpleUploadedFile(
            "production_orders.csv",
            (
                "生产指令号,销售订单号,销售订单行号,物料编码,生产数量,BOM 编号,BOM 版本,计划开始日期,计划完成日期,备注\n"
                "MO-IMP-001,SO001,1,FG001,6,,,2026-06-10,2026-06-15,导入生产\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/production/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/production/orders/")
        order = ProductionOrder.objects.get(production_order_no="MO-IMP-001")
        self.assertEqual(order.status, ProductionOrder.Status.PENDING)
        self.assertEqual(order.sales_order_item, self.sales_item)
        self.assertEqual(order.finished_material, self.finished)
        self.assertEqual(order.production_qty, Decimal("6.0000"))
        self.assertEqual(order.locked_bom, self.bom)
        self.assertEqual(order.locked_bom_version, self.bom.bom_version)
        self.assertEqual(order.created_by, self.user)
        self.assertEqual(order.updated_by, self.user)
        job = ImportJob.objects.get(template_type="production_orders")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_production_order_import_uses_default_bom_for_manual_order(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        upload = SimpleUploadedFile(
            "production_orders.csv",
            (
                "生产指令号,销售订单号,销售订单行号,物料编码,生产数量,BOM 编号,BOM 版本,计划开始日期,计划完成日期,备注\n"
                "MO-IMP-MANUAL,,,FG001,3,,,2026-06-10,,手工导入\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/production/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        order = ProductionOrder.objects.get(production_order_no="MO-IMP-MANUAL")
        self.assertIsNone(order.sales_order_item)
        self.assertEqual(order.finished_material, self.finished)
        self.assertEqual(order.production_qty, Decimal("3.0000"))
        self.assertEqual(order.locked_bom, self.bom)
        self.assertEqual(order.planned_start_date.isoformat(), "2026-06-10")

    def test_production_order_import_reports_validation_errors(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        upload = SimpleUploadedFile(
            "production_orders.csv",
            (
                "生产指令号,销售订单号,销售订单行号,物料编码,生产数量,BOM 编号,BOM 版本,计划开始日期,计划完成日期,备注\n"
                "MO-BAD,SO001,1,FG001,99,,,bad-date,2026-06-01,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/production/orders/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "生产数量不能超过来源销售订单行剩余未排产数量")
        self.assertContains(response, "计划开始日期格式错误")
        job = ImportJob.objects.get(template_type="production_orders")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)
        self.assertFalse(ProductionOrder.objects.filter(production_order_no="MO-BAD").exists())

    def test_production_order_import_requires_production_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/production/orders/import-template/")
        import_response = self.client.get("/production/orders/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_material_requisition_import_template_downloads_csv(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.get("/production/requisitions/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("领料单号,生产指令号,领料日期", content)
        self.assertIn("MR-INIT-001", content)

    def test_material_requisition_import_creates_pending_requisition_without_deducting_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock(qty=Decimal("20.0000"))
        upload = SimpleUploadedFile(
            "material_requisitions.csv",
            (
                "领料单号,生产指令号,领料日期,物料编码,应发数量,实发数量,批次号,库位编码,调整原因,备注\n"
                f"MR-IMP-001,MO001,2026-06-10,RM001,20,15,{batch.batch_no},{self.location.location_code},先领 15,导入领料\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/production/requisitions/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/production/requisitions/")
        requisition = ProductionMaterialRequisition.objects.get(requisition_no="MR-IMP-001")
        item = requisition.items.get()
        self.assertEqual(requisition.status, ProductionMaterialRequisition.Status.PENDING_CONFIRM)
        self.assertEqual(requisition.production_order, self.production_order)
        self.assertEqual(requisition.created_by, self.user)
        self.assertEqual(item.line_no, 1)
        self.assertEqual(item.material, self.raw)
        self.assertEqual(item.required_qty, Decimal("20"))
        self.assertEqual(item.issued_qty, Decimal("15"))
        self.assertEqual(item.batch, batch)
        self.assertEqual(item.location, self.location)
        self.assertEqual(item.adjust_reason, "先领 15")
        batch.refresh_from_db()
        self.assertEqual(batch.remaining_qty, Decimal("20.0000"))
        job = ImportJob.objects.get(template_type="production_material_requisitions")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_material_requisition_import_reports_validation_errors(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        upload = SimpleUploadedFile(
            "material_requisitions.csv",
            (
                "领料单号,生产指令号,领料日期,物料编码,应发数量,实发数量,批次号,库位编码,调整原因,备注\n"
                "MR-BAD,MO001,bad-date,FG001,-1,5,B-MISSING,L-MISSING,错误,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/production/requisitions/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "领料日期格式错误")
        self.assertContains(response, "领料物料不在生产指令锁定 BOM 子件中")
        self.assertContains(response, "需求数量必须大于 0")
        self.assertContains(response, "实领数量不能超过需求数量")
        self.assertContains(response, "批次不存在、不是可用库存或未在库")
        self.assertContains(response, "库位不存在或未启用")
        job = ImportJob.objects.get(template_type="production_material_requisitions")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertFalse(ProductionMaterialRequisition.objects.filter(requisition_no="MR-BAD").exists())

    def test_material_requisition_import_requires_production_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/production/requisitions/import-template/")
        import_response = self.client.get("/production/requisitions/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_production_receipt_import_template_downloads_csv(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.get("/production/receipts/import-template/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("生产入库单号,生产指令号,单据日期", content)
        self.assertIn("PI-INIT-001", content)

    def test_production_receipt_import_creates_pending_receipt_without_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        upload = SimpleUploadedFile(
            "production_receipts.csv",
            (
                "生产入库单号,生产指令号,单据日期,入库数量,库位编码,批次号,质量状态,备注\n"
                f"PI-IMP-001,MO001,2026-06-10,6,{self.location.location_code},FG-IMP-001,pending,导入入库\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post("/production/receipts/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/production/receipts/")
        receipt = ProductionReceipt.objects.get(production_receipt_no="PI-IMP-001")
        item = receipt.items.get()
        self.assertEqual(receipt.status, ProductionReceipt.Status.PENDING_CONFIRM)
        self.assertEqual(receipt.production_order, self.production_order)
        self.assertEqual(receipt.created_by, self.user)
        self.assertEqual(item.line_no, 1)
        self.assertEqual(item.finished_material, self.finished)
        self.assertEqual(item.receipt_qty, Decimal("6"))
        self.assertEqual(item.location, self.location)
        self.assertIsNone(item.batch)
        self.assertEqual(item.batch_no, "FG-IMP-001")
        self.assertEqual(item.quality_status, ProductionReceiptItem.QualityStatus.PENDING)
        self.production_order.refresh_from_db()
        self.assertEqual(self.production_order.received_qty, Decimal("0.0000"))
        self.assertFalse(InventoryBatch.objects.filter(batch_no="FG-IMP-001").exists())
        self.assertFalse(Inventory.objects.filter(material=self.finished, location=self.location).exists())
        job = ImportJob.objects.get(template_type="production_receipts")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 1)

    def test_production_receipt_import_reports_validation_errors(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        self.production_order.received_qty = Decimal("8.0000")
        self.production_order.save(update_fields=["received_qty"])
        upload = SimpleUploadedFile(
            "production_receipts.csv",
            (
                "生产入库单号,生产指令号,单据日期,入库数量,库位编码,批次号,质量状态,备注\n"
                "PI-BAD,MO001,bad-date,3,L-MISSING,,bad-status,错误\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post("/production/receipts/import/", {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "入库日期格式错误")
        self.assertContains(response, "入库数量不能超过生产指令剩余未入库数量")
        self.assertContains(response, "库位不存在或未启用")
        self.assertContains(response, "质量状态必须是 qualified、pending 或 defective")
        job = ImportJob.objects.get(template_type="production_receipts")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertFalse(ProductionReceipt.objects.filter(production_receipt_no="PI-BAD").exists())

    def test_production_receipt_import_requires_production_process_permission(self):
        self.client.force_login(self.user)

        template_response = self.client.get("/production/receipts/import-template/")
        import_response = self.client.get("/production/receipts/import/")

        self.assertEqual(template_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)

    def test_production_order_edit_updates_pending_order_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.post(
            f"/production/orders/{self.production_order.id}/edit/",
            {
                "finished_material": self.finished.id,
                "production_qty": "8",
                "locked_bom": self.bom.id,
                "planned_start_date": timezone.localdate().isoformat(),
                "planned_finish_date": "",
                "remark": "调整生产数量",
                "operation_reason": "插单调整产量",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/orders/{self.production_order.id}/")
        self.production_order.refresh_from_db()
        self.assertEqual(self.production_order.production_qty, Decimal("8.0000"))
        self.assertEqual(self.production_order.remark, "调整生产数量")
        self.assertEqual(self.production_order.updated_by, self.user)
        audit_log = AuditLog.objects.get(action="production_order_update", source_doc_id=self.production_order.id)
        self.assertEqual(audit_log.before_snapshot["production_qty"], "10.0000")
        self.assertEqual(audit_log.after_snapshot["production_qty"], "8.0000")
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "插单调整产量")

    def test_production_order_edit_rejects_order_with_requisition(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()
        self._requisition(batch)

        response = self.client.get(f"/production/orders/{self.production_order.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/orders/{self.production_order.id}/")

    def test_production_order_cancel_updates_pending_order_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.post(
            f"/production/orders/{self.production_order.id}/cancel/",
            {"current_password": "x", "cancel_reason": "测试取消"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/orders/{self.production_order.id}/")
        self.production_order.refresh_from_db()
        self.assertEqual(self.production_order.status, ProductionOrder.Status.CANCELLED)
        audit_log = AuditLog.objects.get(action="production_order_cancel", source_doc_id=self.production_order.id)
        self.assertEqual(audit_log.before_snapshot["status"], ProductionOrder.Status.PENDING)
        self.assertEqual(audit_log.after_snapshot["status"], ProductionOrder.Status.CANCELLED)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "测试取消")

    def test_production_order_cancel_requires_reason(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        response = self.client.post(
            f"/production/orders/{self.production_order.id}/cancel/",
            {"current_password": "x", "cancel_reason": ""},
            follow=True,
        )

        self.production_order.refresh_from_db()
        self.assertEqual(self.production_order.status, ProductionOrder.Status.PENDING)
        self.assertContains(response, "请填写生产指令取消原因")
        self.assertFalse(AuditLog.objects.filter(action="production_order_cancel", source_doc_id=self.production_order.id).exists())

    def test_production_order_create_requisition_requires_permission(self):
        self.client.force_login(self.user)
        self._raw_stock()

        response = self.client.post(f"/production/orders/{self.production_order.id}/create-requisition/")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProductionMaterialRequisition.objects.exists())

    def test_production_order_detail_creates_production_receipt(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)

        page_response = self.client.get(f"/production/orders/{self.production_order.id}/")
        self.assertContains(page_response, "生成入库单")

        response = self.client.post(
            f"/production/orders/{self.production_order.id}/create-receipt/",
            {"location": self.location.id},
        )

        receipt = ProductionReceipt.objects.get()
        receipt_item = receipt.items.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/receipts/{receipt.id}/")
        self.assertEqual(receipt.production_order, self.production_order)
        self.assertEqual(receipt.status, ProductionReceipt.Status.PENDING_CONFIRM)
        self.assertEqual(receipt.created_by, self.user)
        self.assertEqual(receipt_item.receipt_qty, Decimal("10.0000"))
        self.assertEqual(receipt_item.location, self.location)

    def test_material_requisition_detail_renders_confirm_action(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        response = self.client.get(f"/production/requisitions/{requisition.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, requisition.requisition_no)
        self.assertContains(response, "打印")
        self.assertContains(response, "确认领料出库")
        self.assertContains(response, self.raw.material_code)
        self.assertContains(response, "返回生产单")
        self.assertContains(response, f'href="/production/orders/{self.production_order.id}/"')

    def test_material_requisition_edit_updates_pending_requisition_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()
        requisition = self._requisition(batch)
        item = requisition.items.get()

        response = self.client.post(
            f"/production/requisitions/{requisition.id}/edit/",
            {
                "requisition_date": timezone.localdate().isoformat(),
                "remark": "少领一部分",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-issued_qty": "15",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
                "items-0-adjust_reason": "先领 15",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/requisitions/{requisition.id}/")
        requisition.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(requisition.remark, "少领一部分")
        self.assertEqual(item.issued_qty, Decimal("15.0000"))
        self.assertEqual(item.adjust_reason, "先领 15")
        audit_log = AuditLog.objects.get(action="production_material_requisition_update", source_doc_id=requisition.id)
        self.assertEqual(audit_log.before_snapshot["items"][0]["issued_qty"], "20.0000")
        self.assertEqual(audit_log.after_snapshot["items"][0]["issued_qty"], "15.0000")

    def test_material_requisition_edit_rejects_issued_requisition(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()
        requisition = self._requisition(batch)
        requisition.status = ProductionMaterialRequisition.Status.ISSUED
        requisition.save(update_fields=["status"])

        response = self.client.get(f"/production/requisitions/{requisition.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/requisitions/{requisition.id}/")

    def test_material_requisition_edit_rejects_qty_over_batch_remaining(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock(qty=Decimal("10.0000"))
        requisition = self._requisition(batch)
        item = requisition.items.get()

        response = self.client.post(
            f"/production/requisitions/{requisition.id}/edit/",
            {
                "requisition_date": timezone.localdate().isoformat(),
                "remark": "超批次",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-issued_qty": "15",
                "items-0-batch": batch.id,
                "items-0-location": self.location.id,
                "items-0-adjust_reason": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "实领数量不能超过批次剩余数量")
        item.refresh_from_db()
        self.assertEqual(item.issued_qty, Decimal("20.0000"))

    def test_material_requisition_print_records_log(self):
        self.client.force_login(self.user)
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        response = self.client.get(f"/production/requisitions/{requisition.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "生产领料单")
        self.assertContains(response, requisition.requisition_no)
        self.assertContains(response, self.raw.material_code)
        print_log = PrintLog.objects.get(
            source_doc_type="production_material_requisition",
            source_doc_id=requisition.id,
        )
        self.assertEqual(print_log.template_type, "production_material_requisition")
        self.assertEqual(print_log.source_doc_no, requisition.requisition_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_material_requisition_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        list_response = self.client.get("/production/requisitions/")
        response = self.client.get("/production/requisitions/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("领料单号,生产单,领料日期,状态", content)
        self.assertIn(requisition.requisition_no, content)
        export_log = ExportLog.objects.get(module="production_material_requisitions")
        self.assertEqual(export_log.row_count, 1)

    def test_production_order_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        self.production_order.production_order_no = "MO-EXPORT"
        self.production_order.save(update_fields=["production_order_no"])

        list_response = self.client.get("/production/orders/")
        response = self.client.get("/production/orders/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("生产单号,成品,生产数量,已入库,状态", content)
        self.assertIn("MO-EXPORT", content)
        export_log = ExportLog.objects.get(module="production_orders")
        self.assertEqual(export_log.row_count, 1)

    def test_production_order_print_records_log(self):
        self.client.force_login(self.user)
        self.production_order.production_order_no = "MO-PRINT"
        self.production_order.save(update_fields=["production_order_no"])

        detail_response = self.client.get(f"/production/orders/{self.production_order.id}/")
        response = self.client.get(f"/production/orders/{self.production_order.id}/print/")

        self.assertContains(detail_response, "打印")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "生产指令单")
        self.assertContains(response, "MO-PRINT")
        self.assertContains(response, self.raw.material_code)
        print_log = PrintLog.objects.get(source_doc_type="production_order", source_doc_id=self.production_order.id)
        self.assertEqual(print_log.template_type, "production_order")
        self.assertEqual(print_log.source_doc_no, self.production_order.production_order_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_material_requisition_list_filter_and_export_share_query(self):
        self.client.force_login(self.user)
        batch = self._raw_stock()
        requisition = self._requisition(batch)
        requisition.requisition_no = "MR-FILTER-KEEP"
        requisition.status = ProductionMaterialRequisition.Status.ISSUED
        requisition.save(update_fields=["requisition_no", "status"])
        ProductionMaterialRequisition.objects.create(
            requisition_no="MR-FILTER-HIDE",
            production_order=self.production_order,
            requisition_date=timezone.localdate(),
            status=ProductionMaterialRequisition.Status.PENDING_CONFIRM,
        )

        list_response = self.client.get("/production/requisitions/?q=KEEP&status=issued")
        export_response = self.client.get("/production/requisitions/export/?q=KEEP&status=issued")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "MR-FILTER-KEEP")
        self.assertNotContains(list_response, "MR-FILTER-HIDE")
        self.assertContains(list_response, "/production/requisitions/export/?q=KEEP&amp;status=issued")
        self.assertIn("MR-FILTER-KEEP", content)
        self.assertNotIn("MR-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="production_material_requisitions")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "issued")

    def test_material_requisition_confirm_view_deducts_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        response = self.client.post(
            f"/production/requisitions/{requisition.id}/confirm/",
            {"current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/requisitions/{requisition.id}/")
        batch.refresh_from_db()
        requisition.refresh_from_db()
        self.production_order.refresh_from_db()
        self.sales_item.refresh_from_db()
        inventory = Inventory.objects.get(material=self.raw, location=self.location)
        transaction_row = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.PRODUCTION_ISSUE)
        self.assertEqual(batch.remaining_qty, Decimal("0.0000"))
        self.assertEqual(inventory.qty, Decimal("0.0000"))
        self.assertEqual(requisition.status, ProductionMaterialRequisition.Status.ISSUED)
        self.assertEqual(self.production_order.status, ProductionOrder.Status.IN_PROGRESS)
        self.assertEqual(self.sales_item.line_status, SalesOrderItem.LineStatus.IN_PRODUCTION)
        self.assertEqual(transaction_row.qty_delta, Decimal("-20.0000"))

    def test_material_requisition_confirm_requires_production_process_permission(self):
        self.client.force_login(self.user)
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        response = self.client.post(f"/production/requisitions/{requisition.id}/confirm/")

        self.assertEqual(response.status_code, 403)
        requisition.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(requisition.status, ProductionMaterialRequisition.Status.PENDING_CONFIRM)
        self.assertEqual(batch.remaining_qty, Decimal("20.0000"))

    def test_material_requisition_confirm_requires_second_verify_password(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        batch = self._raw_stock()
        requisition = self._requisition(batch)

        response = self.client.post(
            f"/production/requisitions/{requisition.id}/confirm/",
            {"current_password": "wrong-password"},
            follow=True,
        )

        requisition.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(requisition.status, ProductionMaterialRequisition.Status.PENDING_CONFIRM)
        self.assertEqual(batch.remaining_qty, Decimal("20.0000"))
        self.assertContains(response, "二次验证失败")
        self.assertFalse(
            InventoryTransaction.objects.filter(
                transaction_type=InventoryTransaction.TransactionType.PRODUCTION_ISSUE
            ).exists()
        )

    def test_production_process_actions_require_production_process_permission(self):
        self.client.force_login(self.user)
        batch = self._raw_stock()
        requisition = self._requisition(batch)
        receipt = self._production_receipt()

        order_detail = self.client.get(f"/production/orders/{self.production_order.id}/")
        requisition_detail = self.client.get(f"/production/requisitions/{requisition.id}/")
        receipt_detail = self.client.get(f"/production/receipts/{receipt.id}/")

        self.assertNotContains(order_detail, f"/production/orders/{self.production_order.id}/edit/")
        self.assertNotContains(order_detail, "生成领料单")
        self.assertNotContains(order_detail, "生成入库单")
        self.assertNotContains(requisition_detail, f"/production/requisitions/{requisition.id}/edit/")
        self.assertNotContains(requisition_detail, "确认领料出库")
        self.assertNotContains(receipt_detail, f"/production/receipts/{receipt.id}/edit/")
        self.assertNotContains(receipt_detail, "确认生产入库")

        blocked_responses = [
            self.client.get(f"/production/orders/{self.production_order.id}/edit/"),
            self.client.post(f"/production/orders/{self.production_order.id}/cancel/"),
            self.client.post(f"/production/orders/{self.production_order.id}/create-requisition/"),
            self.client.post(f"/production/orders/{self.production_order.id}/create-receipt/"),
            self.client.get(f"/production/requisitions/{requisition.id}/edit/"),
            self.client.post(f"/production/requisitions/{requisition.id}/confirm/"),
            self.client.get(f"/production/receipts/{receipt.id}/edit/"),
            self.client.post(f"/production/receipts/{receipt.id}/confirm/"),
        ]
        self.assertTrue(all(response.status_code == 403 for response in blocked_responses))

    def test_production_receipt_detail_renders_confirm_action(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        receipt = self._production_receipt()

        response = self.client.get(f"/production/receipts/{receipt.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, receipt.production_receipt_no)
        self.assertContains(response, "打印")
        self.assertContains(response, "确认生产入库")
        self.assertContains(response, self.finished.material_code)
        self.assertContains(response, "返回生产单")
        self.assertContains(response, f'href="/production/orders/{self.production_order.id}/"')

    def test_production_receipt_edit_updates_pending_receipt_and_writes_audit_log(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        other_location = WarehouseLocation.objects.create(location_code="B01", location_name="B01")
        receipt = self._production_receipt()
        item = receipt.items.get()

        response = self.client.post(
            f"/production/receipts/{receipt.id}/edit/",
            {
                "receipt_date": timezone.localdate().isoformat(),
                "remark": "调整入库数量",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-receipt_qty": "6",
                "items-0-location": other_location.id,
                "items-0-batch_no": "MANUAL-BA",
                "items-0-quality_status": ProductionReceiptItem.QualityStatus.PENDING,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/receipts/{receipt.id}/")
        receipt.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(receipt.remark, "调整入库数量")
        self.assertEqual(item.receipt_qty, Decimal("6.0000"))
        self.assertEqual(item.location, other_location)
        self.assertEqual(item.batch_no, "MANUAL-BA")
        self.assertEqual(item.quality_status, ProductionReceiptItem.QualityStatus.PENDING)
        audit_log = AuditLog.objects.get(action="production_receipt_update", source_doc_id=receipt.id)
        self.assertEqual(audit_log.before_snapshot["items"][0]["receipt_qty"], "10.0000")
        self.assertEqual(audit_log.after_snapshot["items"][0]["receipt_qty"], "6.0000")

    def test_production_receipt_edit_rejects_received_receipt(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        receipt = self._production_receipt()
        receipt.status = ProductionReceipt.Status.RECEIVED
        receipt.save(update_fields=["status"])

        response = self.client.get(f"/production/receipts/{receipt.id}/edit/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/receipts/{receipt.id}/")

    def test_production_receipt_edit_rejects_qty_over_remaining(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        receipt = self._production_receipt()
        item = receipt.items.get()
        self.production_order.received_qty = Decimal("8.0000")
        self.production_order.save(update_fields=["received_qty"])

        response = self.client.post(
            f"/production/receipts/{receipt.id}/edit/",
            {
                "receipt_date": timezone.localdate().isoformat(),
                "remark": "超量",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": item.id,
                "items-0-receipt_qty": "3",
                "items-0-location": self.location.id,
                "items-0-batch_no": "",
                "items-0-quality_status": ProductionReceiptItem.QualityStatus.QUALIFIED,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "入库数量不能超过生产指令剩余未入库数量")
        item.refresh_from_db()
        self.assertEqual(item.receipt_qty, Decimal("10.0000"))

    def test_production_receipt_print_records_log(self):
        self.client.force_login(self.user)
        receipt = self._production_receipt()

        response = self.client.get(f"/production/receipts/{receipt.id}/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "生产入库单")
        self.assertContains(response, receipt.production_receipt_no)
        self.assertContains(response, self.finished.material_code)
        print_log = PrintLog.objects.get(source_doc_type="production_receipt", source_doc_id=receipt.id)
        self.assertEqual(print_log.template_type, "production_receipt")
        self.assertEqual(print_log.source_doc_no, receipt.production_receipt_no)
        self.assertEqual(print_log.printed_by, self.user)

    def test_production_receipt_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        receipt = self._production_receipt()

        list_response = self.client.get("/production/receipts/")
        response = self.client.get("/production/receipts/export/")
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("入库单号,生产单,入库日期,状态", content)
        self.assertIn(receipt.production_receipt_no, content)
        export_log = ExportLog.objects.get(module="production_receipts")
        self.assertEqual(export_log.row_count, 1)

    def test_production_receipt_list_filter_and_export_share_query(self):
        self.client.force_login(self.user)
        receipt = self._production_receipt()
        receipt.production_receipt_no = "PI-FILTER-KEEP"
        receipt.status = ProductionReceipt.Status.RECEIVED
        receipt.save(update_fields=["production_receipt_no", "status"])
        ProductionReceipt.objects.create(
            production_receipt_no="PI-FILTER-HIDE",
            production_order=self.production_order,
            receipt_date=timezone.localdate(),
            status=ProductionReceipt.Status.PENDING_CONFIRM,
        )

        list_response = self.client.get("/production/receipts/?q=KEEP&status=received")
        export_response = self.client.get("/production/receipts/export/?q=KEEP&status=received")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "PI-FILTER-KEEP")
        self.assertNotContains(list_response, "PI-FILTER-HIDE")
        self.assertContains(list_response, "/production/receipts/export/?q=KEEP&amp;status=received")
        self.assertIn("PI-FILTER-KEEP", content)
        self.assertNotIn("PI-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="production_receipts")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "received")

    def test_production_receipt_confirm_view_increases_finished_inventory(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        receipt = self._production_receipt()

        response = self.client.post(
            f"/production/receipts/{receipt.id}/confirm/",
            {"current_password": "x"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/production/receipts/{receipt.id}/")
        receipt.refresh_from_db()
        self.production_order.refresh_from_db()
        self.sales_item.refresh_from_db()
        finished_inventory = Inventory.objects.get(material=self.finished, location=self.location)
        receipt_item = receipt.items.get()
        transaction_row = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.PRODUCTION_RECEIPT)
        self.assertEqual(receipt.status, ProductionReceipt.Status.RECEIVED)
        self.assertEqual(receipt_item.batch.material, self.finished)
        self.assertEqual(self.production_order.status, ProductionOrder.Status.COMPLETED)
        self.assertEqual(self.production_order.received_qty, Decimal("10.0000"))
        self.assertEqual(finished_inventory.qty, Decimal("10.0000"))
        self.assertEqual(self.sales_item.inventory_check_status, SalesOrderItem.InventoryCheckStatus.SUFFICIENT)
        self.assertEqual(self.sales_item.line_status, SalesOrderItem.LineStatus.CONFIRMED)
        self.assertEqual(transaction_row.qty_delta, Decimal("10.0000"))


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
