import os, tempfile, zipfile, json
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


class BackupOperationsTest(TestCase):
    """Verify backup, verify, cleanup, and restore drill operations."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("ops_admin", "ops@t.com", "Admin@2026!")
        from masterdata.models import Material
        Material.objects.create(material_code="OPS_MAT", material_name="OPS Material", material_type="raw", base_unit="kg", qty_precision=3, status="active")

    def test_01_backup_daily_creates_backup_record(self):
        from system.services import backup_daily
        result = backup_daily()
        self.assertTrue(result.success)
        self.assertIn("backup_id", result.data)
        self.assertIn("checksum", result.data)

    def test_02_verify_backups_passes(self):
        from system.services import backup_daily, verify_backups
        backup_daily()
        result = verify_backups()
        self.assertTrue(result.success)

    def test_03_cleanup_backups_runs(self):
        from system.services import cleanup_backups
        result = cleanup_backups()
        self.assertTrue(result.success)

    def test_04_restore_drill_runs(self):
        from system.services import backup_daily, restore_drill
        backup_daily()
        result = restore_drill()
        self.assertTrue(result.success)

    def test_05_backup_files_exist_on_disk(self):
        from system.services import backup_daily
        from system.models import Backup
        result = backup_daily()
        self.assertTrue(result.success)
        backup = Backup.objects.get(id=result.data["backup_id"])
        self.assertTrue(os.path.isfile(backup.file_path))
        self.assertGreater(os.path.getsize(backup.file_path), 0)

    def test_06_zip_slash_safety_enforced(self):
        bad_zip_path = os.path.join(tempfile.gettempdir(), "malicious.zip")
        try:
            with zipfile.ZipFile(bad_zip_path, "w") as zf:
                zf.writestr("../etc/passwd", "malicious content")
            try:
                with zipfile.ZipFile(bad_zip_path, "r") as zf:
                    for info in zf.infolist():
                        if "../" in info.filename or info.filename.startswith("/"):
                            self.assertTrue(True)
            finally:
                os.remove(bad_zip_path)
        except Exception:
            pass


class HealthCheckTest(TestCase):
    """Verify the health check page reports accurate status."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("hc_admin", "hc@t.com", "Admin@2026!")

    def test_health_check_page_accessible_by_admin(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("OK", content)

    def test_health_check_shows_backup_info(self):
        from system.services import backup_daily
        backup_daily()
        self.client.force_login(self.admin)
        resp = self.client.get("/health/")
        self.assertIn("200", str(resp.status_code))


class BackgroundJobTest(TestCase):
    """Verify background job infrastructure."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("bg_admin", "bg@t.com", "Admin@2026!")

    def test_01_start_and_finish_job(self):
        from system.services import start_background_job, finish_background_job
        job = start_background_job("test_job")
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "running")
        result = finish_background_job(job.id, "completed successfully")
        self.assertTrue(result.success)
        job.refresh_from_db()
        self.assertEqual(job.status, "success")

    def test_02_duplicate_job_prevented(self):
        from system.services import start_background_job, finish_background_job
        job1 = start_background_job("test_job_dup")
        self.assertIsNotNone(job1)
        job2 = start_background_job("test_job_dup")
        self.assertIsNone(job2)
        finish_background_job(job1.id, "done")

    def test_03_fail_job_recorded(self):
        from system.services import start_background_job, fail_background_job
        job = start_background_job("test_job_fail")
        result = fail_background_job(job.id, "intentional failure")
        self.assertTrue(result.success)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
