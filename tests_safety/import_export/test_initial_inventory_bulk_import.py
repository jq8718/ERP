import csv
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from files.models import InitializationJob
from inventory.import_services import confirm_initial_inventory_import, preview_initial_inventory_from_csv
from inventory.models import Inventory, InventoryBatch, InventoryTransaction, WarehouseLocation
from masterdata.models import Material


User = get_user_model()


class InitialInventoryBulkImportTest(TestCase):
    """Production-like initial inventory import checks."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_user(
            "bulk_import_operator",
            password="Bulk@2026!",
            display_name="Bulk Import Operator",
        )
        cls.location = WarehouseLocation.objects.create(
            location_code="BULK-A01",
            location_name="Bulk Import A01",
        )
        cls.materials = [
            Material.objects.create(
                material_code=f"BULK-M{i:04d}",
                material_name=f"Bulk Material {i:04d}",
                material_type=Material.MaterialType.RAW,
                base_unit="kg",
                qty_precision=3,
                status=Material.MaterialStatus.ACTIVE,
            )
            for i in range(1000)
        ]

    def test_1000_row_initial_inventory_preview_confirm_and_summary_consistency(self):
        csv_file = StringIO()
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "material_code",
                "location_code",
                "batch_no",
                "inventory_type",
                "initial_qty",
                "cost_price",
                "received_at",
            ]
        )
        for index, material in enumerate(self.materials, start=1):
            writer.writerow(
                [
                    material.material_code,
                    self.location.location_code,
                    f"BULK-BATCH-{index:04d}",
                    InventoryBatch.InventoryType.AVAILABLE,
                    "1.2500",
                    "2.500000",
                    "2026-06-01",
                ]
            )
        csv_file.seek(0)

        preview = preview_initial_inventory_from_csv(csv_file, self.operator.id)
        self.assertTrue(preview.success, preview.message)
        self.assertEqual(preview.data["success_count"], 1000)
        self.assertEqual(InventoryBatch.objects.count(), 0)

        confirm = confirm_initial_inventory_import(preview.data["initialization_job_id"], self.operator.id)
        self.assertTrue(confirm.success, confirm.message)
        self.assertEqual(confirm.data["success_count"], 1000)
        self.assertEqual(InventoryBatch.objects.count(), 1000)
        self.assertEqual(InventoryTransaction.objects.count(), 1000)

        total_inventory_qty = sum(
            Inventory.objects.values_list("qty", flat=True),
            Decimal("0"),
        )
        total_batch_qty = sum(
            InventoryBatch.objects.values_list("remaining_qty", flat=True),
            Decimal("0"),
        )
        self.assertEqual(total_inventory_qty, Decimal("1250.0000"))
        self.assertEqual(total_inventory_qty, total_batch_qty)

        job = InitializationJob.objects.get(id=preview.data["initialization_job_id"])
        self.assertEqual(job.status, InitializationJob.JobStatus.SUCCESS)

    def test_initial_inventory_validation_errors_do_not_partially_import(self):
        existing = InventoryBatch.objects.create(
            batch_no="BULK-EXISTING",
            material=self.materials[0],
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            received_at="2026-06-01T00:00:00Z",
            initial_qty=Decimal("1"),
            remaining_qty=Decimal("1"),
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
        )
        Inventory.objects.create(
            material=self.materials[0],
            location=self.location,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            qty=Decimal("1"),
        )
        csv_file = StringIO(
            "material_code,location_code,batch_no,inventory_type,initial_qty,cost_price,received_at\n"
            f"{self.materials[1].material_code},{self.location.location_code},BULK-OK,available,5,1.2,2026-06-01\n"
            f"NO-SUCH-MATERIAL,{self.location.location_code},BULK-BAD-MAT,available,5,1.2,2026-06-01\n"
            f"{self.materials[2].material_code},NO-SUCH-LOC,BULK-BAD-LOC,available,5,1.2,2026-06-01\n"
            f"{self.materials[3].material_code},{self.location.location_code},{existing.batch_no},available,5,1.2,2026-06-01\n"
            f"{self.materials[4].material_code},{self.location.location_code},BULK-BAD-QTY,available,0,1.2,2026-06-01\n"
            f"{self.materials[5].material_code},{self.location.location_code},BULK-BAD-DATE,available,5,1.2,not-a-date\n"
        )

        result = preview_initial_inventory_from_csv(csv_file, self.operator.id)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "FILE_IMPORT_VALIDATION_FAILED")
        self.assertGreaterEqual(len(result.data["errors"]), 5)
        self.assertEqual(InventoryBatch.objects.count(), 1)
        self.assertEqual(Inventory.objects.count(), 1)

    @override_settings(ERP_MAX_CSV_IMPORT_ROWS=2)
    def test_initial_inventory_row_limit_rejects_oversized_template(self):
        csv_file = StringIO(
            "material_code,location_code,batch_no,inventory_type,initial_qty,cost_price,received_at\n"
            f"{self.materials[0].material_code},{self.location.location_code},BULK-LIMIT-1,available,1,1,2026-06-01\n"
            f"{self.materials[1].material_code},{self.location.location_code},BULK-LIMIT-2,available,1,1,2026-06-01\n"
            f"{self.materials[2].material_code},{self.location.location_code},BULK-LIMIT-3,available,1,1,2026-06-01\n"
        )

        result = preview_initial_inventory_from_csv(csv_file, self.operator.id)

        self.assertFalse(result.success)
        self.assertIn("超过 2 行限制", result.message)
        self.assertEqual(InventoryBatch.objects.count(), 0)
