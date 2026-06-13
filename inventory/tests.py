from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from files.models import ExportLog, ImportJob, InitializationJob, PrintLog
from inventory.models import Inventory, InventoryBatch, InventoryTransaction, LocationTransfer, StockCount, StockCountItem, WarehouseLocation
from inventory.services import confirm_location_transfer, confirm_stock_count_adjustment, create_stock_count_from_batches
from masterdata.models import Material
from system.models import AuditLog, PendingEvent


class StockCountAdjustmentServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="stock", password="x")
        self.location = WarehouseLocation.objects.create(location_code="A01", location_name="A01")
        self.to_location = WarehouseLocation.objects.create(location_code="B01", location_name="B01")
        self.material = Material.objects.create(
            material_code="RM001",
            material_name="原料 1",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
            qty_precision=0,
        )
        self._grant_permission(PermissionCode.INVENTORY_VIEW)

    def _grant_permission(self, permission_code: str):
        permission_type = Permission.PermissionType.MODULE if permission_code == PermissionCode.INVENTORY_VIEW else Permission.PermissionType.ACTION
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_code,
                "permission_type": permission_type,
            },
        )
        role = Role.objects.create(role_code=f"inventory-role-{permission_code}-{self.user.id}", role_name=permission_code)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role

    def _stock_count(self):
        return StockCount.objects.create(
            stock_count_no="SC001",
            scope_type="material",
            scope_value=self.material.material_code,
            snapshot_at=timezone.now(),
            status=StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT,
            created_by=self.user,
        )

    def _batch_and_inventory(self, qty=Decimal("10.0000")):
        batch = InventoryBatch.objects.create(
            batch_no="B001",
            material=self.material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=qty,
            remaining_qty=qty,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=self.material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=qty,
        )
        return batch

    def _location_transfer(self):
        batch = self._batch_and_inventory()
        transfer = LocationTransfer.objects.create(
            transfer_no="LT001",
            material=self.material,
            batch=batch,
            from_location=self.location,
            to_location=self.to_location,
            transfer_qty=Decimal("4.0000"),
            status=LocationTransfer.TransferStatus.DRAFT,
        )
        return transfer, batch

    def test_confirm_stock_count_adjustment_creates_gain_batch(self):
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            batch=None,
            book_qty=Decimal("0.0000"),
            counted_qty=Decimal("5.0000"),
        )

        result = confirm_stock_count_adjustment(stock_count.id, self.user.id, "count-1")

        self.assertTrue(result.success)
        stock_count.refresh_from_db()
        batch = InventoryBatch.objects.get()
        inventory = Inventory.objects.get(material=self.material, location=self.location)
        transaction = InventoryTransaction.objects.get()
        self.assertEqual(stock_count.status, StockCount.CountStatus.ADJUSTED)
        self.assertEqual(batch.remaining_qty, Decimal("5.0000"))
        self.assertEqual(inventory.qty, Decimal("5.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("5.0000"))
        self.assertEqual(transaction.transaction_type, InventoryTransaction.TransactionType.STOCK_ADJUSTMENT)
        self.assertTrue(PendingEvent.objects.filter(event_type="stock_count_adjusted").exists())

    def test_confirm_stock_count_adjustment_deducts_loss_from_batch(self):
        batch = self._batch_and_inventory()
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            batch=batch,
            book_qty=Decimal("10.0000"),
            counted_qty=Decimal("7.0000"),
        )

        result = confirm_stock_count_adjustment(stock_count.id, self.user.id, "count-2")

        self.assertTrue(result.success)
        stock_count.refresh_from_db()
        batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.material, location=self.location)
        transaction = InventoryTransaction.objects.get()
        self.assertEqual(stock_count.status, StockCount.CountStatus.ADJUSTED)
        self.assertEqual(batch.remaining_qty, Decimal("7.0000"))
        self.assertEqual(inventory.qty, Decimal("7.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("-3.0000"))
        self.assertEqual(transaction.transaction_type, InventoryTransaction.TransactionType.STOCK_ADJUSTMENT)

    def test_create_stock_count_from_batches_snapshots_current_batches(self):
        batch = self._batch_and_inventory()

        result = create_stock_count_from_batches(self.user.id, location_id=self.location.id)

        self.assertTrue(result.success)
        stock_count = StockCount.objects.get(id=result.data["stock_count_id"])
        item = StockCountItem.objects.get(stock_count=stock_count)
        self.assertEqual(stock_count.status, StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT)
        self.assertEqual(stock_count.created_by, self.user)
        self.assertEqual(item.batch, batch)
        self.assertEqual(item.book_qty, Decimal("10.0000"))
        self.assertEqual(item.counted_qty, Decimal("10.0000"))

    def test_confirm_location_transfer_moves_inventory_to_target_location(self):
        transfer, batch = self._location_transfer()

        result = confirm_location_transfer(transfer.id, self.user.id, "transfer-1")

        self.assertTrue(result.success)
        transfer.refresh_from_db()
        batch.refresh_from_db()
        source_inventory = Inventory.objects.get(material=self.material, location=self.location)
        target_inventory = Inventory.objects.get(material=self.material, location=self.to_location)
        transaction = InventoryTransaction.objects.get(transaction_type=InventoryTransaction.TransactionType.LOCATION_TRANSFER)
        target_batch = InventoryBatch.objects.get(id=result.data["target_batch_id"])
        self.assertEqual(transfer.status, LocationTransfer.TransferStatus.CONFIRMED)
        self.assertEqual(batch.remaining_qty, Decimal("6.0000"))
        self.assertEqual(source_inventory.qty, Decimal("6.0000"))
        self.assertEqual(target_inventory.qty, Decimal("4.0000"))
        self.assertEqual(target_batch.location, self.to_location)
        self.assertEqual(target_batch.remaining_qty, Decimal("4.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("4.0000"))

    def test_stock_count_detail_renders_confirm_action(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            book_qty=Decimal("0.0000"),
            counted_qty=Decimal("5.0000"),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:stock_count_detail", kwargs={"pk": stock_count.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, stock_count.stock_count_no)
        self.assertContains(response, "确认调整库存")
        self.assertContains(response, self.material.material_code)
        self.assertContains(response, 'name="source_doc_type" value="stock_count"', html=False)
        self.assertContains(response, f'name="source_doc_id" value="{stock_count.id}"', html=False)
        self.assertContains(response, f'name="source_doc_no" value="{stock_count.stock_count_no}"', html=False)

    def test_stock_count_print_writes_log(self):
        self.client.force_login(self.user)
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            book_qty=Decimal("0.0000"),
            counted_qty=Decimal("5.0000"),
            difference_qty=Decimal("5.0000"),
            difference_reason="盘盈",
        )

        response = self.client.get(reverse("inventory:stock_count_print", kwargs={"pk": stock_count.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, stock_count.stock_count_no)
        self.assertContains(response, "盘点单")
        self.assertContains(response, self.material.material_code)
        print_log = PrintLog.objects.get(source_doc_type="stock_count", source_doc_id=stock_count.id)
        self.assertEqual(print_log.template_type, "stock_count")
        self.assertEqual(print_log.source_doc_no, stock_count.stock_count_no)

    def test_stock_count_confirm_view_applies_adjustment(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        batch = self._batch_and_inventory()
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            batch=batch,
            book_qty=Decimal("10.0000"),
            counted_qty=Decimal("7.0000"),
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("inventory:stock_count_confirm", kwargs={"pk": stock_count.pk}),
            {"current_password": "x", "adjust_reason": "测试调整"},
        )

        self.assertRedirects(response, reverse("inventory:stock_count_detail", kwargs={"pk": stock_count.pk}))
        stock_count.refresh_from_db()
        batch.refresh_from_db()
        inventory = Inventory.objects.get(material=self.material, location=self.location)
        transaction = InventoryTransaction.objects.get(source_doc_type="stock_count", source_doc_id=stock_count.id)
        self.assertEqual(stock_count.status, StockCount.CountStatus.ADJUSTED)
        self.assertEqual(batch.remaining_qty, Decimal("7.0000"))
        self.assertEqual(inventory.qty, Decimal("7.0000"))
        self.assertEqual(transaction.qty_delta, Decimal("-3.0000"))
        audit_log = AuditLog.objects.get(action="stock_count_confirm_adjustment", source_doc_id=stock_count.id)
        self.assertEqual(audit_log.after_snapshot["operation_reason"], "测试调整")

    def test_stock_count_confirm_requires_adjust_reason(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        batch = self._batch_and_inventory()
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            batch=batch,
            book_qty=Decimal("10.0000"),
            counted_qty=Decimal("7.0000"),
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("inventory:stock_count_confirm", kwargs={"pk": stock_count.pk}),
            {"current_password": "x", "adjust_reason": ""},
            follow=True,
        )

        stock_count.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(stock_count.status, StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT)
        self.assertEqual(batch.remaining_qty, Decimal("10.0000"))
        self.assertContains(response, "请填写盘点调整原因")
        self.assertFalse(AuditLog.objects.filter(action="stock_count_confirm_adjustment", source_doc_id=stock_count.id).exists())

    def test_stock_count_confirm_requires_inventory_process_permission(self):
        batch = self._batch_and_inventory()
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            batch=batch,
            book_qty=Decimal("10.0000"),
            counted_qty=Decimal("7.0000"),
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse("inventory:stock_count_confirm", kwargs={"pk": stock_count.pk}))

        self.assertEqual(response.status_code, 403)
        stock_count.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(stock_count.status, StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT)
        self.assertEqual(batch.remaining_qty, Decimal("10.0000"))

    def test_stock_count_confirm_requires_second_verify_password(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        batch = self._batch_and_inventory()
        stock_count = self._stock_count()
        StockCountItem.objects.create(
            stock_count=stock_count,
            material=self.material,
            location=self.location,
            batch=batch,
            book_qty=Decimal("10.0000"),
            counted_qty=Decimal("7.0000"),
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("inventory:stock_count_confirm", kwargs={"pk": stock_count.pk}),
            {"current_password": "wrong-password"},
            follow=True,
        )

        stock_count.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(stock_count.status, StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT)
        self.assertEqual(batch.remaining_qty, Decimal("10.0000"))
        self.assertContains(response, "二次验证失败")
        self.assertFalse(InventoryTransaction.objects.filter(source_doc_type="stock_count", source_doc_id=stock_count.id).exists())

    def test_inventory_process_actions_require_inventory_process_permission(self):
        batch = self._batch_and_inventory()
        stock_count = self._stock_count()
        self.client.force_login(self.user)

        detail_response = self.client.get(reverse("inventory:stock_count_detail", kwargs={"pk": stock_count.pk}))
        self.assertNotContains(detail_response, "新增盘点明细")

        blocked_responses = [
            self.client.get(reverse("inventory:location_transfer_create")),
            self.client.post(
                reverse("inventory:location_transfer_create"),
                {"batch": batch.id, "to_location": self.to_location.id, "transfer_qty": "1"},
            ),
            self.client.get(reverse("inventory:stock_count_create")),
            self.client.post(
                reverse("inventory:stock_count_create"),
                {"scope_type": "batch", "scope_value": "", "location": self.location.id},
            ),
            self.client.post(
                reverse("inventory:stock_count_item_create", kwargs={"pk": stock_count.pk}),
                {
                    "material": self.material.id,
                    "location": self.location.id,
                    "batch": "",
                    "book_qty": "0",
                    "counted_qty": "1",
                    "difference_reason": "no permission",
                },
            ),
        ]
        self.assertTrue(all(response.status_code == 403 for response in blocked_responses))
        self.assertFalse(LocationTransfer.objects.exists())
        self.assertEqual(StockCount.objects.count(), 1)
        self.assertFalse(StockCountItem.objects.exists())

    def test_stock_count_create_view_snapshots_inventory(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        batch = self._batch_and_inventory()

        response = self.client.post(
            reverse("inventory:stock_count_create"),
            {
                "scope_type": "batch",
                "scope_value": "",
                "location": self.location.id,
            },
        )

        stock_count = StockCount.objects.get()
        item = StockCountItem.objects.get(stock_count=stock_count)
        self.assertRedirects(response, reverse("inventory:stock_count_detail", kwargs={"pk": stock_count.pk}))
        self.assertEqual(item.batch, batch)
        self.assertEqual(item.book_qty, Decimal("10.0000"))

    def test_stock_count_detail_adds_manual_gain_item(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        stock_count = self._stock_count()

        response = self.client.post(
            reverse("inventory:stock_count_item_create", kwargs={"pk": stock_count.pk}),
            {
                "material": self.material.id,
                "location": self.location.id,
                "batch": "",
                "book_qty": "0",
                "counted_qty": "3",
                "difference_reason": "发现未入账库存",
            },
        )

        self.assertRedirects(response, reverse("inventory:stock_count_detail", kwargs={"pk": stock_count.pk}))
        item = StockCountItem.objects.get(stock_count=stock_count)
        self.assertIsNone(item.batch)
        self.assertEqual(item.book_qty, Decimal("0"))
        self.assertEqual(item.counted_qty, Decimal("3"))
        self.assertEqual(item.difference_qty, Decimal("3"))
        self.assertEqual(item.difference_reason, "发现未入账库存")

    def test_location_transfer_create_and_confirm_views(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        batch = self._batch_and_inventory()

        create_response = self.client.post(
            reverse("inventory:location_transfer_create"),
            {
                "batch": batch.id,
                "to_location": self.to_location.id,
                "transfer_qty": "4",
            },
        )

        transfer = LocationTransfer.objects.get()
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(create_response["Location"], reverse("inventory:location_transfer_detail", kwargs={"pk": transfer.pk}))
        self.assertEqual(transfer.material, self.material)
        self.assertEqual(transfer.from_location, self.location)

        page_response = self.client.get(reverse("inventory:location_transfer_detail", kwargs={"pk": transfer.pk}))
        self.assertContains(page_response, "确认移库")
        self.assertContains(page_response, 'name="source_doc_type" value="location_transfer"', html=False)
        self.assertContains(page_response, f'name="source_doc_id" value="{transfer.id}"', html=False)
        self.assertContains(page_response, f'name="source_doc_no" value="{transfer.transfer_no}"', html=False)

        confirm_response = self.client.post(
            reverse("inventory:location_transfer_confirm", kwargs={"pk": transfer.pk}),
            {"current_password": "x"},
        )

        self.assertRedirects(confirm_response, reverse("inventory:location_transfer_detail", kwargs={"pk": transfer.pk}))
        transfer.refresh_from_db()
        source_inventory = Inventory.objects.get(material=self.material, location=self.location)
        target_inventory = Inventory.objects.get(material=self.material, location=self.to_location)
        self.assertEqual(transfer.status, LocationTransfer.TransferStatus.CONFIRMED)
        self.assertEqual(source_inventory.qty, Decimal("6.0000"))
        self.assertEqual(target_inventory.qty, Decimal("4.0000"))

    def test_location_transfer_print_writes_log(self):
        self.client.force_login(self.user)
        transfer, batch = self._location_transfer()

        response = self.client.get(reverse("inventory:location_transfer_print", kwargs={"pk": transfer.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, transfer.transfer_no)
        self.assertContains(response, "库位移库单")
        self.assertContains(response, self.material.material_code)
        self.assertContains(response, self.to_location.location_code)
        print_log = PrintLog.objects.get(source_doc_type="location_transfer", source_doc_id=transfer.id)
        self.assertEqual(print_log.template_type, "location_transfer")
        self.assertEqual(print_log.source_doc_no, transfer.transfer_no)

    def test_inventory_export_creates_csv_and_log(self):
        self._batch_and_inventory()
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:inventory_export"))
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("物料,库位,库存类型,数量", content)
        self.assertIn(self.material.material_code, content)
        export_log = ExportLog.objects.get(module="inventory")
        self.assertEqual(export_log.row_count, 1)

    def test_inventory_list_filter_and_export_share_query(self):
        self._batch_and_inventory()
        other_material = Material.objects.create(
            material_code="RM-HIDE",
            material_name="隐藏原料",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
        )
        Inventory.objects.create(
            material=other_material,
            location=self.to_location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("5.0000"),
        )
        self.client.force_login(self.user)

        list_response = self.client.get(reverse("inventory:inventory_list") + "?q=RM001")
        export_response = self.client.get(reverse("inventory:inventory_export") + "?q=RM001")
        content = _streaming_text(export_response)

        self.assertContains(list_response, self.material.material_code)
        self.assertNotContains(list_response, "RM-HIDE")
        self.assertContains(list_response, reverse("inventory:inventory_export") + "?q=RM001")
        self.assertIn(self.material.material_code, content)
        self.assertNotIn("RM-HIDE", content)
        export_log = ExportLog.objects.get(module="inventory")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "RM001")

    def test_inventory_list_and_detail_render_batches_and_transactions(self):
        batch = self._batch_and_inventory()
        inventory = Inventory.objects.get(
            material=self.material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        )
        transaction = InventoryTransaction.objects.create(
            transaction_no="IT-SUMMARY",
            transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK,
            material=self.material,
            batch=batch,
            location=self.location,
            qty_delta=Decimal("10.0000"),
            source_doc_type="initial_inventory_import",
            source_doc_id=1,
            source_doc_no="INI-SUMMARY",
            created_by=self.user,
        )
        self.client.force_login(self.user)

        list_response = self.client.get(reverse("inventory:inventory_list"))
        detail_response = self.client.get(reverse("inventory:inventory_detail", kwargs={"pk": inventory.pk}))

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, self.material.material_code)
        self.assertContains(list_response, reverse("inventory:inventory_detail", kwargs={"pk": inventory.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, batch.batch_no)
        self.assertContains(detail_response, transaction.transaction_no)
        self.assertContains(detail_response, reverse("inventory:inventory_batch_detail", kwargs={"pk": batch.pk}))
        self.assertContains(detail_response, reverse("inventory:inventory_transaction_detail", kwargs={"pk": transaction.pk}))

    def test_inventory_batch_list_and_detail_render_transactions(self):
        batch = self._batch_and_inventory()
        InventoryTransaction.objects.create(
            transaction_no="IT-BATCH",
            transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK,
            material=self.material,
            batch=batch,
            location=self.location,
            qty_delta=Decimal("10.0000"),
            source_doc_type="initial_inventory_import",
            source_doc_id=1,
            source_doc_no="INI001",
            created_by=self.user,
        )
        self.client.force_login(self.user)

        list_response = self.client.get(reverse("inventory:inventory_batch_list"))
        detail_response = self.client.get(reverse("inventory:inventory_batch_detail", kwargs={"pk": batch.pk}))

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, batch.batch_no)
        self.assertContains(list_response, reverse("inventory:inventory_batch_detail", kwargs={"pk": batch.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, self.material.material_code)
        self.assertContains(detail_response, self.location.location_code)
        self.assertContains(detail_response, "IT-BATCH")
        self.assertContains(detail_response, "INI001")

    def test_inventory_transaction_list_and_detail_render(self):
        batch = self._batch_and_inventory()
        inventory = Inventory.objects.get(
            material=self.material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
        )
        transaction = InventoryTransaction.objects.create(
            transaction_no="IT-DETAIL",
            transaction_type=InventoryTransaction.TransactionType.STOCK_ADJUSTMENT,
            material=self.material,
            batch=batch,
            location=self.location,
            qty_delta=Decimal("-2.0000"),
            source_doc_type="stock_count",
            source_doc_id=7,
            source_doc_no="SC007",
            created_by=self.user,
        )
        self.client.force_login(self.user)

        list_response = self.client.get(reverse("inventory:inventory_transaction_list"))
        detail_response = self.client.get(reverse("inventory:inventory_transaction_detail", kwargs={"pk": transaction.pk}))

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, transaction.transaction_no)
        self.assertContains(list_response, reverse("inventory:inventory_transaction_detail", kwargs={"pk": transaction.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "SC007")
        self.assertContains(detail_response, self.material.material_code)
        self.assertContains(detail_response, batch.batch_no)
        self.assertContains(detail_response, reverse("inventory:inventory_batch_detail", kwargs={"pk": batch.pk}))
        self.assertContains(detail_response, reverse("inventory:inventory_detail", kwargs={"pk": inventory.pk}))

    def test_warehouse_location_list_and_detail_render(self):
        self.client.force_login(self.user)

        list_response = self.client.get(reverse("inventory:warehouse_location_list"))
        detail_response = self.client.get(reverse("inventory:warehouse_location_detail", kwargs={"pk": self.location.pk}))

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, self.location.location_code)
        self.assertContains(list_response, reverse("inventory:warehouse_location_detail", kwargs={"pk": self.location.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, self.location.location_name)

    def test_inventory_process_list_actions_follow_permission(self):
        self.client.force_login(self.user)

        location_response = self.client.get(reverse("inventory:warehouse_location_list"))
        inventory_response = self.client.get(reverse("inventory:inventory_list"))
        transfer_response = self.client.get(reverse("inventory:location_transfer_list"))
        stock_count_response = self.client.get(reverse("inventory:stock_count_list"))

        self.assertContains(location_response, reverse("inventory:warehouse_location_export"))
        self.assertNotContains(location_response, reverse("inventory:warehouse_location_create"))
        self.assertNotContains(location_response, reverse("inventory:warehouse_location_import_template"))
        self.assertNotContains(location_response, reverse("inventory:warehouse_location_import"))
        self.assertContains(inventory_response, reverse("inventory:inventory_export"))
        self.assertNotContains(inventory_response, reverse("inventory:initial_inventory_import_template"))
        self.assertNotContains(inventory_response, reverse("inventory:initial_inventory_import"))
        self.assertNotContains(transfer_response, reverse("inventory:location_transfer_create"))
        self.assertNotContains(stock_count_response, reverse("inventory:stock_count_create"))

        self._grant_permission(PermissionCode.INVENTORY_PROCESS)

        location_response = self.client.get(reverse("inventory:warehouse_location_list"))
        inventory_response = self.client.get(reverse("inventory:inventory_list"))
        transfer_response = self.client.get(reverse("inventory:location_transfer_list"))
        stock_count_response = self.client.get(reverse("inventory:stock_count_list"))

        self.assertContains(location_response, reverse("inventory:warehouse_location_create"))
        self.assertContains(location_response, reverse("inventory:warehouse_location_import_template"))
        self.assertContains(location_response, reverse("inventory:warehouse_location_import"))
        self.assertContains(inventory_response, reverse("inventory:initial_inventory_import_template"))
        self.assertContains(inventory_response, reverse("inventory:initial_inventory_import"))
        self.assertContains(transfer_response, reverse("inventory:location_transfer_create"))
        self.assertContains(stock_count_response, reverse("inventory:stock_count_create"))

    def test_warehouse_location_list_filter_and_export_share_query(self):
        WarehouseLocation.objects.create(
            location_code="C02",
            location_name="库位 C02",
            status=WarehouseLocation.LocationStatus.INACTIVE,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:warehouse_location_list") + "?q=C02&status=inactive")
        export_response = self.client.get(reverse("inventory:warehouse_location_export") + "?q=C02&status=inactive")
        content = _streaming_text(export_response)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "C02")
        self.assertContains(response, reverse("inventory:warehouse_location_export") + "?q=C02&amp;status=inactive")
        self.assertNotContains(response, self.location.location_code)
        self.assertIn("C02", content)
        self.assertNotIn(self.location.location_code, content)
        export_log = ExportLog.objects.get(module="warehouse_locations")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "C02")
        self.assertEqual(export_log.filter_json["query"]["status"], "inactive")

    def test_warehouse_location_create_and_edit_require_inventory_process_permission(self):
        self.client.force_login(self.user)

        create_response = self.client.get(reverse("inventory:warehouse_location_create"))
        edit_response = self.client.get(reverse("inventory:warehouse_location_edit", kwargs={"pk": self.location.pk}))

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(edit_response.status_code, 403)

    def test_warehouse_location_create_view_creates_location(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("inventory:warehouse_location_create"),
            {
                "location_code": "C02",
                "location_name": "库位 C02",
                "status": WarehouseLocation.LocationStatus.ACTIVE,
                "remark": "页面创建",
            },
        )

        location = WarehouseLocation.objects.get(location_code="C02")
        self.assertRedirects(response, reverse("inventory:warehouse_location_detail", kwargs={"pk": location.pk}))
        self.assertEqual(location.location_name, "库位 C02")

    def test_warehouse_location_edit_view_updates_location(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("inventory:warehouse_location_edit", kwargs={"pk": self.location.pk}),
            {
                "location_code": self.location.location_code,
                "location_name": "库位 A01 改",
                "status": WarehouseLocation.LocationStatus.INACTIVE,
                "remark": "页面编辑",
            },
        )

        self.assertRedirects(response, reverse("inventory:warehouse_location_detail", kwargs={"pk": self.location.pk}))
        self.location.refresh_from_db()
        self.assertEqual(self.location.location_name, "库位 A01 改")
        self.assertEqual(self.location.status, WarehouseLocation.LocationStatus.INACTIVE)

    def test_warehouse_location_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:warehouse_location_import_template"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("库位编码,库位名称,状态,备注", content)
        self.assertIn("A01", content)

    def test_warehouse_location_import_requires_inventory_process_permission(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:warehouse_location_import_template"))

        self.assertEqual(response.status_code, 403)

    def test_warehouse_location_import_creates_locations_and_job(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "warehouse_locations.csv",
            (
                "库位编码,库位名称,状态,备注\n"
                "C01,库位 C01,active,新库位\n"
                "D01,库位 D01,inactive,停用库位\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:warehouse_location_import"), {"import_file": upload})

        self.assertRedirects(response, reverse("inventory:warehouse_location_list"))
        self.assertTrue(WarehouseLocation.objects.filter(location_code="C01", location_name="库位 C01").exists())
        self.assertTrue(WarehouseLocation.objects.filter(location_code="D01", status=WarehouseLocation.LocationStatus.INACTIVE).exists())
        job = ImportJob.objects.get(template_type="warehouse_locations")
        self.assertEqual(job.status, ImportJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 2)
        self.assertEqual(job.created_by, self.user)

    def test_warehouse_location_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "warehouse_locations.csv",
            (
                "库位编码,库位名称,状态,备注\n"
                "A01,重复库位,bad_status,重复\n"
                "E01,,active,缺名称\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:warehouse_location_import"), {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "库位编码已存在")
        self.assertContains(response, "状态不合法")
        self.assertContains(response, "必填字段不能为空")
        job = ImportJob.objects.get(template_type="warehouse_locations")
        self.assertEqual(job.status, ImportJob.JobStatus.FAILED)
        self.assertGreater(job.failed_count, 0)

    @override_settings(ERP_MAX_CSV_IMPORT_SIZE=16)
    def test_warehouse_location_import_rejects_oversized_csv_before_creating_job(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "warehouse_locations.csv",
            "库位编码,库位名称,状态,备注\nC99,big,active,\n".encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:warehouse_location_import"), {"import_file": upload})

        self.assertRedirects(response, reverse("inventory:warehouse_location_import"))
        self.assertFalse(ImportJob.objects.filter(template_type="warehouse_locations").exists())

    def test_initial_inventory_import_template_downloads_csv(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:initial_inventory_import_template"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("物料编码,库位编码,批次号,库存类型,期初数量", content)
        self.assertIn("OPEN-RM001-A01-001", content)

    def test_initial_inventory_import_template_requires_inventory_process_permission(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:initial_inventory_import_template"))

        self.assertEqual(response.status_code, 403)

    def test_initial_inventory_import_previews_then_confirms_batches_inventory_and_transactions(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "initial_inventory.csv",
            (
                "物料编码,库位编码,批次号,库存类型,期初数量,成本单价,入库时间\n"
                "RM001,A01,OPEN-RM001-A01,available,12.5000,3.210000,2026-06-09\n"
                "RM001,B01,,sample,2.0000,,2026-06-09T09:30:00\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:initial_inventory_import"), {"import_file": upload})

        job = InitializationJob.objects.get(template_type="initial_inventory")
        self.assertRedirects(response, reverse("files:initialization_job_detail", kwargs={"pk": job.id}))
        self.assertEqual(job.status, InitializationJob.JobStatus.PENDING_CONFIRM)
        self.assertEqual(job.success_count, 2)
        self.assertEqual(len(job.error_summary["preview_rows"]), 2)
        self.assertFalse(InventoryBatch.objects.filter(batch_no="OPEN-RM001-A01").exists())
        self.assertFalse(InventoryTransaction.objects.filter(transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK).exists())
        self.assertTrue(AuditLog.objects.filter(action="initial_inventory_preview", source_doc_id=job.id).exists())

        detail_response = self.client.get(reverse("files:initialization_job_detail", kwargs={"pk": job.id}))
        self.assertContains(detail_response, "确认入账")
        self.assertContains(detail_response, "OPEN-RM001-A01")

        confirm_response = self.client.post(
            reverse("inventory:initial_inventory_confirm", kwargs={"pk": job.id}),
            {"current_password": "x"},
        )

        self.assertRedirects(confirm_response, reverse("files:initialization_job_detail", kwargs={"pk": job.id}))
        job = InitializationJob.objects.get(template_type="initial_inventory")
        self.assertEqual(job.status, InitializationJob.JobStatus.SUCCESS)
        self.assertEqual(job.success_count, 2)
        batch = InventoryBatch.objects.get(batch_no="OPEN-RM001-A01")
        self.assertEqual(batch.material, self.material)
        self.assertEqual(batch.location, self.location)
        self.assertEqual(batch.remaining_qty, Decimal("12.5000"))
        self.assertEqual(batch.cost_price, Decimal("3.210000"))
        self.assertEqual(
            Inventory.objects.get(material=self.material, location=self.location, inventory_type=InventoryBatch.InventoryType.AVAILABLE).qty,
            Decimal("12.5000"),
        )
        self.assertEqual(
            Inventory.objects.get(material=self.material, location=self.to_location, inventory_type=InventoryBatch.InventoryType.SAMPLE).qty,
            Decimal("2.0000"),
        )
        self.assertEqual(
            InventoryTransaction.objects.filter(transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK).count(),
            2,
        )
        transaction = InventoryTransaction.objects.get(batch=batch)
        self.assertEqual(transaction.qty_delta, Decimal("12.5000"))
        self.assertEqual(transaction.source_doc_type, "initial_inventory_import")
        self.assertEqual(transaction.source_doc_id, job.id)
        self.assertTrue(AuditLog.objects.filter(action="initial_inventory_import", source_doc_id=job.id).exists())

    @override_settings(ERP_MAX_CSV_IMPORT_SIZE=16)
    def test_initial_inventory_import_rejects_oversized_csv_before_creating_job(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "initial_inventory.csv",
            "物料编码,库位编码,批次号,库存类型,期初数量\nRM001,A01,BIG,available,1\n".encode("utf-8-sig"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:initial_inventory_import"), {"import_file": upload})

        self.assertRedirects(response, reverse("inventory:initial_inventory_import"))
        self.assertFalse(InitializationJob.objects.filter(template_type="initial_inventory").exists())

    def test_initial_inventory_import_success_can_be_cancelled_before_use(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "initial_inventory.csv",
            (
                "物料编码,库位编码,批次号,库存类型,期初数量,成本单价,入库时间\n"
                "RM001,A01,OPEN-CANCEL,available,5.0000,1.000000,2026-06-09\n"
            ).encode("utf-8-sig"),
            content_type="text/csv",
        )
        self.client.post(reverse("inventory:initial_inventory_import"), {"import_file": upload})
        job = InitializationJob.objects.get(template_type="initial_inventory")
        self.client.post(
            reverse("inventory:initial_inventory_confirm", kwargs={"pk": job.id}),
            {"current_password": "x"},
        )

        response = self.client.post(
            reverse("inventory:initial_inventory_cancel", kwargs={"pk": job.id}),
            {"current_password": "x", "cancel_reason": "测试撤销"},
        )

        self.assertRedirects(response, reverse("files:initialization_job_detail", kwargs={"pk": job.id}))
        job.refresh_from_db()
        self.assertEqual(job.status, InitializationJob.JobStatus.CANCELLED)
        batch = InventoryBatch.objects.get(batch_no="OPEN-CANCEL")
        self.assertEqual(batch.remaining_qty, Decimal("0.0000"))
        self.assertEqual(batch.batch_status, InventoryBatch.BatchStatus.VOIDED)
        inventory = Inventory.objects.get(material=self.material, location=self.location, inventory_type=InventoryBatch.InventoryType.AVAILABLE)
        self.assertEqual(inventory.qty, Decimal("0.0000"))
        reverse_transaction = InventoryTransaction.objects.get(source_doc_type="initial_inventory_cancel", source_doc_id=job.id)
        self.assertEqual(reverse_transaction.qty_delta, Decimal("-5.0000"))
        self.assertTrue(AuditLog.objects.filter(action="initial_inventory_cancel", source_doc_id=job.id).exists())

    def test_initial_inventory_import_reports_validation_errors(self):
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self.client.force_login(self.user)
        self._batch_and_inventory()
        upload = SimpleUploadedFile(
            "initial_inventory.csv",
            (
                "物料编码,库位编码,批次号,库存类型,期初数量,成本单价,入库时间\n"
                "RM-MISSING,A01,B001,bad_type,0,-1,bad-date\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:initial_inventory_import"), {"import_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "物料编码不存在")
        self.assertContains(response, "批次号已存在")
        self.assertContains(response, "库存类型不合法")
        self.assertContains(response, "期初数量必须大于 0")
        self.assertContains(response, "成本单价不能小于 0")
        self.assertContains(response, "入库日期格式不合法")
        job = InitializationJob.objects.get(template_type="initial_inventory")
        self.assertEqual(job.status, InitializationJob.JobStatus.FAILED)
        self.assertFalse(InventoryTransaction.objects.filter(transaction_type=InventoryTransaction.TransactionType.INITIAL_STOCK).exists())

    def test_initial_inventory_import_requires_inventory_process_permission(self):
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "initial_inventory.csv",
            (
                "物料编码,库位编码,批次号,库存类型,期初数量,成本单价,入库时间\n"
                "RM001,A01,OPEN-DENIED,available,1.0000,,2026-06-09\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        response = self.client.post(reverse("inventory:initial_inventory_import"), {"import_file": upload})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(InitializationJob.objects.exists())
        self.assertFalse(InventoryBatch.objects.filter(batch_no="OPEN-DENIED").exists())

    def test_inventory_batch_export_creates_csv_and_log(self):
        self._batch_and_inventory()
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:inventory_batch_export"))
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("批次号,物料,库位,库存类型,剩余数量,状态", content)
        self.assertIn("B001", content)
        export_log = ExportLog.objects.get(module="inventory_batches")
        self.assertEqual(export_log.row_count, 1)

    def test_inventory_batch_list_filter_and_export_share_query(self):
        batch = self._batch_and_inventory()
        batch.batch_no = "B-FILTER-KEEP"
        batch.save(update_fields=["batch_no"])
        InventoryBatch.objects.create(
            batch_no="B-FILTER-HIDE",
            material=self.material,
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at=timezone.now(),
            initial_qty=Decimal("1.0000"),
            remaining_qty=Decimal("0.0000"),
            batch_status=InventoryBatch.BatchStatus.USED_UP,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/inventory/batches/?q=KEEP&status=in_stock")
        export_response = self.client.get("/inventory/batches/export/?q=KEEP&status=in_stock")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "B-FILTER-KEEP")
        self.assertNotContains(list_response, "B-FILTER-HIDE")
        self.assertContains(list_response, "/inventory/batches/export/?q=KEEP&amp;status=in_stock")
        self.assertIn("B-FILTER-KEEP", content)
        self.assertNotIn("B-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="inventory_batches")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["status"], "in_stock")

    def test_inventory_transaction_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        InventoryTransaction.objects.create(
            transaction_no="IT-EXPORT",
            transaction_type=InventoryTransaction.TransactionType.STOCK_ADJUSTMENT,
            material=self.material,
            location=self.location,
            qty_delta=Decimal("3.0000"),
            source_doc_type="manual",
            source_doc_id=1,
            source_doc_no="MANUAL",
            created_by=self.user,
        )

        response = self.client.get(reverse("inventory:inventory_transaction_export"))
        content = _streaming_text(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("流水号,类型,物料,库位,数量变化,创建时间", content)
        self.assertIn("IT-EXPORT", content)
        export_log = ExportLog.objects.get(module="inventory_transactions")
        self.assertEqual(export_log.row_count, 1)

    def test_inventory_transaction_list_filter_and_export_share_query(self):
        batch = self._batch_and_inventory()
        InventoryTransaction.objects.create(
            transaction_no="IT-FILTER-KEEP",
            transaction_type=InventoryTransaction.TransactionType.STOCK_ADJUSTMENT,
            material=self.material,
            batch=batch,
            location=self.location,
            qty_delta=Decimal("3.0000"),
            source_doc_type="stock_count",
            source_doc_id=1,
            source_doc_no="SC-KEEP",
            created_by=self.user,
        )
        InventoryTransaction.objects.create(
            transaction_no="IT-FILTER-HIDE",
            transaction_type=InventoryTransaction.TransactionType.PURCHASE_IN,
            material=self.material,
            batch=batch,
            location=self.location,
            qty_delta=Decimal("5.0000"),
            source_doc_type="purchase_receipt",
            source_doc_id=2,
            source_doc_no="PR-HIDE",
            created_by=self.user,
        )
        self.client.force_login(self.user)

        list_response = self.client.get("/inventory/transactions/?q=KEEP&transaction_type=stock_adjustment")
        export_response = self.client.get("/inventory/transactions/export/?q=KEEP&transaction_type=stock_adjustment")
        content = _streaming_text(export_response)

        self.assertContains(list_response, "IT-FILTER-KEEP")
        self.assertNotContains(list_response, "IT-FILTER-HIDE")
        self.assertContains(
            list_response,
            "/inventory/transactions/export/?q=KEEP&amp;transaction_type=stock_adjustment",
        )
        self.assertIn("IT-FILTER-KEEP", content)
        self.assertNotIn("IT-FILTER-HIDE", content)
        export_log = ExportLog.objects.get(module="inventory_transactions")
        self.assertEqual(export_log.row_count, 1)
        self.assertEqual(export_log.filter_json["query"]["q"], "KEEP")
        self.assertEqual(export_log.filter_json["query"]["transaction_type"], "stock_adjustment")

    def test_location_transfer_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        transfer, batch = self._location_transfer()
        transfer.transfer_no = "LT-EXPORT"
        transfer.save(update_fields=["transfer_no"])

        list_response = self.client.get(reverse("inventory:location_transfer_list"))
        response = self.client.get(reverse("inventory:location_transfer_export"))
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("移库单号,物料,批次,原库位,目标库位,数量,状态", content)
        self.assertIn("LT-EXPORT", content)
        export_log = ExportLog.objects.get(module="location_transfers")
        self.assertEqual(export_log.row_count, 1)

    def test_stock_count_export_creates_csv_and_log(self):
        self.client.force_login(self.user)
        stock_count = self._stock_count()
        stock_count.stock_count_no = "SC-EXPORT"
        stock_count.save(update_fields=["stock_count_no"])

        list_response = self.client.get(reverse("inventory:stock_count_list"))
        response = self.client.get(reverse("inventory:stock_count_export"))
        content = _streaming_text(response)

        self.assertContains(list_response, "导出CSV")
        self.assertEqual(response.status_code, 200)
        self.assertIn("盘点单号,范围类型,范围值,状态,快照时间", content)
        self.assertIn("SC-EXPORT", content)
        export_log = ExportLog.objects.get(module="stock_counts")
        self.assertEqual(export_log.row_count, 1)


def _streaming_text(response) -> str:
    content = b"".join(response.streaming_content).decode("utf-8-sig")
    response.close()
    return content
