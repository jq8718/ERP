from datetime import datetime, timedelta
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from bom.models import Bom
from files.models import Attachment, AttachmentAccessLog, ExportLog, ImportJob, InitializationJob, PrintLog
from files.services import (
    CsvImportReadError,
    _attachment_scan_command,
    csv_import_header_row,
    delete_attachment,
    export_queryset_to_csv,
    read_csv_dict_rows,
    record_attachment_access,
    register_attachment,
    resolve_export_file_path,
    uploaded_csv_text_file,
)
from finance.models import CustomerInvoice, CustomerReceipt, ExpenseRecord, Reconciliation
from inventory.models import StockCount
from masterdata.models import Customer, Material, Supplier
from production.models import ProductionOrder
from purchase.models import PurchaseOrder, PurchaseRequest
from sales.models import SalesOrder


class FileServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="fileuser", password="x")

    def test_register_attachment_rejects_disallowed_extension(self):
        result = register_attachment(
            source_doc_type="sales_order",
            source_doc_id=1,
            original_filename="bad.exe",
            stored_filename="bad.exe",
            file_path="attachments/bad.exe",
            file_size=100,
            mime_type="application/octet-stream",
            uploaded_by_id=self.user.id,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "FILE_TYPE_NOT_ALLOWED")

    def test_register_attachment_rejects_path_outside_attachment_dir(self):
        result = register_attachment(
            source_doc_type="sales_order",
            source_doc_id=1,
            original_filename="contract.pdf",
            stored_filename="contract.pdf",
            file_path="../exports/contract.pdf",
            file_size=100,
            mime_type="application/pdf",
            uploaded_by_id=self.user.id,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "FILE_PATH_INVALID")
        self.assertFalse(Attachment.objects.exists())

    def test_register_access_and_delete_attachment(self):
        result = register_attachment(
            source_doc_type="sales_order",
            source_doc_id=1,
            source_doc_no="SO001",
            original_filename="contract.pdf",
            stored_filename="ATT001.pdf",
            file_path="attachments/ATT001.pdf",
            file_size=100,
            mime_type="application/pdf",
            uploaded_by_id=self.user.id,
            is_sensitive=True,
        )
        self.assertTrue(result.success)
        attachment = Attachment.objects.get()

        access_result = record_attachment_access(attachment.id, self.user.id)
        delete_result = delete_attachment(attachment.id, self.user.id, "录错")

        self.assertTrue(access_result.success)
        self.assertTrue(delete_result.success)
        attachment.refresh_from_db()
        self.assertEqual(attachment.status, Attachment.AttachmentStatus.DELETED)
        self.assertEqual(AttachmentAccessLog.objects.count(), 2)
        self.assertEqual(attachment.scan_status, Attachment.ScanStatus.NOT_REQUIRED)

    def test_register_attachment_marks_scan_passed_when_scanner_succeeds(self):
        with TemporaryDirectory() as temp_dir:
            attachment_dir = Path(temp_dir) / "attachments"
            attachment_dir.mkdir()
            (attachment_dir / "safe.pdf").write_bytes(b"safe")
            scanner_path = Path(temp_dir) / "scan_pass.py"
            scanner_path.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
            command = f"{sys.executable} {scanner_path} --file {{file}}"
            with override_settings(MEDIA_ROOT=temp_dir, ERP_ATTACHMENT_SCAN_COMMAND=command):
                result = register_attachment(
                    source_doc_type="sales_order",
                    source_doc_id=1,
                    original_filename="safe.pdf",
                    stored_filename="safe.pdf",
                    file_path="attachments/safe.pdf",
                    file_size=4,
                    mime_type="application/pdf",
                    uploaded_by_id=self.user.id,
                )

        self.assertTrue(result.success)
        attachment = Attachment.objects.get(original_filename="safe.pdf")
        self.assertEqual(attachment.scan_status, Attachment.ScanStatus.PASSED)

    def test_register_attachment_rejects_file_when_scanner_fails(self):
        with TemporaryDirectory() as temp_dir:
            attachment_dir = Path(temp_dir) / "attachments"
            attachment_dir.mkdir()
            (attachment_dir / "bad.pdf").write_bytes(b"bad")
            scanner_path = Path(temp_dir) / "scan_fail.py"
            scanner_path.write_text("import sys\nprint('FOUND')\nsys.exit(1)\n", encoding="utf-8")
            command = f"{sys.executable} {scanner_path} {{file}}"
            with override_settings(MEDIA_ROOT=temp_dir, ERP_ATTACHMENT_SCAN_COMMAND=command):
                result = register_attachment(
                    source_doc_type="sales_order",
                    source_doc_id=1,
                    original_filename="bad.pdf",
                    stored_filename="bad.pdf",
                    file_path="attachments/bad.pdf",
                    file_size=3,
                    mime_type="application/pdf",
                    uploaded_by_id=self.user.id,
                )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "FILE_SCAN_FAILED")
        self.assertIn("FOUND", result.message)
        self.assertFalse(Attachment.objects.filter(original_filename="bad.pdf").exists())

    def test_attachment_scan_command_strips_windows_executable_quotes(self):
        command = _attachment_scan_command(
            r'"C:\Program Files\Windows Defender\MpCmdRun.exe" -Scan -File {file}',
            Path(r"C:\ERP\data\attachments\safe.pdf"),
        )

        self.assertEqual(command[0], r"C:\Program Files\Windows Defender\MpCmdRun.exe")
        self.assertEqual(command[-1], r"C:\ERP\data\attachments\safe.pdf")

    def test_delete_attachment_rejects_already_deleted_attachment(self):
        attachment = Attachment.objects.create(
            attachment_no="ATT-DELETE-ONCE",
            source_doc_type="sales_order",
            source_doc_id=1,
            original_filename="contract.pdf",
            stored_filename="contract.pdf",
            file_path="attachments/contract.pdf",
            file_size=100,
            uploaded_by=self.user,
        )

        first_result = delete_attachment(attachment.id, self.user.id, "录错")
        second_result = delete_attachment(attachment.id, self.user.id, "再次删除")

        attachment.refresh_from_db()
        self.assertTrue(first_result.success)
        self.assertFalse(second_result.success)
        self.assertEqual(second_result.error_code, "STATE_INVALID_TRANSITION")
        self.assertEqual(attachment.delete_reason, "录错")
        self.assertEqual(AttachmentAccessLog.objects.filter(action="delete").count(), 1)

    def test_export_queryset_to_csv_escapes_formula_like_values(self):
        class Row:
            def __init__(self, material_code, material_name):
                self.material_code = material_code
                self.material_name = material_name

        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir):
                result = export_queryset_to_csv(
                    "materials",
                    [
                        Row("RM001", "=HYPERLINK(\"http://evil.example\")"),
                        Row("RM002", "  +cmd"),
                    ],
                    (("编码", "material_code"), ("名称", "material_name")),
                    self.user.id,
                )

            self.assertTrue(result.success)
            content = Path(result.data["file_path"]).read_text(encoding="utf-8-sig")
            self.assertIn("RM001", content)
            self.assertIn("'=HYPERLINK", content)
            self.assertIn("'  +cmd", content)

    def test_export_queryset_to_csv_stores_unique_file_but_keeps_download_filename(self):
        class Row:
            material_code = "RM001"
            material_name = "原料"

        with TemporaryDirectory() as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir):
                result = export_queryset_to_csv(
                    "materials",
                    [Row()],
                    (("编码", "material_code"), ("名称", "material_name")),
                    self.user.id,
                )

                export_no = result.data["export_no"]
                stored_path = Path(result.data["file_path"])

                self.assertTrue(result.success)
                self.assertTrue(stored_path.name.startswith(f"{export_no}-"))
                self.assertTrue(stored_path.name.endswith(".csv"))
                self.assertEqual(result.data["filename"], f"{export_no}.csv")
                self.assertEqual(resolve_export_file_path(str(stored_path), export_no), stored_path.resolve())

    def test_resolve_export_file_path_accepts_legacy_and_randomized_export_names(self):
        with TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir) / "exports"
            export_dir.mkdir(parents=True)
            legacy_path = export_dir / "EXP-TEST.csv"
            randomized_path = export_dir / "EXP-TEST-a1b2c3d4.csv"
            wrong_path = export_dir / "EXP-OTHER-a1b2c3d4.csv"
            legacy_path.write_text("legacy", encoding="utf-8")
            randomized_path.write_text("randomized", encoding="utf-8")
            wrong_path.write_text("wrong", encoding="utf-8")

            with override_settings(MEDIA_ROOT=temp_dir):
                self.assertEqual(resolve_export_file_path(str(legacy_path), "EXP-TEST"), legacy_path.resolve())
                self.assertEqual(resolve_export_file_path(str(randomized_path), "EXP-TEST"), randomized_path.resolve())
                self.assertIsNone(resolve_export_file_path(str(wrong_path), "EXP-TEST"))

    @override_settings(ERP_MAX_CSV_IMPORT_ROWS=1)
    def test_read_csv_dict_rows_rejects_too_many_rows(self):
        with self.assertRaises(CsvImportReadError) as context:
            read_csv_dict_rows(StringIO("物料编码,物料名称\nA,一\nB,二\n"))

        self.assertIn("超过 1 行限制", str(context.exception))

    def test_read_csv_dict_rows_rejects_extra_columns(self):
        with self.assertRaises(CsvImportReadError) as context:
            read_csv_dict_rows(StringIO("物料编码,物料名称\nA,一,多余\n"))

        self.assertIn("列数超过表头", str(context.exception))

    def test_read_csv_dict_rows_normalizes_chinese_headers(self):
        rows = read_csv_dict_rows(StringIO("物料编码,物料名称\nRM001,中文 English 混合\n"))

        self.assertEqual(rows, [{"material_code": "RM001", "material_name": "中文 English 混合"}])

    def test_read_csv_dict_rows_rejects_english_headers(self):
        with self.assertRaises(CsvImportReadError) as context:
            read_csv_dict_rows(StringIO("material_code,material_name\nRM001,中文 English 混合\n"))

        self.assertIn("不支持的表头", str(context.exception))

    def test_csv_import_header_row_uses_chinese_labels(self):
        header = csv_import_header_row(("material_code", "material_name", "unknown_field"))

        self.assertEqual(header, ("物料编码", "物料名称", "unknown_field"))

    def test_uploaded_csv_text_file_accepts_gbk_csv_from_excel(self):
        upload = SimpleUploadedFile(
            "materials.csv",
            "物料编码,物料名称\nRM001,中文 English 混合\n".encode("gbk"),
            content_type="text/csv",
        )

        rows = read_csv_dict_rows(uploaded_csv_text_file(upload))

        self.assertEqual(rows, [{"material_code": "RM001", "material_name": "中文 English 混合"}])


class FileViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="fileview", password="x")
        self.customer = Customer.objects.create(customer_no="C001", customer_name="测试客户", sales_owner=self.user)
        self.sales_order = SalesOrder.objects.create(
            sales_order_no="SO001",
            customer=self.customer,
            order_date="2026-06-08",
            status=SalesOrder.Status.DRAFT,
            created_by=self.user,
        )
        self.customer_receipt = CustomerReceipt.objects.create(
            receipt_no="RC001",
            customer=self.customer,
            receipt_date="2026-06-08",
            receipt_amount="100.00",
            status=CustomerReceipt.Status.PENDING_APPROVAL,
            handled_by=self.user,
            created_by=self.user,
        )
        self.customer_invoice = CustomerInvoice.objects.create(
            invoice_no="INV-FILE-001",
            external_invoice_no="FP-FILE-001",
            customer=self.customer,
            invoice_date="2026-06-08",
            invoice_amount="100.00",
            status=CustomerInvoice.Status.DRAFT,
            created_by=self.user,
        )
        self.reconciliation = Reconciliation.objects.create(
            reconciliation_no="REC-FILE-001",
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=self.customer,
            period_start="2026-06-01",
            period_end="2026-06-08",
            total_amount="100.00",
            status=Reconciliation.Status.DRAFT,
            created_by=self.user,
        )
        self.expense_record = ExpenseRecord.objects.create(
            expense_no="EX-FILE-001",
            expense_date="2026-06-08",
            category=ExpenseRecord.ExpenseCategory.FREIGHT,
            amount="12.30",
            payment_method=ExpenseRecord.PaymentMethod.CASH,
            payee="物流公司",
            status=ExpenseRecord.Status.DRAFT,
            handled_by=self.user,
            created_by=self.user,
        )
        self.temp_dir = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.temp_dir.cleanup()

    def test_attachment_upload_view_saves_file_and_registers_attachment(self):
        self.client.force_login(self.user)
        uploaded_file = SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf")

        response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "sales_order",
                "source_doc_id": str(self.sales_order.id),
                "source_doc_no": "FORGED-SO-NO",
                "file": uploaded_file,
                "is_sensitive": "on",
            },
        )

        attachment = Attachment.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/files/{attachment.id}/")
        self.assertEqual(attachment.original_filename, "contract.pdf")
        self.assertTrue(attachment.is_sensitive)
        self.assertEqual(attachment.source_doc_no, self.sales_order.sales_order_no)

    def test_attachment_upload_from_source_doc_redirects_back_to_source_doc(self):
        self.client.force_login(self.user)
        return_to = f"/sales/orders/{self.sales_order.id}/"

        response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "sales_order",
                "source_doc_id": str(self.sales_order.id),
                "file": SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf"),
                "return_to": return_to,
            },
        )

        attachment = Attachment.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], return_to)
        self.assertEqual(attachment.source_doc_type, "sales_order")
        self.assertEqual(attachment.source_doc_id, self.sales_order.id)

    def test_attachment_upload_accepts_readable_source_doc_no(self):
        self.client.force_login(self.user)

        response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "sales_order",
                "source_doc_no": self.sales_order.sales_order_no,
                "file": SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf"),
            },
        )

        attachment = Attachment.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(attachment.source_doc_id, self.sales_order.id)
        self.assertEqual(attachment.source_doc_no, self.sales_order.sales_order_no)

    def test_attachment_detail_links_back_to_source_doc(self):
        attachment = Attachment.objects.create(
            attachment_no="ATT-SOURCE-LINK",
            source_doc_type="sales_order",
            source_doc_id=self.sales_order.id,
            source_doc_no=self.sales_order.sales_order_no,
            original_filename="contract.pdf",
            stored_filename="contract.pdf",
            file_path="attachments/contract.pdf",
            file_size=100,
            uploaded_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/{attachment.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "返回来源单据")
        self.assertContains(response, f'href="/sales/orders/{self.sales_order.id}/"')

    def test_expense_record_attachment_upload_and_detail_link_back_to_source_doc(self):
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self.client.force_login(self.user)
        return_to = f"/finance/expenses/{self.expense_record.id}/"

        upload_response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "expense_record",
                "source_doc_id": str(self.expense_record.id),
                "file": SimpleUploadedFile("invoice.pdf", b"pdf-content", content_type="application/pdf"),
                "return_to": return_to,
            },
        )

        attachment = Attachment.objects.get(original_filename="invoice.pdf")
        detail_response = self.client.get(f"/files/{attachment.id}/")

        self.assertEqual(upload_response.status_code, 302)
        self.assertEqual(upload_response["Location"], return_to)
        self.assertEqual(attachment.source_doc_no, self.expense_record.expense_no)
        self.assertContains(detail_response, "返回来源单据")
        self.assertContains(detail_response, f'href="/finance/expenses/{self.expense_record.id}/"')

    def test_attachment_detail_download_and_delete_views(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.ATTACHMENT_DELETE)
        upload_response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "sales_order",
                "source_doc_id": str(self.sales_order.id),
                "source_doc_no": self.sales_order.sales_order_no,
                "file": SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf"),
            },
        )
        attachment = Attachment.objects.get()
        self.assertEqual(upload_response["Location"], f"/files/{attachment.id}/")

        detail_response = self.client.get(f"/files/{attachment.id}/")
        download_response = self.client.get(f"/files/{attachment.id}/download/")
        delete_response = self.client.post(f"/files/{attachment.id}/delete/", {"reason": "录错"})
        deleted_download_response = self.client.get(f"/files/{attachment.id}/download/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "contract.pdf")
        self.assertEqual(download_response.status_code, 200)
        b"".join(download_response.streaming_content)
        download_response.close()
        self.assertEqual(delete_response.status_code, 302)
        attachment.refresh_from_db()
        self.assertEqual(attachment.status, Attachment.AttachmentStatus.DELETED)
        self.assertEqual(AttachmentAccessLog.objects.filter(action="download").count(), 1)
        self.assertEqual(deleted_download_response.status_code, 404)

    def test_sensitive_attachment_download_requires_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        upload_response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "customer_receipt",
                "source_doc_id": str(self.customer_receipt.id),
                "source_doc_no": self.customer_receipt.receipt_no,
                "file": SimpleUploadedFile("receipt.pdf", b"pdf-content", content_type="application/pdf"),
                "is_sensitive": "on",
            },
        )
        attachment = Attachment.objects.get()
        self.assertEqual(upload_response["Location"], f"/files/{attachment.id}/")

        detail_response = self.client.get(f"/files/{attachment.id}/")
        denied_response = self.client.get(f"/files/{attachment.id}/download/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertNotContains(detail_response, "下载")
        self.assertEqual(denied_response.status_code, 404)
        self.assertFalse(AttachmentAccessLog.objects.filter(action="download").exists())

    def test_sensitive_attachment_download_with_permission(self):
        self.client.force_login(self.user)
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        self._grant_permission(PermissionCode.ATTACHMENT_VIEW_SENSITIVE)
        self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "customer_receipt",
                "source_doc_id": str(self.customer_receipt.id),
                "source_doc_no": self.customer_receipt.receipt_no,
                "file": SimpleUploadedFile("receipt.pdf", b"pdf-content", content_type="application/pdf"),
                "is_sensitive": "on",
            },
        )
        attachment = Attachment.objects.get()

        response = self.client.get(f"/files/{attachment.id}/download/")

        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)
        response.close()
        self.assertEqual(AttachmentAccessLog.objects.filter(action="download").count(), 1)

    def test_missing_attachment_file_does_not_write_download_log(self):
        attachment = Attachment.objects.create(
            attachment_no="ATT-MISSING-FILE",
            source_doc_type="sales_order",
            source_doc_id=self.sales_order.id,
            source_doc_no=self.sales_order.sales_order_no,
            original_filename="missing.pdf",
            stored_filename="missing.pdf",
            file_path="attachments/missing.pdf",
            file_size=100,
            uploaded_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/{attachment.id}/download/")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(AttachmentAccessLog.objects.filter(attachment=attachment, action="download").exists())

    def test_download_rejects_attachment_path_outside_attachment_dir(self):
        exports_dir = Path(self.temp_dir.name) / "exports"
        exports_dir.mkdir()
        (exports_dir / "leak.pdf").write_bytes(b"secret")
        attachment = Attachment.objects.create(
            attachment_no="ATT-BAD-PATH",
            source_doc_type="sales_order",
            source_doc_id=self.sales_order.id,
            source_doc_no=self.sales_order.sales_order_no,
            original_filename="leak.pdf",
            stored_filename="leak.pdf",
            file_path="exports/leak.pdf",
            file_size=6,
            uploaded_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/{attachment.id}/download/")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(AttachmentAccessLog.objects.filter(attachment=attachment, action="download").exists())

    def test_attachment_delete_requires_permission(self):
        self.client.force_login(self.user)
        self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "sales_order",
                "source_doc_id": str(self.sales_order.id),
                "source_doc_no": self.sales_order.sales_order_no,
                "file": SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf"),
            },
        )
        attachment = Attachment.objects.get()

        response = self.client.post(f"/files/{attachment.id}/delete/", {"reason": "录错"})

        self.assertEqual(response.status_code, 302)
        attachment.refresh_from_db()
        self.assertEqual(attachment.status, Attachment.AttachmentStatus.ACTIVE)

    def test_upload_requires_source_document_access(self):
        other_user = get_user_model().objects.create_user(username="other-sales", password="x")
        other_customer = Customer.objects.create(customer_no="C002", customer_name="其他客户", sales_owner=other_user)
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date="2026-06-08",
            status=SalesOrder.Status.DRAFT,
            created_by=other_user,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "sales_order",
                "source_doc_id": str(other_order.id),
                "source_doc_no": other_order.sales_order_no,
                "file": SimpleUploadedFile("contract.pdf", b"pdf-content", content_type="application/pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Attachment.objects.exists())

    def test_reconciliation_attachment_upload_requires_finance_amount_permission(self):
        self.client.force_login(self.user)

        denied_response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "reconciliation",
                "source_doc_id": str(self.reconciliation.id),
                "source_doc_no": self.reconciliation.reconciliation_no,
                "file": SimpleUploadedFile("reconciliation.pdf", b"pdf-content", content_type="application/pdf"),
            },
        )
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        allowed_response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "reconciliation",
                "source_doc_id": str(self.reconciliation.id),
                "source_doc_no": self.reconciliation.reconciliation_no,
                "file": SimpleUploadedFile("reconciliation.pdf", b"pdf-content", content_type="application/pdf"),
            },
        )

        attachment = Attachment.objects.get()
        self.assertEqual(denied_response.status_code, 302)
        self.assertEqual(allowed_response.status_code, 302)
        self.assertEqual(allowed_response["Location"], f"/files/{attachment.id}/")
        self.assertEqual(attachment.source_doc_type, "reconciliation")
        self.assertEqual(attachment.source_doc_id, self.reconciliation.id)

    def test_customer_invoice_attachment_upload_links_back_to_invoice(self):
        self._grant_permission(PermissionCode.SALES_PROCESS)
        self.client.force_login(self.user)
        return_to = f"/finance/customer-invoices/{self.customer_invoice.id}/"

        upload_response = self.client.post(
            "/files/upload/",
            {
                "source_doc_type": "customer_invoice",
                "source_doc_id": str(self.customer_invoice.id),
                "file": SimpleUploadedFile("invoice.pdf", b"pdf-content", content_type="application/pdf"),
                "return_to": return_to,
            },
        )

        attachment = Attachment.objects.get(original_filename="invoice.pdf")
        detail_response = self.client.get(f"/files/{attachment.id}/")

        self.assertEqual(upload_response.status_code, 302)
        self.assertEqual(upload_response["Location"], return_to)
        self.assertEqual(attachment.source_doc_no, self.customer_invoice.invoice_no)
        self.assertContains(detail_response, "返回来源单据")
        self.assertContains(detail_response, f'href="/finance/customer-invoices/{self.customer_invoice.id}/"')

    def test_attachment_detail_and_download_require_source_document_access(self):
        other_user = get_user_model().objects.create_user(username="other-sales", password="x")
        other_customer = Customer.objects.create(customer_no="C002", customer_name="其他客户", sales_owner=other_user)
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date="2026-06-08",
            status=SalesOrder.Status.DRAFT,
            created_by=other_user,
        )
        attachment = Attachment.objects.create(
            attachment_no="ATT-SOURCE",
            source_doc_type="sales_order",
            source_doc_id=other_order.id,
            source_doc_no=other_order.sales_order_no,
            original_filename="contract.pdf",
            stored_filename="contract.pdf",
            file_path="attachments/contract.pdf",
            file_size=100,
            uploaded_by=other_user,
        )
        self.client.force_login(self.user)

        detail_response = self.client.get(f"/files/{attachment.id}/")
        download_response = self.client.get(f"/files/{attachment.id}/download/")
        list_response = self.client.get("/files/")

        self.assertEqual(detail_response.status_code, 404)
        self.assertEqual(download_response.status_code, 404)
        self.assertNotContains(list_response, "contract.pdf")

    def test_sales_view_all_can_access_sales_order_attachment(self):
        other_user = get_user_model().objects.create_user(username="other-sales", password="x")
        other_customer = Customer.objects.create(customer_no="C002", customer_name="其他客户", sales_owner=other_user)
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date="2026-06-08",
            status=SalesOrder.Status.DRAFT,
            created_by=other_user,
        )
        attachment = Attachment.objects.create(
            attachment_no="ATT-VIEW-ALL",
            source_doc_type="sales_order",
            source_doc_id=other_order.id,
            source_doc_no=other_order.sales_order_no,
            original_filename="contract.pdf",
            stored_filename="contract.pdf",
            file_path="attachments/contract.pdf",
            file_size=100,
            uploaded_by=other_user,
        )
        self._grant_permission(PermissionCode.SALES_VIEW_ALL)
        self.client.force_login(self.user)

        response = self.client.get(f"/files/{attachment.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "contract.pdf")

    def test_operational_document_attachments_require_module_process_permission(self):
        other_user = get_user_model().objects.create_user(username="other-operator", password="x")
        supplier = Supplier.objects.create(supplier_no="S-FILE", supplier_name="附件供应商")
        material = Material.objects.create(
            material_code="FG-FILE",
            material_name="附件成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        bom = Bom.objects.create(
            bom_no="BOM-FILE",
            finished_material=material,
            bom_version="V1",
            status=Bom.BomStatus.ENABLED,
        )
        purchase_request = PurchaseRequest.objects.create(purchase_request_no="PR-FILE", requested_by=other_user)
        purchase_order = PurchaseOrder.objects.create(
            purchase_order_no="PO-FILE",
            supplier=supplier,
            order_date="2026-06-08",
            created_by=other_user,
        )
        production_order = ProductionOrder.objects.create(
            production_order_no="MO-FILE",
            finished_material=material,
            production_qty="1.0000",
            locked_bom=bom,
            locked_bom_version="V1",
            created_by=other_user,
        )
        stock_count = StockCount.objects.create(stock_count_no="SC-FILE", scope_type="batch", created_by=other_user)
        attachments = [
            ("purchase_request", purchase_request.id, purchase_request.purchase_request_no),
            ("purchase_order", purchase_order.id, purchase_order.purchase_order_no),
            ("production_order", production_order.id, production_order.production_order_no),
            ("stock_count", stock_count.id, stock_count.stock_count_no),
        ]
        for source_doc_type, source_doc_id, source_doc_no in attachments:
            Attachment.objects.create(
                attachment_no=f"ATT-{source_doc_type}",
                source_doc_type=source_doc_type,
                source_doc_id=source_doc_id,
                source_doc_no=source_doc_no,
                original_filename=f"{source_doc_type}.pdf",
                stored_filename=f"{source_doc_type}.pdf",
                file_path=f"attachments/{source_doc_type}.pdf",
                file_size=100,
                uploaded_by=other_user,
            )
        self.client.force_login(self.user)

        denied_response = self.client.get("/files/")
        for source_doc_type, _source_doc_id, _source_doc_no in attachments:
            self.assertNotContains(denied_response, f"{source_doc_type}.pdf")

        self._grant_permission(PermissionCode.PURCHASE_PROCESS)
        self._grant_permission(PermissionCode.INVENTORY_PROCESS)
        self._grant_permission(PermissionCode.PRODUCTION_PROCESS)
        allowed_response = self.client.get("/files/")

        self.assertEqual(allowed_response.status_code, 200)
        for source_doc_type, _source_doc_id, _source_doc_no in attachments:
            self.assertContains(allowed_response, f"{source_doc_type}.pdf")

    def test_finance_attachments_require_amount_permission(self):
        other_user = get_user_model().objects.create_user(username="other-finance", password="x")
        attachment = Attachment.objects.create(
            attachment_no="ATT-FIN-AMOUNT",
            source_doc_type="customer_receipt",
            source_doc_id=self.customer_receipt.id,
            source_doc_no=self.customer_receipt.receipt_no,
            original_filename="receipt.pdf",
            stored_filename="receipt.pdf",
            file_path="attachments/receipt.pdf",
            file_size=100,
            uploaded_by=other_user,
        )
        self.client.force_login(self.user)

        denied_response = self.client.get(f"/files/{attachment.id}/")
        self._grant_permission(PermissionCode.FINANCE_VIEW_AMOUNT)
        allowed_response = self.client.get(f"/files/{attachment.id}/")

        self.assertEqual(denied_response.status_code, 404)
        self.assertEqual(allowed_response.status_code, 200)
        self.assertContains(allowed_response, "receipt.pdf")

    def test_attachment_list_filters_visible_files_by_keyword_and_status(self):
        other_user = get_user_model().objects.create_user(username="other-sales-filter", password="x")
        other_customer = Customer.objects.create(customer_no="C002", customer_name="其他客户", sales_owner=other_user)
        other_order = SalesOrder.objects.create(
            sales_order_no="SO-OTHER",
            customer=other_customer,
            order_date="2026-06-08",
            status=SalesOrder.Status.DRAFT,
            created_by=other_user,
        )
        Attachment.objects.create(
            attachment_no="ATT-KEEP",
            source_doc_type="sales_order",
            source_doc_id=self.sales_order.id,
            source_doc_no=self.sales_order.sales_order_no,
            original_filename="contract-keep.pdf",
            stored_filename="contract-keep.pdf",
            file_path="attachments/contract-keep.pdf",
            file_size=100,
            status=Attachment.AttachmentStatus.ACTIVE,
            uploaded_by=self.user,
        )
        Attachment.objects.create(
            attachment_no="ATT-DELETED",
            source_doc_type="sales_order",
            source_doc_id=self.sales_order.id,
            source_doc_no=self.sales_order.sales_order_no,
            original_filename="contract-deleted.pdf",
            stored_filename="contract-deleted.pdf",
            file_path="attachments/contract-deleted.pdf",
            file_size=100,
            status=Attachment.AttachmentStatus.DELETED,
            uploaded_by=self.user,
        )
        Attachment.objects.create(
            attachment_no="ATT-HIDDEN",
            source_doc_type="sales_order",
            source_doc_id=other_order.id,
            source_doc_no=other_order.sales_order_no,
            original_filename="contract-keep-hidden.pdf",
            stored_filename="contract-keep-hidden.pdf",
            file_path="attachments/contract-keep-hidden.pdf",
            file_size=100,
            status=Attachment.AttachmentStatus.ACTIVE,
            uploaded_by=other_user,
        )
        self.client.force_login(self.user)

        response = self.client.get("/files/?q=keep&status=active")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "contract-keep.pdf")
        self.assertNotContains(response, "contract-deleted.pdf")
        self.assertNotContains(response, "contract-keep-hidden.pdf")
        self.assertContains(response, "清除")

    def test_attachment_access_log_list_requires_permission(self):
        self.client.force_login(self.user)

        response = self.client.get("/files/access-logs/")

        self.assertEqual(response.status_code, 403)

    def test_attachment_access_log_list_filters_logs(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        attachment = Attachment.objects.create(
            attachment_no="ATT-LOG-1",
            source_doc_type="sales_order",
            source_doc_id=self.sales_order.id,
            source_doc_no=self.sales_order.sales_order_no,
            original_filename="contract.pdf",
            stored_filename="contract.pdf",
            file_path="attachments/contract.pdf",
            file_size=100,
            uploaded_by=self.user,
        )
        other_attachment = Attachment.objects.create(
            attachment_no="ATT-LOG-2",
            source_doc_type="customer_receipt",
            source_doc_id=self.customer_receipt.id,
            source_doc_no=self.customer_receipt.receipt_no,
            original_filename="receipt.pdf",
            stored_filename="receipt.pdf",
            file_path="attachments/receipt.pdf",
            file_size=100,
            uploaded_by=self.user,
        )
        keep_log = AttachmentAccessLog.objects.create(attachment=attachment, operator=self.user, action="download", ip_address="127.0.0.1")
        hide_log = AttachmentAccessLog.objects.create(attachment=other_attachment, operator=self.user, action="delete", ip_address="127.0.0.2")
        AttachmentAccessLog.objects.filter(id=keep_log.id).update(created_at=datetime(2026, 7, 4, 9, 0, tzinfo=timezone.get_current_timezone()))
        AttachmentAccessLog.objects.filter(id=hide_log.id).update(created_at=timezone.now() - timedelta(days=30))
        self.client.force_login(self.user)

        response = self.client.get(
            "/files/access-logs/",
            {"q": "SO001", "action": "download", "operator": "fileview", "date_from": "2026/7/4", "date_to": "2026/7/4"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "contract.pdf")
        self.assertContains(response, "127.0.0.1")
        self.assertNotContains(response, "receipt.pdf")

    def test_operational_log_lists_require_permission(self):
        self.client.force_login(self.user)

        paths = [
            "/files/import-jobs/",
            "/files/initialization-jobs/",
            "/files/export-logs/",
            "/files/print-logs/",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403)

    def test_import_job_list_filters_jobs(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        job = ImportJob.objects.create(
            job_no="IMP-LOG-1",
            template_type="materials",
            status=ImportJob.JobStatus.SUCCESS,
            success_count=3,
            created_by=self.user,
        )
        ImportJob.objects.create(
            job_no="IMP-LOG-2",
            template_type="customers",
            status=ImportJob.JobStatus.FAILED,
            failed_count=1,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get("/files/import-jobs/", {"q": "materials", "status": ImportJob.JobStatus.SUCCESS})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IMP-LOG-1")
        self.assertContains(response, f"/files/import-jobs/{job.id}/")
        self.assertContains(response, "materials")
        self.assertNotContains(response, "IMP-LOG-2")

    def test_import_job_detail_shows_validation_errors(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        job = ImportJob.objects.create(
            job_no="IMP-DETAIL",
            template_type="materials",
            status=ImportJob.JobStatus.FAILED,
            failed_count=1,
            error_summary={"errors": [{"row": 2, "field": "material_code", "message": "物料编码已存在"}]},
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/import-jobs/{job.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IMP-DETAIL")
        self.assertContains(response, "物料编码")
        self.assertNotContains(response, "material_code")
        self.assertContains(response, "物料编码已存在")

    def test_initialization_job_list_filters_jobs(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        job = InitializationJob.objects.create(
            job_no="INI-LOG-1",
            template_type="initial_inventory",
            status=InitializationJob.JobStatus.SUCCESS,
            success_count=2,
            confirmed_by=self.user,
            created_by=self.user,
        )
        InitializationJob.objects.create(
            job_no="INI-LOG-2",
            template_type="other",
            status=InitializationJob.JobStatus.FAILED,
            failed_count=1,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            "/files/initialization-jobs/",
            {"q": "initial_inventory", "status": InitializationJob.JobStatus.SUCCESS},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INI-LOG-1")
        self.assertContains(response, f"/files/initialization-jobs/{job.id}/")
        self.assertContains(response, "initial_inventory")
        self.assertNotContains(response, "INI-LOG-2")

    def test_initialization_job_detail_shows_validation_errors(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        job = InitializationJob.objects.create(
            job_no="INI-DETAIL",
            template_type="initial_inventory",
            status=InitializationJob.JobStatus.FAILED,
            failed_count=1,
            error_summary={"errors": [{"row": 3, "field": "batch_no", "message": "批次号已存在"}]},
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/initialization-jobs/{job.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INI-DETAIL")
        self.assertContains(response, "批次号")
        self.assertNotContains(response, "batch_no")
        self.assertContains(response, "批次号已存在")

    def test_export_and_print_log_lists_filter_logs(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        export_log = ExportLog.objects.create(
            export_no="EXP-LOG-1",
            module="materials",
            filter_json={"q": "RM"},
            row_count=5,
            exported_by=self.user,
        )
        ExportLog.objects.create(
            export_no="EXP-LOG-2",
            module="sales_orders",
            row_count=1,
            exported_by=self.user,
        )
        print_log = PrintLog.objects.create(
            print_no="PRT-LOG-1",
            template_type="sales_order",
            source_doc_type="sales_order",
            source_doc_id=1,
            source_doc_no="SO-LOG-1",
            printed_by=self.user,
        )
        PrintLog.objects.create(
            print_no="PRT-LOG-2",
            template_type="purchase_order",
            source_doc_type="purchase_order",
            source_doc_id=2,
            source_doc_no="PO-LOG-2",
            printed_by=self.user,
        )
        self.client.force_login(self.user)

        export_response = self.client.get("/files/export-logs/", {"q": "materials"})
        print_response = self.client.get("/files/print-logs/", {"q": "SO-LOG-1"})

        self.assertEqual(export_response.status_code, 200)
        self.assertContains(export_response, "EXP-LOG-1")
        self.assertContains(export_response, f"/files/export-logs/{export_log.id}/")
        self.assertNotContains(export_response, "EXP-LOG-2")
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "PRT-LOG-1")
        self.assertContains(print_response, f"/files/print-logs/{print_log.id}/")
        self.assertContains(print_response, "SO-LOG-1")
        self.assertNotContains(print_response, "PRT-LOG-2")

    def test_print_log_detail_shows_source_and_operator(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        print_log = PrintLog.objects.create(
            print_no="PRT-DETAIL",
            template_type="sales_order",
            source_doc_type="sales_order",
            source_doc_id=1,
            source_doc_no="SO-DETAIL",
            printed_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/print-logs/{print_log.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PRT-DETAIL")
        self.assertContains(response, "销售订单")
        self.assertContains(response, "SO-DETAIL")
        self.assertContains(response, self.user.username)

    def test_export_log_detail_shows_download_action_and_filter_json(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        export_path = self._write_export_file("EXP-DETAIL", "编码,名称\nRM001,原料\n")
        export_log = ExportLog.objects.create(
            export_no="EXP-DETAIL",
            module="materials",
            filter_json={"q": "RM001"},
            file_path=str(export_path),
            row_count=1,
            exported_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/export-logs/{export_log.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "EXP-DETAIL")
        self.assertContains(response, "物料")
        self.assertContains(response, "下载CSV")
        self.assertContains(response, "RM001")

    def test_export_log_detail_hides_download_for_non_exporter(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        exporter = get_user_model().objects.create_user(username="actual-exporter", password="x")
        export_path = self._write_export_file("EXP-OTHER", "编码,名称\nRM001,原料\n")
        export_log = ExportLog.objects.create(
            export_no="EXP-OTHER",
            module="materials",
            file_path=str(export_path),
            row_count=1,
            exported_by=exporter,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/export-logs/{export_log.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "仅导出人可下载")
        self.assertNotContains(response, "下载CSV")

    def test_export_log_download_requires_permission(self):
        export_path = self._write_export_file("EXP-DOWNLOAD", "编码,名称\nRM001,原料\n")
        export_log = ExportLog.objects.create(
            export_no="EXP-DOWNLOAD",
            module="materials",
            file_path=str(export_path),
            row_count=1,
            exported_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/export-logs/{export_log.id}/download/")

        self.assertEqual(response.status_code, 404)

    def test_export_log_download_returns_csv_for_exporter_with_permission(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        export_path = self._write_export_file("EXP-DOWNLOAD", "编码,名称\nRM001,原料\n")
        export_log = ExportLog.objects.create(
            export_no="EXP-DOWNLOAD",
            module="materials",
            file_path=str(export_path),
            row_count=1,
            exported_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/export-logs/{export_log.id}/download/")
        content = b"".join(response.streaming_content).decode("utf-8-sig")
        response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("RM001", content)
        self.assertIn('filename="EXP-DOWNLOAD.csv"', response["Content-Disposition"])

    def test_export_log_download_rejects_permission_manager_who_is_not_exporter(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        exporter = get_user_model().objects.create_user(username="other-exporter", password="x")
        export_path = self._write_export_file("EXP-OTHER-DOWNLOAD", "编码,名称\nRM001,原料\n")
        export_log = ExportLog.objects.create(
            export_no="EXP-OTHER-DOWNLOAD",
            module="materials",
            file_path=str(export_path),
            row_count=1,
            exported_by=exporter,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/export-logs/{export_log.id}/download/")

        self.assertEqual(response.status_code, 404)

    def test_export_log_download_rejects_file_outside_export_dir(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        outside_path = Path(self.temp_dir.name) / "EXP-OUTSIDE.csv"
        outside_path.write_text("secret", encoding="utf-8")
        export_log = ExportLog.objects.create(
            export_no="EXP-OUTSIDE",
            module="materials",
            file_path=str(outside_path),
            row_count=1,
            exported_by=self.user,
        )
        self.client.force_login(self.user)

        detail_response = self.client.get(f"/files/export-logs/{export_log.id}/")
        download_response = self.client.get(f"/files/export-logs/{export_log.id}/download/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "文件不存在")
        self.assertNotContains(detail_response, "下载CSV")
        self.assertEqual(download_response.status_code, 404)

    def test_export_log_download_missing_file_returns_404(self):
        self._grant_permission(PermissionCode.ADMIN_PERMISSION_MANAGE)
        export_log = ExportLog.objects.create(
            export_no="EXP-MISSING",
            module="materials",
            file_path="EXP-MISSING.csv",
            row_count=1,
            exported_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/files/export-logs/{export_log.id}/download/")

        self.assertEqual(response.status_code, 404)

    def _write_export_file(self, export_no: str, content: str) -> Path:
        export_dir = Path(self.temp_dir.name) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"{export_no}.csv"
        export_path.write_text(content, encoding="utf-8-sig")
        return export_path

    def _grant_permission(self, permission_code: str):
        permission_names = {
            PermissionCode.ADMIN_PERMISSION_MANAGE: "权限与审批规则管理",
            PermissionCode.ATTACHMENT_DELETE: "附件删除",
            PermissionCode.ATTACHMENT_VIEW_SENSITIVE: "敏感附件查看",
            PermissionCode.SALES_VIEW_ALL: "查看全部销售数据",
            PermissionCode.FINANCE_VIEW_AMOUNT: "查看财务金额",
            PermissionCode.PURCHASE_PROCESS: "处理采购单据",
            PermissionCode.INVENTORY_PROCESS: "处理库存单据",
            PermissionCode.PRODUCTION_PROCESS: "处理生产单据",
            PermissionCode.FINANCE_PAYMENT_PROCESS: "处理收付款和余额",
        }
        permission_types = {
            PermissionCode.SALES_VIEW_ALL: Permission.PermissionType.DATA_SCOPE,
            PermissionCode.ATTACHMENT_VIEW_SENSITIVE: Permission.PermissionType.FIELD,
            PermissionCode.FINANCE_VIEW_AMOUNT: Permission.PermissionType.FIELD,
        }
        permission, _ = Permission.objects.get_or_create(
            permission_code=permission_code,
            defaults={
                "permission_name": permission_names.get(permission_code, permission_code),
                "permission_type": permission_types.get(permission_code, Permission.PermissionType.ACTION),
            },
        )
        role = Role.objects.create(role_code=f"file-role-{permission_code}", role_name=permission.permission_name)
        role.permissions.add(permission)
        self.user.roles.add(role)
        return role
