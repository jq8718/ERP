import tempfile
import os
import subprocess
import sys
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import management
from django.core.management import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Permission, Role
from accounts.permissions import PermissionCode
from accounts.management.commands.check_permission_references import check_permission_references
from accounts.management.commands.check_permissions import check_permissions
from bom.models import Bom
from finance.models import (
    CustomerCreditBalance,
    CustomerReceipt,
    CustomerReceiptAllocation,
    CustomerReceiptReversal,
    SupplierCreditBalance,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from inventory.models import InventoryBatch, StockCount
from masterdata.models import Customer, CustomerProduct, Material, Supplier
from notifications.models import SystemMessage
from production.models import ProductionMaterialRequisition, ProductionOrder, ProductionReceipt
from purchase.models import PurchaseOrder, PurchaseRequest
from sales.models import SalesOrder, SalesOrderItem, ShortageAlert
from sales.models import SampleLoan, SampleLoanReturn
from .backup_services import backup_daily, cleanup_backups, restore_drill, verify_backups
from .management.commands.deployment_runbook import build_deployment_runbook
from .management.commands.check_templates import check_templates
from .management.commands.check_route_protection import check_route_protection, collect_unprotected_routes
from .management.commands.check_csrf_tokens import check_csrf_tokens
from .management.commands.check_navigation_pages import check_navigation_pages, collect_navigation_paths
from .management.commands.check_low_frequency_entrypoints import (
    check_low_frequency_entrypoints,
    collect_low_frequency_entrypoints,
)
from .management.commands.check_url_references import check_url_references
from .management.commands.release_gate import _build_gate_steps, _summarize_output
from .management.commands.production_preflight import run_preflight_checks
from .models import AuditLog, BackgroundJob, Backup, PendingEvent, ReleaseRecord, SavedFilter
from .release_gate_status import get_release_gate_report_status
from .services import process_pending_events, record_audit_log
from .views import bad_request_view, server_error_view


class SystemDashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="dashboard", password="x")

    def test_dashboard_requires_login(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_dashboard_renders_for_authenticated_user(self):
        self.client.force_login(self.user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "工作台")

    def test_dashboard_hides_active_snoozed_messages(self):
        SystemMessage.objects.create(
            message_no="MSG-DASH-SNOOZE",
            receiver=self.user,
            title="稍后再看",
            status=SystemMessage.Status.SNOOZED,
            snoozed_until=timezone.now() + timedelta(hours=1),
        )
        SystemMessage.objects.create(
            message_no="MSG-DASH-ACTIVE",
            receiver=self.user,
            title="现在处理",
            status=SystemMessage.Status.UNREAD,
        )
        self.client.force_login(self.user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "现在处理")
        self.assertNotContains(response, "稍后再看")

    def test_dashboard_sales_widgets_follow_sales_scope(self):
        other_user = get_user_model().objects.create_user(username="dashboard-other", password="x")
        own_order = self._sales_order("SO-DASH-OWN", "C-DASH-OWN", self.user)
        other_order = self._sales_order("SO-DASH-OTHER", "C-DASH-OTHER", other_user)
        self._shortage("SA-DASH-OWN", own_order)
        self._shortage("SA-DASH-OTHER", other_order)
        self.client.force_login(self.user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SO-DASH-OWN")
        self.assertContains(response, "SA-DASH-OWN")
        self.assertContains(response, '<div class="stat-value">1</div>', html=True)
        self.assertNotContains(response, "SO-DASH-OTHER")
        self.assertNotContains(response, "SA-DASH-OTHER")

    def test_dashboard_sales_view_all_can_see_all_sales_widgets(self):
        other_user = get_user_model().objects.create_user(username="dashboard-other-all", password="x")
        own_order = self._sales_order("SO-DASH-ALL-OWN", "C-DASH-ALL-OWN", self.user)
        other_order = self._sales_order("SO-DASH-ALL-OTHER", "C-DASH-ALL-OTHER", other_user)
        self._shortage("SA-DASH-ALL-OWN", own_order)
        self._shortage("SA-DASH-ALL-OTHER", other_order)
        _grant_permission(self.user, PermissionCode.SALES_VIEW_ALL)
        self.client.force_login(self.user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SO-DASH-ALL-OWN")
        self.assertContains(response, "SO-DASH-ALL-OTHER")
        self.assertContains(response, "SA-DASH-ALL-OWN")
        self.assertContains(response, "SA-DASH-ALL-OTHER")

    def _sales_order(self, order_no, customer_no, owner):
        customer = Customer.objects.create(customer_no=customer_no, customer_name=customer_no, sales_owner=owner)
        material = Material.objects.create(
            material_code=f"FG-{customer_no}",
            material_name=f"成品 {customer_no}",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        customer_product = CustomerProduct.objects.create(
            customer=customer,
            customer_product_no=f"CP-{customer_no}",
            customer_product_name=f"客户产品 {customer_no}",
            finished_material=material,
        )
        order = SalesOrder.objects.create(
            sales_order_no=order_no,
            customer=customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.PENDING_APPROVAL,
            total_amount=100,
            created_by=owner,
        )
        SalesOrderItem.objects.create(
            sales_order=order,
            line_no=1,
            customer_product=customer_product,
            finished_material=material,
            order_qty=1,
            unit_price=100,
            line_amount=100,
            line_status=SalesOrderItem.LineStatus.PENDING_APPROVAL,
        )
        return order

    def _shortage(self, shortage_no, order):
        raw = Material.objects.create(
            material_code=f"RM-{shortage_no}",
            material_name=f"原料 {shortage_no}",
            material_type=Material.MaterialType.RAW,
            base_unit="pcs",
        )
        return ShortageAlert.objects.create(
            shortage_no=shortage_no,
            sales_order=order,
            sales_order_item=order.items.get(),
            material=raw,
            required_qty=10,
            available_qty=0,
            shortage_qty=10,
            status=ShortageAlert.Status.UNPROCESSED,
        )

    def test_primary_navigation_targets_render(self):
        self.client.force_login(self.user)

        paths = [
            "/notifications/",
            "/approvals/",
            "/sales/orders/",
            "/sales/shortages/",
            "/sales/shipments/",
            "/sales/sample-loans/",
            "/sales/sample-returns/",
            "/sales/returns/",
            "/purchase/requests/",
            "/purchase/orders/",
            "/purchase/receipts/",
            "/purchase/supplier-returns/",
            "/production/orders/",
            "/production/requisitions/",
            "/production/receipts/",
            "/inventory/",
            "/inventory/locations/",
            "/inventory/batches/",
            "/inventory/transactions/",
            "/inventory/transfers/",
            "/inventory/stock-counts/",
            "/finance/customer-receipts/",
            "/finance/supplier-payments/",
            "/finance/customer-balances/",
            "/finance/supplier-balances/",
            "/finance/reconciliations/",
            "/masterdata/materials/",
            "/bom/",
            "/masterdata/customers/",
            "/masterdata/customer-products/",
            "/masterdata/suppliers/",
            "/files/",
            "/users/",
            "/roles/",
            "/permissions/",
            "/user-sessions/",
            "/release-records/",
        ]
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_health_check_requires_permission(self):
        self.client.force_login(self.user)

        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "无权限访问", status_code=403)
        self.assertContains(response, "缺少系统健康检查权限", status_code=403)

    def test_health_check_renders_for_permission_manager(self):
        self.client.force_login(self.user)
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        Backup.objects.create(
            backup_no="BAK-HEALTH",
            backup_type="daily",
            file_path="backup.zip",
            file_size=100,
            checksum_sha256="a" * 64,
            status=Backup.BackupStatus.SUCCESS,
        )
        BackgroundJob.objects.create(
            job_no="JOB-HEALTH",
            job_type="backup",
            status=BackgroundJob.JobStatus.SUCCESS,
        )
        stale_job = BackgroundJob.objects.create(
            job_no="JOB-HEALTH-STALE",
            job_type="backup",
            status=BackgroundJob.JobStatus.RUNNING,
            started_at=timezone.now() - timedelta(minutes=121),
        )
        BackgroundJob.objects.filter(id=stale_job.id).update(started_at=timezone.now() - timedelta(minutes=121))
        PendingEvent.objects.create(
            event_type="purchase_received",
            idempotency_key="health:pending",
            status=PendingEvent.EventStatus.PENDING,
        )
        PendingEvent.objects.create(
            event_type="shortage_kitted",
            idempotency_key="health:failed",
            status=PendingEvent.EventStatus.FAILED,
            retry_count=2,
            last_error="模拟事件失败",
        )
        stale_event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="health:stale-running",
            status=PendingEvent.EventStatus.RUNNING,
        )
        PendingEvent.objects.filter(id=stale_event.id).update(updated_at=timezone.now() - timedelta(minutes=45))

        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "系统健康检查")
        self.assertContains(response, "数据库连接")
        self.assertContains(response, "附件目录")
        self.assertContains(response, "备份目录")
        self.assertContains(response, "/backups/")
        self.assertContains(response, "/background-jobs/")
        self.assertContains(response, "BAK-HEALTH")
        self.assertContains(response, "JOB-HEALTH")
        self.assertContains(response, "超时后台任务")
        self.assertContains(response, "阈值 120 分钟")
        self.assertContains(response, "事务后事件队列")
        self.assertContains(response, "超时 30 分钟：1")
        self.assertContains(response, "health:failed")
        self.assertContains(response, "模拟事件失败")
        self.assertContains(response, "发布门禁")

    def test_operational_system_lists_require_permission(self):
        self.client.force_login(self.user)

        for path in ["/backups/", "/background-jobs/", "/release-records/"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "无权限访问", status_code=403)

    def test_operational_system_lists_filter_records(self):
        self.client.force_login(self.user)
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        keep_backup = Backup.objects.create(
            backup_no="BAK-FILTER-KEEP",
            backup_type="daily",
            file_path="keep.zip",
            file_size=100,
            checksum_sha256="a" * 64,
            status=Backup.BackupStatus.SUCCESS,
            created_by=self.user,
        )
        Backup.objects.create(
            backup_no="BAK-FILTER-HIDE",
            backup_type="daily",
            file_path="hide.zip",
            file_size=100,
            checksum_sha256="b" * 64,
            status=Backup.BackupStatus.FAILED,
            created_by=self.user,
        )
        keep_job = BackgroundJob.objects.create(
            job_no="JOB-FILTER-KEEP",
            job_type="backup",
            trigger_type="manual",
            status=BackgroundJob.JobStatus.SUCCESS,
        )
        BackgroundJob.objects.create(
            job_no="JOB-FILTER-HIDE",
            job_type="restore_drill",
            trigger_type="manual",
            status=BackgroundJob.JobStatus.FAILED,
            error_message="失败",
        )

        backup_response = self.client.get("/backups/?q=KEEP&status=success")
        job_response = self.client.get("/background-jobs/?q=backup&status=success")

        self.assertEqual(backup_response.status_code, 200)
        self.assertContains(backup_response, keep_backup.backup_no)
        self.assertNotContains(backup_response, "BAK-FILTER-HIDE")
        self.assertContains(backup_response, "清除")
        self.assertEqual(job_response.status_code, 200)
        self.assertContains(job_response, keep_job.job_no)
        self.assertNotContains(job_response, "JOB-FILTER-HIDE")
        self.assertContains(job_response, "清除")

    def test_release_record_list_renders_for_permission_manager(self):
        self.client.force_login(self.user)
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        ReleaseRecord.objects.create(
            version_no="2026.06.11.1",
            released_at=timezone.now(),
            released_by=self.user,
            summary="预上线发布",
        )

        response = self.client.get("/release-records/?q=预上线")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "发布记录")
        self.assertContains(response, "2026.06.11.1")
        self.assertContains(response, "预上线发布")

    def test_common_list_can_save_apply_default_and_delete_filter(self):
        self.client.force_login(self.user)
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        Backup.objects.create(
            backup_no="BAK-SAVED-KEEP",
            backup_type="daily",
            file_path="keep.zip",
            file_size=100,
            checksum_sha256="a" * 64,
            status=Backup.BackupStatus.SUCCESS,
            created_by=self.user,
        )
        Backup.objects.create(
            backup_no="BAK-SAVED-HIDE",
            backup_type="daily",
            file_path="hide.zip",
            file_size=100,
            checksum_sha256="b" * 64,
            status=Backup.BackupStatus.FAILED,
            created_by=self.user,
        )

        save_response = self.client.post(
            "/saved-filters/save/",
            {
                "module": "system.backup",
                "filter_name": "成功备份",
                "query_string": "q=KEEP&status=success",
                "return_to": "/backups/?q=KEEP&status=success",
                "is_default": "on",
            },
        )

        self.assertEqual(save_response.status_code, 302)
        saved_filter = SavedFilter.objects.get(user=self.user, module="system.backup")
        self.assertEqual(saved_filter.filter_json["query"]["q"], "KEEP")
        self.assertTrue(saved_filter.is_default)

        default_response = self.client.get("/backups/")
        self.assertEqual(default_response.status_code, 302)
        self.assertEqual(default_response["Location"], "/backups/?q=KEEP&status=success")

        filtered_response = self.client.get("/backups/?q=KEEP&status=success")
        self.assertContains(filtered_response, "成功备份")
        self.assertContains(filtered_response, "BAK-SAVED-KEEP")
        self.assertNotContains(filtered_response, "BAK-SAVED-HIDE")

        delete_response = self.client.post(
            f"/saved-filters/{saved_filter.id}/delete/",
            {"return_to": "/backups/?q=KEEP&status=success"},
        )

        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(SavedFilter.objects.filter(id=saved_filter.id).exists())

    def test_saved_filter_is_user_scoped(self):
        other_user = get_user_model().objects.create_user(username="other-filter", password="x")
        saved_filter = SavedFilter.objects.create(
            user=other_user,
            module="system.backup",
            filter_name="别人筛选",
            filter_json={"query": {"q": "SECRET"}},
            is_default=True,
        )
        self.client.force_login(self.user)
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)

        response = self.client.get("/backups/")
        delete_response = self.client.post(f"/saved-filters/{saved_filter.id}/delete/", {"return_to": "/backups/"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "别人筛选")
        self.assertTrue(SavedFilter.objects.filter(id=saved_filter.id).exists())
        self.assertEqual(delete_response.status_code, 302)

    def test_saved_filter_return_to_rejects_external_urls(self):
        self.client.force_login(self.user)
        saved_filter = SavedFilter.objects.create(
            user=self.user,
            module="system.backup",
            filter_name="本地筛选",
            filter_json={"query": {"q": "LOCAL"}},
        )

        save_response = self.client.post(
            "/saved-filters/save/",
            {
                "module": "system.backup",
                "filter_name": "外部跳转测试",
                "query_string": "q=KEEP",
                "return_to": "https://evil.example/phish",
            },
        )
        default_response = self.client.post(
            f"/saved-filters/{saved_filter.id}/default/",
            {"return_to": "//evil.example/phish"},
        )
        delete_response = self.client.post(
            f"/saved-filters/{saved_filter.id}/delete/",
            {"return_to": "https://evil.example/phish"},
        )

        self.assertEqual(save_response.status_code, 302)
        self.assertEqual(save_response["Location"], "/")
        self.assertEqual(default_response.status_code, 302)
        self.assertEqual(default_response["Location"], "/")
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(delete_response["Location"], "/")

    def test_audit_log_list_requires_permission(self):
        self.client.force_login(self.user)

        response = self.client.get("/audit-logs/")

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "无权限访问", status_code=403)

    @override_settings(DEBUG=False)
    def test_not_found_uses_custom_error_page(self):
        self.client.force_login(self.user)

        response = self.client.get("/missing-page/")

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "页面不存在", status_code=404)
        self.assertContains(response, "返回工作台", status_code=404)

    @override_settings(DEBUG=False)
    def test_bad_request_uses_custom_error_page(self):
        request = self.client.request().wsgi_request
        request.user = self.user

        response = bad_request_view(request)

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "请求无法处理", status_code=400)
        self.assertContains(response, "返回工作台", status_code=400)

    @override_settings(DEBUG=False)
    def test_server_error_uses_custom_error_page(self):
        request = self.client.request().wsgi_request
        request.user = self.user

        response = server_error_view(request)

        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "系统暂时无法处理", status_code=500)
        self.assertContains(response, "返回工作台", status_code=500)

    def test_audit_log_list_renders_for_permission_manager(self):
        self.client.force_login(self.user)
        _grant_permission(self.user, PermissionCode.ADMIN_PERMISSION_MANAGE)
        record_audit_log(
            action="sales_order_confirm",
            source_doc_type="sales_order",
            source_doc_id=1,
            source_doc_no="SO001",
            operator_id=self.user.id,
        )

        response = self.client.get("/audit-logs/?q=SO001")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "审计日志")
        self.assertContains(response, "sales_order_confirm")
        self.assertContains(response, "SO001")


class PendingEventProcessorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="event-user", password="x")

    def test_process_generic_event_marks_success_and_creates_message(self):
        PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:system",
            payload={"operator_id": self.user.id},
        )

        result = process_pending_events()

        self.assertTrue(result.success)
        event = PendingEvent.objects.get()
        self.assertEqual(event.status, PendingEvent.EventStatus.SUCCESS)
        self.assertEqual(SystemMessage.objects.filter(receiver=self.user).count(), 1)

    def test_process_sample_out_event_creates_document_scoped_message(self):
        customer = Customer.objects.create(customer_no="C-EVENT-SAMPLE", customer_name="事件客户")
        sample_loan = SampleLoan.objects.create(
            sample_loan_no="SL-EVENT-OUT",
            customer=customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate() + timedelta(days=7),
            status=SampleLoan.Status.OUT,
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="sample_out",
            idempotency_key="sample_out:event",
            payload={"sample_loan_id": sample_loan.id, "operator_id": self.user.id},
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="sample_loan")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, sample_loan.id)
        self.assertEqual(message.source_doc_no, sample_loan.sample_loan_no)
        self.assertEqual(message.title, "借样出库已完成")

    def test_process_sample_to_sales_event_points_message_to_sales_order(self):
        customer = Customer.objects.create(customer_no="C-EVENT-CONVERT", customer_name="转销售客户")
        sample_loan = SampleLoan.objects.create(
            sample_loan_no="SL-EVENT-CONVERT",
            customer=customer,
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate() + timedelta(days=7),
            status=SampleLoan.Status.PART_SOLD,
            created_by=self.user,
        )
        sales_order = SalesOrder.objects.create(
            sales_order_no="SO-EVENT-CONVERT",
            customer=customer,
            order_date=timezone.localdate(),
            status=SalesOrder.Status.PENDING_APPROVAL,
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="sample_to_sales",
            idempotency_key="sample_to_sales:event",
            payload={
                "sample_loan_id": sample_loan.id,
                "sales_order_id": sales_order.id,
                "operator_id": self.user.id,
            },
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="sales_order")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, sales_order.id)
        self.assertEqual(message.source_doc_no, sales_order.sales_order_no)
        self.assertEqual(message.title, "借样转销售已完成")
        self.assertIn(sample_loan.sample_loan_no, message.content)

    def test_process_production_material_issued_event_points_message_to_requisition(self):
        material = Material.objects.create(
            material_code="FG-EVENT-REQ",
            material_name="事件成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        bom = Bom.objects.create(
            bom_no="BOM-EVENT-REQ",
            finished_material=material,
            bom_version="V1",
        )
        production_order = ProductionOrder.objects.create(
            production_order_no="MO-EVENT-REQ",
            finished_material=material,
            production_qty=1,
            locked_bom=bom,
            locked_bom_version=bom.bom_version,
            created_by=self.user,
        )
        requisition = ProductionMaterialRequisition.objects.create(
            requisition_no="MR-EVENT-REQ",
            production_order=production_order,
            requisition_date=timezone.localdate(),
            status=ProductionMaterialRequisition.Status.ISSUED,
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="production_material_issued",
            idempotency_key="production_material_issued:event",
            payload={"requisition_id": requisition.id, "operator_id": self.user.id},
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="production_material_requisition")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, requisition.id)
        self.assertEqual(message.source_doc_no, requisition.requisition_no)
        self.assertEqual(message.title, "生产领料已完成")

    def test_process_production_received_event_points_message_to_receipt(self):
        material = Material.objects.create(
            material_code="FG-EVENT-PR",
            material_name="事件入库成品",
            material_type=Material.MaterialType.FINISHED,
            base_unit="pcs",
        )
        bom = Bom.objects.create(
            bom_no="BOM-EVENT-PR",
            finished_material=material,
            bom_version="V1",
        )
        production_order = ProductionOrder.objects.create(
            production_order_no="MO-EVENT-PR",
            finished_material=material,
            production_qty=1,
            locked_bom=bom,
            locked_bom_version=bom.bom_version,
            created_by=self.user,
        )
        production_receipt = ProductionReceipt.objects.create(
            production_receipt_no="PR-EVENT-PR",
            production_order=production_order,
            receipt_date=timezone.localdate(),
            status=ProductionReceipt.Status.RECEIVED,
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="production_received",
            idempotency_key="production_received:event",
            payload={"production_receipt_id": production_receipt.id, "operator_id": self.user.id},
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="production_receipt")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, production_receipt.id)
        self.assertEqual(message.source_doc_no, production_receipt.production_receipt_no)
        self.assertEqual(message.title, "生产入库已完成")

    def test_process_stock_count_adjusted_event_points_message_to_stock_count(self):
        stock_count = StockCount.objects.create(
            stock_count_no="SC-EVENT-ADJ",
            scope_type="batch",
            status=StockCount.CountStatus.ADJUSTED,
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="stock_count_adjusted",
            idempotency_key="stock_count_adjusted:event",
            payload={"stock_count_id": stock_count.id, "operator_id": self.user.id},
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="stock_count")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, stock_count.id)
        self.assertEqual(message.source_doc_no, stock_count.stock_count_no)
        self.assertEqual(message.title, "盘点调整已完成")

    def test_process_purchase_request_created_event_points_message_to_purchase_request(self):
        purchase_request = PurchaseRequest.objects.create(
            purchase_request_no="PRQ-EVENT",
            source_type=PurchaseRequest.SourceType.SHORTAGE,
            requested_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="purchase_request_created",
            idempotency_key="purchase_request_created:event",
            payload={"purchase_request_id": purchase_request.id},
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="purchase_request")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, purchase_request.id)
        self.assertEqual(message.source_doc_no, purchase_request.purchase_request_no)
        self.assertEqual(message.title, "采购需求已生成")

    def test_process_purchase_order_created_event_points_message_to_purchase_order(self):
        supplier = Supplier.objects.create(supplier_no="SUP-EVENT-PO", supplier_name="事件供应商")
        purchase_request = PurchaseRequest.objects.create(
            purchase_request_no="PRQ-EVENT-PO",
            source_type=PurchaseRequest.SourceType.SHORTAGE,
            requested_by=self.user,
        )
        purchase_order = PurchaseOrder.objects.create(
            purchase_order_no="PO-EVENT",
            supplier=supplier,
            order_date=timezone.localdate(),
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="purchase_order_created",
            idempotency_key="purchase_order_created:event",
            payload={
                "purchase_request_id": purchase_request.id,
                "purchase_order_id": purchase_order.id,
                "operator_id": self.user.id,
            },
        )

        result = process_pending_events()

        message = SystemMessage.objects.get(receiver=self.user, source_doc_type="purchase_order")
        self.assertTrue(result.success)
        self.assertEqual(message.source_doc_id, purchase_order.id)
        self.assertEqual(message.source_doc_no, purchase_order.purchase_order_no)
        self.assertEqual(message.title, "采购单已生成")
        self.assertIn(purchase_request.purchase_request_no, message.content)

    def test_process_customer_payment_events_point_messages_to_payment_documents(self):
        customer = Customer.objects.create(customer_no="C-EVENT-PAY", customer_name="事件收款客户")
        receipt = CustomerReceipt.objects.create(
            receipt_no="RC-EVENT",
            customer=customer,
            receipt_date=timezone.localdate(),
            receipt_amount=100,
            status=CustomerReceipt.Status.CONFIRMED,
            created_by=self.user,
        )
        reversal = CustomerReceiptReversal.objects.create(
            reversal_no="RCR-EVENT",
            source_receipt=receipt,
            reversal_amount=20,
            reason="测试红冲",
            status=CustomerReceiptReversal.Status.CONFIRMED,
            idempotency_key="customer-reversal-event",
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="payment_confirmed",
            idempotency_key="payment_confirmed:customer:event",
            payload={"receipt_id": receipt.id, "party": "customer", "operator_id": self.user.id},
        )
        PendingEvent.objects.create(
            event_type="payment_reversed",
            idempotency_key="payment_reversed:customer:event",
            payload={
                "receipt_id": receipt.id,
                "reversal_id": reversal.id,
                "party": "customer",
                "operator_id": self.user.id,
            },
        )

        result = process_pending_events()

        receipt_message = SystemMessage.objects.get(receiver=self.user, source_doc_type="customer_receipt")
        reversal_message = SystemMessage.objects.get(receiver=self.user, source_doc_type="customer_receipt_reversal")
        self.assertTrue(result.success)
        self.assertEqual(receipt_message.source_doc_id, receipt.id)
        self.assertEqual(receipt_message.source_doc_no, receipt.receipt_no)
        self.assertEqual(receipt_message.title, "客户收款已确认")
        self.assertEqual(reversal_message.source_doc_id, reversal.id)
        self.assertEqual(reversal_message.source_doc_no, reversal.reversal_no)
        self.assertEqual(reversal_message.title, "客户收款红冲已完成")

    def test_process_supplier_payment_events_point_messages_to_payment_documents(self):
        supplier = Supplier.objects.create(supplier_no="SUP-EVENT-PAY", supplier_name="事件付款供应商")
        payment = SupplierPayment.objects.create(
            payment_no="PY-EVENT",
            supplier=supplier,
            payment_date=timezone.localdate(),
            payment_amount=100,
            status=SupplierPayment.Status.CONFIRMED,
            created_by=self.user,
        )
        reversal = SupplierPaymentReversal.objects.create(
            reversal_no="RPY-EVENT",
            source_payment=payment,
            reversal_amount=20,
            reason="测试红冲",
            status=SupplierPaymentReversal.Status.CONFIRMED,
            idempotency_key="supplier-reversal-event",
            created_by=self.user,
        )
        PendingEvent.objects.create(
            event_type="payment_confirmed",
            idempotency_key="payment_confirmed:supplier:event",
            payload={"payment_id": payment.id, "party": "supplier", "operator_id": self.user.id},
        )
        PendingEvent.objects.create(
            event_type="payment_reversed",
            idempotency_key="payment_reversed:supplier:event",
            payload={
                "payment_id": payment.id,
                "reversal_id": reversal.id,
                "party": "supplier",
                "operator_id": self.user.id,
            },
        )

        result = process_pending_events()

        payment_message = SystemMessage.objects.get(receiver=self.user, source_doc_type="supplier_payment")
        reversal_message = SystemMessage.objects.get(receiver=self.user, source_doc_type="supplier_payment_reversal")
        self.assertTrue(result.success)
        self.assertEqual(payment_message.source_doc_id, payment.id)
        self.assertEqual(payment_message.source_doc_no, payment.payment_no)
        self.assertEqual(payment_message.title, "供应商付款已确认")
        self.assertEqual(reversal_message.source_doc_id, reversal.id)
        self.assertEqual(reversal_message.source_doc_no, reversal.reversal_no)
        self.assertEqual(reversal_message.title, "供应商付款红冲已完成")

    def test_process_pending_events_command_records_background_job(self):
        PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:command",
            payload={"operator_id": self.user.id},
        )

        management.call_command("process_pending_events", "--limit", "10")

        job = BackgroundJob.objects.get(job_type="process_pending_events")
        self.assertEqual(job.status, BackgroundJob.JobStatus.SUCCESS)
        self.assertEqual(job.result_summary["processed"], 1)

    def test_process_pending_events_command_marks_failed_and_notifies_admin(self):
        admin = get_user_model().objects.create_user(username="event-admin", password="x")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:command-fail",
            payload={"operator_id": self.user.id},
        )

        with patch("system.services._dispatch_pending_event", side_effect=RuntimeError("handler boom")):
            with self.assertRaises(CommandError):
                management.call_command("process_pending_events", "--limit", "10")

        event = PendingEvent.objects.get(idempotency_key="demo:command-fail")
        job = BackgroundJob.objects.get(job_type="process_pending_events")
        message = SystemMessage.objects.get(receiver=admin, source_doc_type="background_job", source_doc_id=job.id)
        self.assertEqual(event.status, PendingEvent.EventStatus.FAILED)
        self.assertEqual(event.last_error, "handler boom")
        self.assertEqual(job.status, BackgroundJob.JobStatus.FAILED)
        self.assertEqual(job.result_summary["failed"], 1)
        self.assertEqual(message.level, SystemMessage.Level.URGENT)
        self.assertIn(job.job_no, message.content)

    def test_due_failed_event_is_retried_and_can_succeed(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:retry-success",
            payload={"operator_id": self.user.id},
            status=PendingEvent.EventStatus.FAILED,
            retry_count=1,
            next_retry_at=timezone.now() - timedelta(minutes=1),
            last_error="temporary failure",
        )

        with patch("system.services._dispatch_pending_event", return_value={"ok": True}):
            result = process_pending_events()

        event.refresh_from_db()
        self.assertTrue(result.success)
        self.assertEqual(result.data["processed"], 1)
        self.assertEqual(event.status, PendingEvent.EventStatus.SUCCESS)
        self.assertEqual(event.retry_count, 1)
        self.assertEqual(event.last_error, "")
        self.assertIsNone(event.next_retry_at)

    def test_failed_event_before_next_retry_time_is_skipped(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:retry-future",
            payload={"operator_id": self.user.id},
            status=PendingEvent.EventStatus.FAILED,
            retry_count=1,
            next_retry_at=timezone.now() + timedelta(minutes=10),
            last_error="temporary failure",
        )

        result = process_pending_events()

        event.refresh_from_db()
        self.assertTrue(result.success)
        self.assertEqual(result.data["processed"], 0)
        self.assertEqual(result.data["failed"], 0)
        self.assertEqual(event.status, PendingEvent.EventStatus.FAILED)
        self.assertEqual(event.retry_count, 1)
        self.assertIsNotNone(event.next_retry_at)

    @override_settings(ERP_PENDING_EVENT_MAX_RETRIES=3, ERP_PENDING_EVENT_RETRY_BASE_MINUTES=7)
    def test_failure_schedules_next_retry(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:retry-scheduled",
            payload={"operator_id": self.user.id},
        )
        before = timezone.now()

        with patch("system.services._dispatch_pending_event", side_effect=RuntimeError("network timeout")):
            result = process_pending_events()

        event.refresh_from_db()
        self.assertFalse(result.success)
        self.assertEqual(result.data["failed"], 1)
        self.assertEqual(result.data["retry_scheduled"], 1)
        self.assertEqual(result.data["max_retry_exceeded"], 0)
        self.assertEqual(event.status, PendingEvent.EventStatus.FAILED)
        self.assertEqual(event.retry_count, 1)
        self.assertEqual(event.last_error, "network timeout")
        self.assertGreater(event.next_retry_at, before)
        self.assertLessEqual(event.next_retry_at, before + timedelta(minutes=8))

    @override_settings(ERP_PENDING_EVENT_MAX_RETRIES=2, ERP_PENDING_EVENT_RETRY_BASE_MINUTES=1)
    def test_failure_at_max_retry_count_stops_retrying(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:retry-max",
            payload={"operator_id": self.user.id},
            status=PendingEvent.EventStatus.FAILED,
            retry_count=1,
            next_retry_at=timezone.now() - timedelta(minutes=1),
        )

        with patch("system.services._dispatch_pending_event", side_effect=RuntimeError("still broken")):
            result = process_pending_events()

        event.refresh_from_db()
        self.assertFalse(result.success)
        self.assertEqual(result.data["failed"], 1)
        self.assertEqual(result.data["retry_scheduled"], 0)
        self.assertEqual(result.data["max_retry_exceeded"], 1)
        self.assertEqual(event.status, PendingEvent.EventStatus.FAILED)
        self.assertEqual(event.retry_count, 2)
        self.assertEqual(event.last_error, "still broken")
        self.assertIsNone(event.next_retry_at)

    @override_settings(ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES=30)
    def test_stale_running_event_is_reclaimed_and_processed(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:stale-running",
            payload={"operator_id": self.user.id},
            status=PendingEvent.EventStatus.RUNNING,
        )
        PendingEvent.objects.filter(id=event.id).update(updated_at=timezone.now() - timedelta(minutes=31))

        with patch("system.services._dispatch_pending_event", return_value={"reclaimed": True}):
            result = process_pending_events()

        event.refresh_from_db()
        self.assertTrue(result.success)
        self.assertEqual(result.data["processed"], 1)
        self.assertEqual(event.status, PendingEvent.EventStatus.SUCCESS)
        self.assertEqual(event.payload["handler_result"], {"reclaimed": True})

    @override_settings(ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES=30)
    def test_fresh_running_event_is_not_reclaimed(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:fresh-running",
            payload={"operator_id": self.user.id},
            status=PendingEvent.EventStatus.RUNNING,
        )

        result = process_pending_events()

        event.refresh_from_db()
        self.assertTrue(result.success)
        self.assertEqual(result.data["processed"], 0)
        self.assertEqual(event.status, PendingEvent.EventStatus.RUNNING)


class BackupCommandTests(TestCase):
    def test_backup_daily_creates_archive_and_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            media_dir.mkdir()
            (media_dir / "contract.txt").write_text("attachment", encoding="utf-8")
            backup_dir = Path(temp_dir) / "backups"
            with override_settings(MEDIA_ROOT=media_dir):
                result = backup_daily(backup_dir=backup_dir)

            self.assertTrue(result.success)
            backup = Backup.objects.get()
            self.assertEqual(backup.status, Backup.BackupStatus.SUCCESS)
            self.assertTrue(Path(backup.file_path).exists())
            self.assertGreater(backup.file_size, 0)
            self.assertEqual(len(backup.checksum_sha256), 64)

    def test_verify_backups_reports_missing_file(self):
        Backup.objects.create(
            backup_no="BAK-MISSING",
            backup_type="daily",
            file_path="missing.zip",
            file_size=10,
            checksum_sha256="x",
            status=Backup.BackupStatus.SUCCESS,
        )

        result = verify_backups()

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SYSTEM_BACKUP_VERIFY_FAILED")
        self.assertEqual(result.data["failed"][0]["backup_no"], "BAK-MISSING")

    def test_verify_backups_command_failure_notifies_permission_admin(self):
        admin = get_user_model().objects.create_user(username="backup-admin", password="x")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        Backup.objects.create(
            backup_no="BAK-CMD-MISSING",
            backup_type="daily",
            file_path="missing.zip",
            file_size=10,
            checksum_sha256="x",
            status=Backup.BackupStatus.SUCCESS,
        )

        with self.assertRaises(CommandError):
            management.call_command("verify_backups")

        job = BackgroundJob.objects.get(job_type="backup_verify")
        message = SystemMessage.objects.get(receiver=admin, source_doc_type="background_job", source_doc_id=job.id)
        self.assertEqual(job.status, BackgroundJob.JobStatus.FAILED)
        self.assertIn("BAK-CMD-MISSING", str(job.result_summary))
        self.assertEqual(message.level, SystemMessage.Level.URGENT)
        self.assertEqual(message.action_url, "/background-jobs/?status=failed")

    def test_restore_drill_validates_database_dump_and_attachments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            media_dir.mkdir()
            (media_dir / "contract.txt").write_text("attachment", encoding="utf-8")
            backup_dir = Path(temp_dir) / "backups"
            with override_settings(MEDIA_ROOT=media_dir):
                backup_daily(backup_dir=backup_dir)

            backup = Backup.objects.get(status=Backup.BackupStatus.SUCCESS)
            extract_dir = Path(temp_dir) / "restore-drill"

            result = restore_drill(backup_id=backup.id, extract_dir=extract_dir)

            self.assertTrue(result.success)
            self.assertGreater(result.data["object_count"], 0)
            self.assertEqual(result.data["attachment_file_count"], 1)
            self.assertTrue((extract_dir / "database.json").exists())

    def test_restore_drill_reports_invalid_database_dump(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            archive_path = backup_dir / "invalid.zip"
            database_path = backup_dir / "database.json"
            database_path.write_text('{"bad": true}', encoding="utf-8")
            import zipfile

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(database_path, "database.json")
            backup = Backup.objects.create(
                backup_no="BAK-INVALID",
                backup_type="daily",
                file_path=str(archive_path),
                file_size=archive_path.stat().st_size,
                checksum_sha256="",
                status=Backup.BackupStatus.SUCCESS,
            )
            from .backup_services import calculate_sha256

            backup.checksum_sha256 = calculate_sha256(archive_path)
            backup.save(update_fields=["checksum_sha256"])

            result = restore_drill(backup_id=backup.id)

            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "SYSTEM_BACKUP_VERIFY_FAILED")
            self.assertIn("database.json", result.message)

    def test_restore_drill_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            archive_path = backup_dir / "unsafe.zip"
            import zipfile

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("database.json", "[]")
                archive.writestr("../evil.txt", "bad")
            backup = Backup.objects.create(
                backup_no="BAK-UNSAFE",
                backup_type="daily",
                file_path=str(archive_path),
                file_size=archive_path.stat().st_size,
                checksum_sha256="",
                status=Backup.BackupStatus.SUCCESS,
            )
            from .backup_services import calculate_sha256

            backup.checksum_sha256 = calculate_sha256(archive_path)
            backup.save(update_fields=["checksum_sha256"])

            result = restore_drill(backup_id=backup.id, extract_dir=backup_dir / "restore")

            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "SYSTEM_BACKUP_VERIFY_FAILED")
            self.assertIn("不安全路径", result.message)
            self.assertFalse((backup_dir / "evil.txt").exists())

    def test_backup_daily_command_records_background_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            media_dir.mkdir()
            backup_dir = Path(temp_dir) / "backups"
            with override_settings(MEDIA_ROOT=media_dir):
                management.call_command("backup_daily", "--backup-dir", str(backup_dir))

        job = BackgroundJob.objects.get(job_type="backup")
        self.assertEqual(job.status, BackgroundJob.JobStatus.SUCCESS)
        self.assertTrue(Backup.objects.filter(status=Backup.BackupStatus.SUCCESS).exists())

    def test_restore_drill_command_records_background_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            media_dir.mkdir()
            backup_dir = Path(temp_dir) / "backups"
            with override_settings(MEDIA_ROOT=media_dir):
                backup_daily(backup_dir=backup_dir)

            backup = Backup.objects.get(status=Backup.BackupStatus.SUCCESS)
            management.call_command("restore_drill", "--backup-id", str(backup.id))

        job = BackgroundJob.objects.get(job_type="restore_drill")
        self.assertEqual(job.status, BackgroundJob.JobStatus.SUCCESS)
        self.assertEqual(job.result_summary["backup_no"], backup.backup_no)

    def test_backup_daily_command_blocks_duplicate_running_job(self):
        BackgroundJob.objects.create(
            job_no="JOB-RUNNING",
            job_type="backup",
            status=BackgroundJob.JobStatus.RUNNING,
            started_at=timezone.now(),
        )

        with self.assertRaises(CommandError):
            management.call_command("backup_daily")

    @override_settings(ERP_BACKGROUND_JOB_RUNNING_TIMEOUT_MINUTES=120)
    def test_backup_daily_command_marks_stale_running_job_failed_before_start(self):
        stale_job = BackgroundJob.objects.create(
            job_no="JOB-STALE-RUNNING",
            job_type="backup",
            status=BackgroundJob.JobStatus.RUNNING,
            started_at=timezone.now() - timedelta(minutes=121),
        )
        BackgroundJob.objects.filter(id=stale_job.id).update(started_at=timezone.now() - timedelta(minutes=121))

        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            media_dir.mkdir()
            backup_dir = Path(temp_dir) / "backups"
            with override_settings(MEDIA_ROOT=media_dir):
                management.call_command("backup_daily", "--backup-dir", str(backup_dir))

        stale_job.refresh_from_db()
        new_job = BackgroundJob.objects.exclude(id=stale_job.id).get(job_type="backup")
        self.assertEqual(stale_job.status, BackgroundJob.JobStatus.FAILED)
        self.assertTrue(stale_job.result_summary["auto_failed_by_timeout"])
        self.assertIn("运行超过 120 分钟", stale_job.error_message)
        self.assertEqual(new_job.status, BackgroundJob.JobStatus.SUCCESS)

    def test_cleanup_backups_keeps_daily_weekly_and_monthly_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            recent = _backup_record("BAK-RECENT", backup_dir, days_ago=5)
            weekly = _backup_record("BAK-WEEKLY", backup_dir, days_ago=40)
            monthly = _backup_record("BAK-MONTHLY", backup_dir, days_ago=75)
            old = _backup_record("BAK-OLD", backup_dir, days_ago=120)
            failed_old = _backup_record("BAK-FAILED", backup_dir, days_ago=45, status=Backup.BackupStatus.FAILED)

            result = cleanup_backups(
                keep_daily_days=30,
                keep_weekly=1,
                keep_monthly=1,
                keep_failed_days=30,
                backup_dir=backup_dir,
            )

            self.assertTrue(result.success)
            self.assertTrue(Backup.objects.filter(id=recent.id).exists())
            self.assertTrue(Backup.objects.filter(id=weekly.id).exists())
            self.assertTrue(Backup.objects.filter(id=monthly.id).exists())
            self.assertFalse(Backup.objects.filter(id=old.id).exists())
            self.assertFalse(Backup.objects.filter(id=failed_old.id).exists())
            self.assertFalse(Path(old.file_path).exists())
            self.assertFalse(Path(failed_old.file_path).exists())

    def test_cleanup_backups_rejects_file_path_outside_backup_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir) / "backups"
            backup_dir.mkdir()
            outside_file = Path(temp_dir) / "outside.zip"
            outside_file.write_text("do not delete", encoding="utf-8")
            backup = Backup.objects.create(
                backup_no="BAK-OUTSIDE",
                backup_type="daily",
                file_path=str(outside_file),
                file_size=outside_file.stat().st_size,
                checksum_sha256="a" * 64,
                status=Backup.BackupStatus.FAILED,
            )
            Backup.objects.filter(id=backup.id).update(created_at=timezone.now() - timedelta(days=45))

            result = cleanup_backups(keep_failed_days=30, backup_dir=backup_dir)

            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "SYSTEM_BACKUP_CLEANUP_PARTIAL_FAILED")
            self.assertTrue(outside_file.exists())
            self.assertTrue(Backup.objects.filter(id=backup.id).exists())
            self.assertEqual(result.data["file_errors"][0]["backup_no"], "BAK-OUTSIDE")

    def test_cleanup_backups_command_records_background_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir) / "backups"
            backup_dir.mkdir()
            old = _backup_record("BAK-CMD-OLD", backup_dir, days_ago=90)

            with override_settings(ERP_BACKUP_DIR=backup_dir):
                management.call_command(
                    "cleanup_backups",
                    "--keep-daily-days",
                    "30",
                    "--keep-weekly",
                    "0",
                    "--keep-monthly",
                    "0",
                )

            job = BackgroundJob.objects.get(job_type="backup_cleanup")
            self.assertEqual(job.status, BackgroundJob.JobStatus.SUCCESS)
            self.assertFalse(Backup.objects.filter(id=old.id).exists())


class ProductionPreflightCommandTests(TestCase):
    def test_preflight_reports_missing_superuser_as_failure(self):
        checks = run_preflight_checks()

        superuser_check = _check_by_name(checks, "初始超级管理员")
        self.assertFalse(superuser_check.ok)
        self.assertFalse(superuser_check.warning)

    @override_settings(
        IS_PRODUCTION=False,
        DEBUG=False,
        ALLOWED_HOSTS=["erp.example.com"],
        ERP_ATTACHMENT_SCAN_COMMAND="",
        ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY="系统管理员",
    )
    def test_preflight_passes_hard_checks_with_admin_and_role(self):
        admin = get_user_model().objects.create_superuser(username="preflight-admin", password="Secret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)

        checks = run_preflight_checks()

        hard_failures = [check for check in checks if not check.ok and not check.warning]
        self.assertEqual(hard_failures, [])
        self.assertTrue(_check_by_name(checks, "初始超级管理员").ok)
        self.assertTrue(_check_by_name(checks, "权限管理员角色").ok)
        attachment_scan_check = _check_by_name(checks, "附件安全扫描")
        self.assertTrue(attachment_scan_check.ok)
        self.assertIn("系统管理员", attachment_scan_check.message)
        self.assertTrue(_check_by_name(checks, "日志目录").ok)

    @override_settings(ERP_ATTACHMENT_SCAN_COMMAND="", ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY="")
    def test_preflight_warns_when_attachment_scan_has_no_acceptor(self):
        checks = run_preflight_checks()

        attachment_scan_check = _check_by_name(checks, "附件安全扫描")
        self.assertFalse(attachment_scan_check.ok)
        self.assertTrue(attachment_scan_check.warning)
        self.assertIn("需记录风险接受人", attachment_scan_check.message)

    @override_settings(ERP_ATTACHMENT_SCAN_COMMAND="scanner {file}", ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY="")
    def test_preflight_accepts_attachment_scan_command(self):
        checks = run_preflight_checks()

        attachment_scan_check = _check_by_name(checks, "附件安全扫描")
        self.assertTrue(attachment_scan_check.ok)
        self.assertIn("已配置扫描命令", attachment_scan_check.message)

    @override_settings(ERP_BACKGROUND_JOB_RUNNING_TIMEOUT_MINUTES=120)
    def test_preflight_warns_when_running_background_job_is_stale(self):
        BackgroundJob.objects.create(
            job_no="JOB-PREFLIGHT-STALE",
            job_type="backup",
            status=BackgroundJob.JobStatus.RUNNING,
            started_at=timezone.now() - timedelta(minutes=121),
        )

        checks = run_preflight_checks()

        stale_check = _check_by_name(checks, "卡住后台任务")
        self.assertFalse(stale_check.ok)
        self.assertTrue(stale_check.warning)
        self.assertIn("超过 120 分钟", stale_check.message)

    @override_settings(ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES=30)
    def test_preflight_warns_when_running_pending_event_is_stale(self):
        event = PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="preflight:stale-running",
            status=PendingEvent.EventStatus.RUNNING,
        )
        PendingEvent.objects.filter(id=event.id).update(updated_at=timezone.now() - timedelta(minutes=31))

        checks = run_preflight_checks()

        stale_check = _check_by_name(checks, "卡住事务后事件")
        self.assertFalse(stale_check.ok)
        self.assertTrue(stale_check.warning)
        self.assertIn("超过 30 分钟", stale_check.message)

    def test_preflight_command_allows_warnings_by_default(self):
        admin = get_user_model().objects.create_superuser(username="preflight-admin", password="Secret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)
        output = StringIO()

        management.call_command("production_preflight", stdout=output)

        self.assertIn("WARN", output.getvalue())
        self.assertIn("生产预检通过", output.getvalue())

    def test_preflight_command_strict_fails_on_warnings(self):
        admin = get_user_model().objects.create_superuser(username="preflight-admin", password="Secret123!")
        _grant_permission(admin, PermissionCode.ADMIN_PERMISSION_MANAGE)

        with self.assertRaises(CommandError):
            management.call_command("production_preflight", "--strict", stdout=StringIO())

    def test_preflight_command_fails_on_missing_admin(self):
        with self.assertRaises(CommandError):
            management.call_command("production_preflight", stdout=StringIO())

    def test_preflight_warns_when_release_gate_report_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "missing-release-gate.md"
            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_path):
                checks = run_preflight_checks()

        release_gate_check = _check_by_name(checks, "发布门禁报告")
        self.assertFalse(release_gate_check.ok)
        self.assertTrue(release_gate_check.warning)
        self.assertIn("门禁报告不存在", release_gate_check.message)

    def test_preflight_fails_when_release_gate_report_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "release-gate.md"
            _write_release_gate_report(report_path, overall_result="未通过")

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_path):
                checks = run_preflight_checks()

        release_gate_check = _check_by_name(checks, "发布门禁报告")
        self.assertFalse(release_gate_check.ok)
        self.assertFalse(release_gate_check.warning)
        self.assertIn("未通过", release_gate_check.message)

    def test_preflight_accepts_recent_passed_release_gate_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "release-gate.md"
            _write_release_gate_report(report_path, overall_result="通过")

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_path):
                checks = run_preflight_checks()

        release_gate_check = _check_by_name(checks, "发布门禁报告")
        self.assertTrue(release_gate_check.ok)
        self.assertIn("最近门禁通过", release_gate_check.message)

    def test_preflight_warns_when_release_gate_report_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "release-gate.md"
            _write_release_gate_report(
                report_path,
                overall_result="通过",
                generated_at=timezone.now() - timedelta(hours=25),
            )

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_path, ERP_RELEASE_GATE_MAX_AGE_HOURS=24):
                checks = run_preflight_checks()

        release_gate_check = _check_by_name(checks, "发布门禁报告")
        self.assertFalse(release_gate_check.ok)
        self.assertTrue(release_gate_check.warning)
        self.assertIn("已超过 24 小时", release_gate_check.message)

    def test_preflight_reports_media_inside_static_as_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            static_root = root / "staticfiles"
            media_root = static_root / "attachments"
            backup_root = root / "backups"
            log_root = root / "logs"

            with override_settings(STATIC_ROOT=static_root, MEDIA_ROOT=media_root, ERP_BACKUP_DIR=backup_root, LOG_DIR=log_root):
                checks = run_preflight_checks()

        isolation_check = _check_by_name(checks, "目录隔离")
        self.assertFalse(isolation_check.ok)
        self.assertFalse(isolation_check.warning)
        self.assertIn("附件目录不能位于静态文件目录内", isolation_check.message)

    def test_preflight_reports_log_dir_inside_static_as_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            static_root = root / "staticfiles"
            media_root = root / "media"
            backup_root = root / "backups"
            log_root = static_root / "logs"

            with override_settings(STATIC_ROOT=static_root, MEDIA_ROOT=media_root, ERP_BACKUP_DIR=backup_root, LOG_DIR=log_root):
                checks = run_preflight_checks()

        isolation_check = _check_by_name(checks, "目录隔离")
        self.assertFalse(isolation_check.ok)
        self.assertFalse(isolation_check.warning)
        self.assertIn("日志目录不能位于静态文件目录内", isolation_check.message)

    def test_preflight_reports_static_inside_media_as_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_root = root / "media"
            static_root = media_root / "staticfiles"
            backup_root = root / "backups"
            log_root = root / "logs"

            with override_settings(STATIC_ROOT=static_root, MEDIA_ROOT=media_root, ERP_BACKUP_DIR=backup_root, LOG_DIR=log_root):
                checks = run_preflight_checks()

        isolation_check = _check_by_name(checks, "目录隔离")
        self.assertFalse(isolation_check.ok)
        self.assertFalse(isolation_check.warning)
        self.assertIn("静态文件目录不能位于附件目录内", isolation_check.message)

    @override_settings(IS_PRODUCTION=True)
    def test_preflight_rejects_production_backup_inside_base_dir(self):
        backup_root = Path(settings.BASE_DIR) / "backups"

        with override_settings(ERP_BACKUP_DIR=backup_root):
            checks = run_preflight_checks()

        isolation_check = _check_by_name(checks, "目录隔离")
        self.assertFalse(isolation_check.ok)
        self.assertFalse(isolation_check.warning)
        self.assertIn("生产备份目录不能位于应用代码目录内", isolation_check.message)

    @override_settings(
        IS_PRODUCTION=True,
        DEBUG=False,
        ALLOWED_HOSTS=["erp.example.com"],
        SECURE_SSL_REDIRECT=False,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        CSRF_TRUSTED_ORIGINS=["https://erp.example.com"],
        SECURE_HSTS_SECONDS=31536000,
        ERP_ATTACHMENT_SCAN_COMMAND="scanner {file}",
    )
    def test_preflight_reports_incomplete_https_settings_as_warning(self):
        checks = run_preflight_checks()

        https_check = _check_by_name(checks, "HTTPS 安全配置")
        self.assertFalse(https_check.ok)
        self.assertTrue(https_check.warning)
        self.assertIn("DJANGO_SECURE_SSL_REDIRECT", https_check.message)

    @override_settings(
        IS_PRODUCTION=True,
        DEBUG=False,
        ALLOWED_HOSTS=["erp.example.com"],
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        CSRF_TRUSTED_ORIGINS=["https://erp.example.com"],
        SECURE_HSTS_SECONDS=31536000,
        ERP_ATTACHMENT_SCAN_COMMAND="scanner {file}",
    )
    def test_preflight_accepts_complete_https_settings(self):
        checks = run_preflight_checks()

        https_check = _check_by_name(checks, "HTTPS 安全配置")
        self.assertTrue(https_check.ok)


class ReleaseRecordCommandTests(TestCase):
    def test_record_release_creates_release_record(self):
        user = get_user_model().objects.create_user(username="release-user", password="x")
        output = StringIO()

        management.call_command(
            "record_release",
            "2026.06.11.1",
            "--summary",
            "预上线发布",
            "--released-by",
            user.username,
            stdout=output,
        )

        record = ReleaseRecord.objects.get(version_no="2026.06.11.1")
        self.assertEqual(record.released_by, user)
        self.assertEqual(record.summary, "预上线发布")
        self.assertIn("Release recorded", output.getvalue())

    def test_record_release_rejects_duplicate_version(self):
        ReleaseRecord.objects.create(version_no="2026.06.11.1", released_at=timezone.now())

        with self.assertRaises(CommandError):
            management.call_command("record_release", "2026.06.11.1", stdout=StringIO())

    def test_record_release_rejects_unknown_user(self):
        with self.assertRaises(CommandError):
            management.call_command(
                "record_release",
                "--release-version",
                "2026.06.11.2",
                "--released-by",
                "missing-user",
                stdout=StringIO(),
            )


class ReleaseGateCommandTests(TestCase):
    def test_url_reference_check_detects_missing_static_url_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "broken.html"
            template_path.write_text("{% url 'missing:route' %}\n", encoding="utf-8")

            result = check_url_references(scan_roots=[template_path])

        self.assertEqual(result.reference_count, 1)
        self.assertEqual(result.missing_references[0].url_name, "missing:route")

    def test_url_reference_check_detects_template_argument_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "broken.html"
            template_path.write_text("{% url 'files:attachment_detail' %}\n", encoding="utf-8")

            result = check_url_references(scan_roots=[template_path])

        self.assertEqual(result.reference_count, 1)
        self.assertFalse(result.missing_references)
        self.assertEqual(result.checked_template_argument_count, 1)
        self.assertEqual(result.invalid_references[0].reference.url_name, "files:attachment_detail")

    def test_url_reference_check_passes_current_project(self):
        result = check_url_references()

        self.assertGreater(result.reference_count, 0)
        self.assertFalse(result.missing_references)
        self.assertFalse(result.invalid_references)
        self.assertGreater(result.checked_template_argument_count, 0)

    def test_template_check_detects_syntax_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "broken.html"
            template_path.write_text("{% if missing %}\n", encoding="utf-8")

            result = check_templates(roots=[template_path])

        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.errors[0].template_name, "broken.html")

    def test_template_check_passes_current_project(self):
        result = check_templates()

        self.assertGreaterEqual(result.checked_count, 100)
        self.assertFalse(result.errors)

    def test_permission_check_detects_missing_default_permission(self):
        Permission.objects.filter(permission_code=PermissionCode.SALES_PROCESS).delete()

        result = check_permissions()

        self.assertIn(PermissionCode.SALES_PROCESS, result.missing_permission_codes)

    def test_permission_check_passes_current_database(self):
        result = check_permissions()

        self.assertEqual(result.expected_count, 12)
        self.assertFalse(result.missing_permission_codes)
        self.assertFalse(result.undeclared_permission_codes)

    def test_permission_reference_check_detects_unknown_static_permission_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "broken.html"
            template_path.write_text(
                '{% has_erp_perm request.user "sales.missing_permission" as can_do %}\n',
                encoding="utf-8",
            )

            result = check_permission_references(scan_roots=[template_path])

        self.assertEqual(result.reference_count, 1)
        self.assertEqual(result.unknown_references[0].permission_code, "sales.missing_permission")

    def test_permission_reference_check_passes_current_project(self):
        result = check_permission_references()

        self.assertGreater(result.reference_count, 0)
        self.assertFalse(result.unknown_references)

    def test_route_protection_check_detects_unprotected_view(self):
        from django.http import HttpResponse
        from django.urls import path
        from django.views import View

        class PublicBusinessView(View):
            def get(self, request):
                return HttpResponse("ok")

        routes = collect_unprotected_routes([path("open/", PublicBusinessView.as_view(), name="open_business")])

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].url_name, "open_business")

    def test_route_protection_check_passes_current_project(self):
        result = check_route_protection()

        self.assertGreater(result.route_count, 0)
        self.assertFalse(result.unprotected_routes)

    def test_csrf_check_detects_post_form_without_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "broken.html"
            template_path.write_text('<form method="post"><button>保存</button></form>\n', encoding="utf-8")

            result = check_csrf_tokens(scan_roots=[template_path])

        self.assertEqual(result.post_form_count, 1)
        self.assertEqual(len(result.missing_tokens), 1)

    def test_csrf_check_passes_current_templates(self):
        result = check_csrf_tokens()

        self.assertGreater(result.post_form_count, 0)
        self.assertFalse(result.missing_tokens)

    def test_navigation_path_collector_extracts_unique_local_nav_links(self):
        html = (
            '<a class="nav-link" href="/sales/orders/">销售订单</a>'
            '<a class="nav-link" href="/sales/orders/">重复</a>'
            '<a class="nav-link" href="https://example.com/offsite">外部</a>'
            '<a class="nav-link" href="/finance/reconciliations/?status=draft">对账单</a>'
        )

        paths = collect_navigation_paths(html)

        self.assertEqual(paths, ("/sales/orders/", "/finance/reconciliations/?status=draft"))

    def test_navigation_page_check_passes_current_project_for_superuser(self):
        admin = get_user_model().objects.create_superuser(
            username="nav-admin",
            password="StrongSecret123!",
            is_deleted=False,
            status="active",
        )

        result = check_navigation_pages(admin)

        self.assertGreaterEqual(result.checked_count, 30)
        self.assertFalse(result.broken_pages)

    def test_low_frequency_entrypoint_collector_finds_import_export_and_print_routes(self):
        from django.http import HttpResponse
        from django.urls import include, path
        from django.views import View

        class ExportView(View):
            def get(self, request):
                return HttpResponse("csv")

        routes = [
            path(
                "demo/",
                include(
                    (
                        [
                            path("orders/export/", ExportView.as_view(), name="order_export"),
                            path("orders/import/", ExportView.as_view(), name="order_import"),
                            path("orders/import-template/", ExportView.as_view(), name="order_import_template"),
                            path("orders/<int:pk>/print/", ExportView.as_view(), name="order_print"),
                            path("orders/", ExportView.as_view(), name="order_list"),
                        ],
                        "demo",
                    )
                ),
            )
        ]

        entrypoints = collect_low_frequency_entrypoints(routes)

        self.assertEqual({entrypoint.url_name for entrypoint in entrypoints}, {
            "demo:order_export",
            "demo:order_import",
            "demo:order_import_template",
            "demo:order_print",
        })
        self.assertEqual(
            {entrypoint.url_name: entrypoint.category for entrypoint in entrypoints},
            {
                "demo:order_export": "export",
                "demo:order_import": "import",
                "demo:order_import_template": "import_template",
                "demo:order_print": "print",
            },
        )

    def test_low_frequency_entrypoint_check_passes_current_project_for_superuser(self):
        admin = get_user_model().objects.create_superuser(
            username="entrypoint-admin",
            password="StrongSecret123!",
            is_deleted=False,
            status="active",
        )

        result = check_low_frequency_entrypoints(admin)

        self.assertGreaterEqual(result.entrypoint_count, 50)
        self.assertGreaterEqual(result.smoked_count, 40)
        self.assertFalse(result.broken_entrypoints)

    def test_release_gate_builds_default_steps_with_operator(self):
        steps = _build_gate_steps(operator="admin")

        self.assertEqual(
            [step.name for step in steps],
            [
                "Django 系统检查",
                "URL 引用完整性检查",
                "模板语法检查",
                "权限配置检查",
                "权限引用完整性检查",
                "路由保护检查",
                "CSRF 表单检查",
                "导航页面烟测",
                "低频入口烟测",
                "迁移一致性检查",
                "Python 依赖检查",
                "业务冒烟测试",
            ],
        )
        self.assertIn("--operator", steps[-1].command)
        self.assertIn("admin", steps[-1].command)

    def test_release_gate_can_include_deploy_check(self):
        steps = _build_gate_steps(include_deploy_check=True)

        self.assertEqual(steps[1].name, "Django 生产安全检查")
        self.assertEqual(steps[2].name, "URL 引用完整性检查")
        self.assertEqual(steps[3].name, "模板语法检查")
        self.assertEqual(steps[4].name, "权限配置检查")
        self.assertEqual(steps[5].name, "权限引用完整性检查")
        self.assertEqual(steps[6].name, "路由保护检查")
        self.assertEqual(steps[7].name, "CSRF 表单检查")
        self.assertEqual(steps[8].name, "导航页面烟测")
        self.assertEqual(steps[9].name, "低频入口烟测")
        self.assertIn("--deploy", steps[1].command)
        self.assertIn("--fail-level", steps[1].command)
        self.assertIn("WARNING", steps[1].command)

    def test_release_gate_can_include_tests_and_preflight(self):
        steps = _build_gate_steps(include_tests=True, include_production_preflight=True)

        self.assertEqual(steps[-2].name, "完整自动测试")
        self.assertEqual(steps[-1].name, "生产严格预检")
        self.assertIn("--skip-release-gate-report", steps[-1].command)

    @patch("system.management.commands.release_gate.subprocess.run")
    def test_release_gate_reports_success(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="ok\n", stderr="")
        output = StringIO()

        management.call_command("release_gate", stdout=output)

        self.assertEqual(mocked_run.call_count, 12)
        self.assertIn("[OK] Django 系统检查: ok", output.getvalue())
        self.assertIn("发布前门禁检查通过", output.getvalue())

    @patch("system.management.commands.release_gate.subprocess.run")
    def test_release_gate_writes_markdown_report(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="ok\n", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "release-gate.md"

            management.call_command("release_gate", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")
            self.assertIn("# ERP 发布前门禁报告", content)
            self.assertIn("总体结果：通过", content)
            self.assertIn("| Django 系统检查 | OK | ok |", content)

    @patch("system.management.commands.release_gate.subprocess.run")
    def test_release_gate_raises_on_failure(self, mocked_run):
        mocked_run.side_effect = [
            subprocess.CompletedProcess(args=["check"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["url"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["templates"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["permissions"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["permission-references"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["route-protection"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["csrf"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["navigation"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["entrypoints"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["migrations"], returncode=1, stdout="", stderr="migration failed\n"),
            subprocess.CompletedProcess(args=["pip"], returncode=0, stdout="ok\n", stderr=""),
            subprocess.CompletedProcess(args=["smoke"], returncode=0, stdout="ok\n", stderr=""),
        ]

        with self.assertRaises(CommandError):
            management.call_command("release_gate", stdout=StringIO())

    @patch("system.management.commands.release_gate.subprocess.run")
    def test_release_gate_writes_failure_report_before_raising(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(args=["x"], returncode=1, stdout="", stderr="bad\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "release-gate-failed.md"

            with self.assertRaises(CommandError):
                management.call_command("release_gate", "--report-file", str(report_path), "--fail-fast", stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")
            self.assertIn("总体结果：未通过", content)
            self.assertIn("| Django 系统检查 | FAIL | bad |", content)

    def test_release_gate_summarizes_test_result(self):
        output = "事务后事件处理完成\n----------------------------------------------------------------------\nRan 123 tests in 4.560s\n\nOK\n"

        summary = _summarize_output("完整自动测试", output, "")

        self.assertEqual(summary, "Ran 123 tests in 4.560s; OK")

    def test_release_gate_summarizes_url_reference_check(self):
        output = "URL 引用检查通过：12 个静态引用，8 个 URL 名称，5 个模板参数引用\n"

        summary = _summarize_output("URL 引用完整性检查", output, "")

        self.assertEqual(summary, "URL 引用检查通过：12 个静态引用，8 个 URL 名称，5 个模板参数引用")

    def test_release_gate_summarizes_template_check(self):
        output = "模板语法检查通过：102 个模板\n"

        summary = _summarize_output("模板语法检查", output, "")

        self.assertEqual(summary, "模板语法检查通过：102 个模板")

    def test_release_gate_summarizes_permission_check(self):
        output = "权限配置检查通过：12 个默认权限\n"

        summary = _summarize_output("权限配置检查", output, "")

        self.assertEqual(summary, "权限配置检查通过：12 个默认权限")

    def test_release_gate_summarizes_permission_reference_check(self):
        output = "权限引用检查通过：2 个静态权限引用，12 个默认权限\n"

        summary = _summarize_output("权限引用完整性检查", output, "")

        self.assertEqual(summary, "权限引用检查通过：2 个静态权限引用，12 个默认权限")

    def test_release_gate_summarizes_route_protection_check(self):
        output = "路由保护检查通过：132 个业务 URL 均有登录或权限保护\n"

        summary = _summarize_output("路由保护检查", output, "")

        self.assertEqual(summary, "路由保护检查通过：132 个业务 URL 均有登录或权限保护")

    def test_release_gate_summarizes_csrf_check(self):
        output = "CSRF 表单检查通过：88 个 POST 表单均包含 csrf_token\n"

        summary = _summarize_output("CSRF 表单检查", output, "")

        self.assertEqual(summary, "CSRF 表单检查通过：88 个 POST 表单均包含 csrf_token")

    def test_release_gate_summarizes_navigation_check(self):
        output = "导航页面烟测通过：46 个主导航页面可正常打开，用户 admin\n"

        summary = _summarize_output("导航页面烟测", output, "")

        self.assertEqual(summary, "导航页面烟测通过：46 个主导航页面可正常打开，用户 admin")

    def test_release_gate_summarizes_low_frequency_entrypoint_check(self):
        output = "低频入口烟测通过：61 个入口可反转，45 个非写入口可访问，16 个导出或对象级入口仅做反转检查，用户 admin\n"

        summary = _summarize_output("低频入口烟测", output, "")

        self.assertEqual(summary, "低频入口烟测通过：61 个入口可反转，45 个非写入口可访问，16 个导出或对象级入口仅做反转检查，用户 admin")

    def test_release_gate_report_status_parses_recent_passed_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "release-gate.md"
            _write_release_gate_report(
                report_path,
                overall_result="通过",
                step_count=4,
                step_names=["Django 系统检查", "URL 引用完整性检查", "模板语法检查", "权限配置检查"],
            )

            status = get_release_gate_report_status(report_path)

        self.assertTrue(status.exists)
        self.assertTrue(status.ok)
        self.assertTrue(status.fresh)
        self.assertEqual(status.step_count, 4)
        self.assertEqual(status.overall_result, "通过")
        self.assertEqual(status.step_names, ("Django 系统检查", "URL 引用完整性检查", "模板语法检查", "权限配置检查"))


class PrelaunchReportCommandTests(TestCase):
    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_writes_markdown(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")
            self.assertIn("# ERP 预上线验收报告", content)
            self.assertIn("当前运行环境", content)
            self.assertIn("生产模式", content)
            self.assertIn("本次发布版本", content)
            self.assertIn("部署命令清单", content)
            self.assertIn("## 结论说明", content)
            self.assertIn("当前报告运行在非生产环境", content)
            self.assertIn("剩余待处理项", content)
            self.assertIn("正式上线前必须在生产环境完成这些项目", content)
            self.assertIn("## 发布门禁", content)
            self.assertIn("检查步骤数 | 5", content)
            self.assertIn("是否完整上线门禁 | 否", content)
            self.assertIn("## 生产配置模拟", content)
            self.assertIn("## 初始化验收", content)
            self.assertIn("## 生产预检", content)
            self.assertIn("## 运维证据验收", content)
            self.assertIn("未找到成功备份记录", content)
            self.assertIn("未找到成功恢复演练记录", content)
            self.assertIn("未找到发布记录", content)
            self.assertIn("未指定部署命令清单归档文件", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_warns_when_release_gate_report_is_not_full_gate(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("是否完整上线门禁 | 否", content)
        self.assertIn("缺失门禁步骤", content)
        self.assertIn("release_gate --include-deploy-check --include-tests --include-production-preflight", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_rejects_gate_with_enough_steps_but_missing_required_step(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(
                report_gate_path,
                overall_result="通过",
                step_count=8,
                step_names=[
                    "Django 系统检查",
                    "Django 生产安全检查",
                    "迁移一致性检查",
                    "Python 依赖检查",
                    "业务冒烟测试",
                    "完整自动测试",
                    "生产严格预检",
                    "额外检查",
                ],
            )

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("检查步骤数 | 8", content)
        self.assertIn("是否完整上线门禁 | 否", content)
        self.assertIn("URL 引用完整性检查", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_strict_fails_on_warnings(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(args=["check"], returncode=1, stdout="", stderr="bad\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_gate_path = Path(temp_dir) / "missing-release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=missing_gate_path):
                with self.assertRaises(CommandError):
                    management.call_command("prelaunch_report", "--report-file", str(report_path), "--strict", stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")
            self.assertIn("总体结果：需处理", content)
            self.assertIn("门禁报告不存在", content)
            self.assertIn("bootstrap_admin --username <用户名>", content)
            self.assertIn("release_gate --include-deploy-check --include-tests --include-production-preflight", content)
            self.assertIn(
                "prelaunch_report --strict --bootstrap-username <用户名> --release-version <版本号> --deployment-runbook-file docs/deployment-runbook-<版本号>.md",
                content,
            )
            self.assertIn("simulate_production_settings --host <正式域名>", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_can_check_bootstrap_admin(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        management.call_command("bootstrap_admin", username="admin", password="StrongSecret123!", stdout=StringIO())
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("admin / permission-admin 已通过 check-only", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_accepts_operational_evidence(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        management.call_command("bootstrap_admin", username="admin", password="StrongSecret123!", stdout=StringIO())
        Backup.objects.create(
            backup_no="BAK-PRELAUNCH",
            backup_type="daily",
            file_path="backup.zip",
            file_size=100,
            checksum_sha256="a" * 64,
            status=Backup.BackupStatus.SUCCESS,
        )
        BackgroundJob.objects.create(
            job_no="JOB-RESTORE-PRELAUNCH",
            job_type="restore_drill",
            status=BackgroundJob.JobStatus.SUCCESS,
            finished_at=timezone.now(),
        )
        BackgroundJob.objects.create(
            job_no="JOB-BACKUP-VERIFY-PRELAUNCH",
            job_type="backup_verify",
            status=BackgroundJob.JobStatus.SUCCESS,
            finished_at=timezone.now(),
        )
        BackgroundJob.objects.create(
            job_no="JOB-EVENTS-PRELAUNCH",
            job_type="process_pending_events",
            status=BackgroundJob.JobStatus.SUCCESS,
            finished_at=timezone.now(),
        )
        ReleaseRecord.objects.create(version_no="2026.06.11.2", released_at=timezone.now(), summary="预上线验收")
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            runbook_path = Path(temp_dir) / "deployment-runbook.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)
            runbook_path.write_text(
                "\n".join(
                    [
                        "# ERP 生产部署命令清单",
                        "# version=2026.06.11.2",
                        "python manage.py release_gate --operator admin --include-deploy-check --include-tests --include-production-preflight",
                        "python manage.py backup_daily",
                        "python manage.py verify_backups",
                        "python manage.py restore_drill",
                        "python manage.py process_pending_events",
                        "python manage.py business_smoke_test --operator admin",
                        "python manage.py record_release 2026.06.11.2 --summary release --released-by admin",
                        "python manage.py prelaunch_report --release-version 2026.06.11.2",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path, ERP_ATTACHMENT_SCAN_RISK_ACCEPTED_BY="系统管理员"):
                management.call_command(
                    "prelaunch_report",
                    "--report-file",
                    str(report_path),
                    "--release-version",
                    "2026.06.11.2",
                    "--deployment-runbook-file",
                    str(runbook_path),
                    stdout=StringIO(),
                )

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("## 运维证据验收", content)
        self.assertIn("本次发布版本：2026.06.11.2", content)
        self.assertIn("| 最近成功备份 | OK | 通过 | BAK-PRELAUNCH", content)
        self.assertIn("| 最近备份校验 | OK | 通过 | JOB-BACKUP-VERIFY-PRELAUNCH", content)
        self.assertIn("| 最近恢复演练 | OK | 通过 | JOB-RESTORE-PRELAUNCH", content)
        self.assertIn("| 事务后事件处理 | OK | 通过 | JOB-EVENTS-PRELAUNCH", content)
        self.assertIn("| 发布记录 | OK | 通过 | 2026.06.11.2", content)
        self.assertIn("| 部署命令清单 | OK | 通过 |", content)
        self.assertIn("剩余待处理项：完整上线门禁、生产环境标记", content)
        self.assertNotIn("初始化管理员和附件扫描等项目可能按预期显示为待处理", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_warns_for_invalid_deployment_runbook(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            runbook_path = Path(temp_dir) / "deployment-runbook.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)
            runbook_path.write_text("# wrong file\n", encoding="utf-8")

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command(
                    "prelaunch_report",
                    "--report-file",
                    str(report_path),
                    "--release-version",
                    "2026.06.11.2",
                    "--deployment-runbook-file",
                    str(runbook_path),
                    stdout=StringIO(),
                )

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("部署命令清单内容不完整", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_warns_for_missing_release_version(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        ReleaseRecord.objects.create(version_no="2026.06.11.old", released_at=timezone.now(), summary="旧版本")
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command(
                    "prelaunch_report",
                    "--report-file",
                    str(report_path),
                    "--release-version",
                    "2026.06.11.new",
                    stdout=StringIO(),
                )

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("本次发布版本：2026.06.11.new", content)
        self.assertIn("未找到本次发布版本记录：2026.06.11.new", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_warns_for_stale_operational_evidence(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        stale_backup = Backup.objects.create(
            backup_no="BAK-STALE-PRELAUNCH",
            backup_type="daily",
            file_path="backup.zip",
            file_size=100,
            checksum_sha256="a" * 64,
            status=Backup.BackupStatus.SUCCESS,
        )
        Backup.objects.filter(id=stale_backup.id).update(created_at=timezone.now() - timedelta(hours=25))
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path, ERP_PRELAUNCH_BACKUP_MAX_AGE_HOURS=24):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("最近成功备份已超过 24 小时", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_warns_when_restore_drill_is_older_than_latest_backup(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        backup = Backup.objects.create(
            backup_no="BAK-NEWER-THAN-DRILL",
            backup_type="daily",
            file_path="backup.zip",
            file_size=100,
            checksum_sha256="a" * 64,
            status=Backup.BackupStatus.SUCCESS,
        )
        Backup.objects.filter(id=backup.id).update(created_at=timezone.now())
        older_drill = BackgroundJob.objects.create(
            job_no="JOB-OLD-RESTORE-DRILL",
            job_type="restore_drill",
            status=BackgroundJob.JobStatus.SUCCESS,
            finished_at=timezone.now() - timedelta(hours=1),
        )
        BackgroundJob.objects.filter(id=older_drill.id).update(created_at=timezone.now() - timedelta(hours=1))
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("最近成功恢复演练早于最近成功备份", content)

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_prelaunch_report_warns_when_pending_events_remain(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="prelaunch:pending-event",
            status=PendingEvent.EventStatus.PENDING,
        )
        BackgroundJob.objects.create(
            job_no="JOB-EVENTS-OLD",
            job_type="process_pending_events",
            status=BackgroundJob.JobStatus.SUCCESS,
            finished_at=timezone.now(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_gate_path = Path(temp_dir) / "release-gate.md"
            report_path = Path(temp_dir) / "prelaunch.md"
            _write_release_gate_report(report_gate_path, overall_result="通过", step_count=5)

            with override_settings(ERP_RELEASE_GATE_REPORT_FILE=report_gate_path):
                management.call_command("prelaunch_report", "--report-file", str(report_path), stdout=StringIO())

            content = report_path.read_text(encoding="utf-8")

        self.assertIn("存在 1 条待处理、处理中或失败事务后事件", content)


class SimulateProductionSettingsCommandTests(TestCase):
    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_simulate_production_settings_runs_deploy_check_with_production_env(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=0,
            stdout="System check identified no issues (0 silenced).\n",
            stderr="",
        )
        output = StringIO()

        management.call_command("simulate_production_settings", "--host", "erp.example.com", stdout=output)

        env = mocked_run.call_args.kwargs["env"]
        command = mocked_run.call_args.args[0]
        self.assertIn("--deploy", command)
        self.assertEqual(env["DJANGO_ENV"], "production")
        self.assertEqual(env["DJANGO_DEBUG"], "false")
        self.assertEqual(env["DB_ENGINE"], "postgres")
        self.assertEqual(env["DJANGO_ALLOWED_HOSTS"], "erp.example.com")
        self.assertEqual(env["DJANGO_CSRF_TRUSTED_ORIGINS"], "https://erp.example.com")
        self.assertIn("生产配置模拟检查通过", output.getvalue())

    @patch("system.management.commands.simulate_production_settings.subprocess.run")
    def test_simulate_production_settings_raises_on_failed_check(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            args=["check"],
            returncode=1,
            stdout="",
            stderr="SystemCheckError: bad\n",
        )

        with self.assertRaises(CommandError):
            management.call_command("simulate_production_settings", stdout=StringIO())


class DeploymentRunbookCommandTests(TestCase):
    def test_deployment_runbook_outputs_ordered_linux_commands(self):
        output = StringIO()

        management.call_command(
            "deployment_runbook",
            "--host",
            "erp.example.com",
            "--operator",
            "admin",
            "--release-version",
            "2026.06.11.1",
            "--summary",
            "首次发布",
            stdout=output,
        )

        content = output.getvalue()
        self.assertIn("python manage.py migrate", content)
        self.assertIn("python manage.py bootstrap_admin --username admin --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput", content)
        self.assertIn("python manage.py bootstrap_admin --username admin --check-only", content)
        self.assertIn("python manage.py release_gate --operator admin --include-deploy-check --include-tests --include-production-preflight", content)
        self.assertIn("python manage.py backup_daily", content)
        self.assertIn("python manage.py verify_backups", content)
        self.assertIn("python manage.py restore_drill", content)
        self.assertIn("python manage.py process_pending_events", content)
        self.assertIn("python manage.py record_release 2026.06.11.1 --summary", content)
        self.assertIn("首次发布", content)
        self.assertIn("python manage.py prelaunch_report --strict --bootstrap-username admin --release-version 2026.06.11.1", content)
        self.assertLess(content.index("backup_daily"), content.index("restore_drill"))
        self.assertLess(content.index("backup_daily"), content.index("verify_backups"))
        self.assertLess(content.index("verify_backups"), content.index("restore_drill"))
        self.assertLess(content.index("restore_drill"), content.index("process_pending_events"))
        self.assertLess(content.index("process_pending_events"), content.index("business_smoke_test"))
        self.assertLess(content.index("record_release"), content.index("prelaunch_report"))

    def test_deployment_runbook_quotes_shell_arguments(self):
        commands = build_deployment_runbook(
            host="erp internal.example.com",
            operator="release admin",
            version="2026.06.11.1",
            summary='release "candidate"',
            bootstrap_username="root admin",
            windows=False,
        )

        joined = "\n".join(commands)
        self.assertIn("'erp internal.example.com'", joined)
        self.assertIn("'release admin'", joined)
        self.assertIn("'root admin'", joined)
        self.assertIn("'release \"candidate\"'", joined)

    def test_deployment_runbook_can_output_windows_commands(self):
        commands = build_deployment_runbook(
            host="erp.example.com",
            operator="admin",
            version="2026.06.11.1",
            summary="生产发布",
            bootstrap_username="root-admin",
            windows=True,
        )

        joined = "\n".join(commands)
        self.assertIn(".\\.venv\\Scripts\\python manage.py migrate", joined)
        self.assertIn(".\\.venv\\Scripts\\python manage.py bootstrap_admin --username root-admin --password-env ERP_BOOTSTRAP_ADMIN_PASSWORD --noinput", joined)
        self.assertIn("docs\\latest-release-gate-report.md", joined)
        self.assertIn("docs\\prelaunch-acceptance-report.md", joined)
        self.assertIn(".\\.venv\\Scripts\\python manage.py verify_backups", joined)
        self.assertIn(".\\.venv\\Scripts\\python manage.py process_pending_events", joined)
        self.assertIn("--bootstrap-username root-admin", joined)

    def test_deployment_runbook_can_write_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "deployment-runbook.md"
            output = StringIO()

            management.call_command(
                "deployment_runbook",
                "--host",
                "erp.example.com",
                "--operator",
                "admin",
                "--release-version",
                "2026.06.11.1",
                "--output-file",
                str(output_path),
                stdout=output,
            )

            content = output_path.read_text(encoding="utf-8")

        self.assertIn("部署命令清单已生成", output.getvalue())
        self.assertIn("# ERP 生产部署命令清单", content)
        self.assertIn("python manage.py prelaunch_report --strict", content)
        self.assertIn("--deployment-runbook-file", content)
        self.assertIn(str(output_path), content)


class BusinessSmokeTestCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="smoke-admin", password="x")

    def test_business_smoke_test_rolls_back_by_default(self):
        output = StringIO()

        management.call_command("business_smoke_test", "--operator", self.user.username, stdout=output)

        self.assertIn("业务冒烟测试通过", output.getvalue())
        self.assertIn("已回滚冒烟数据", output.getvalue())
        self.assertFalse(Material.objects.filter(material_code__startswith="SMK-").exists())
        self.assertFalse(SalesOrder.objects.filter(sales_order_no__startswith="SMK-").exists())

    def test_business_smoke_test_commit_keeps_trace_data(self):
        output = StringIO()

        management.call_command("business_smoke_test", "--operator", self.user.username, "--commit", stdout=output)

        self.assertIn("已保留冒烟数据", output.getvalue())
        self.assertIn("借样出库归还与转销售", output.getvalue())
        self.assertIn("客户收款核销与红冲", output.getvalue())
        self.assertIn("供应商付款核销与红冲", output.getvalue())
        self.assertTrue(SalesOrder.objects.filter(sales_order_no__startswith="SMK-SO-STOCK-").exists())
        self.assertTrue(SalesOrder.objects.filter(sales_order_no__startswith="SMK-SO-BOM-").exists())
        self.assertTrue(SalesOrder.objects.filter(remark__startswith="由借样单 SMK-SL-").exists())
        self.assertTrue(ShortageAlert.objects.filter(status=ShortageAlert.Status.KITTED).exists())
        self.assertTrue(InventoryBatch.objects.filter(batch_no__startswith="BA").exists())
        self.assertTrue(SampleLoan.objects.filter(sample_loan_no__startswith="SMK-SL-", status=SampleLoan.Status.PART_SOLD).exists())
        self.assertTrue(SampleLoanReturn.objects.filter(sample_return_no__startswith="SMK-SR-", status=SampleLoanReturn.Status.RECEIVED).exists())
        self.assertTrue(CustomerReceipt.objects.filter(receipt_no__startswith="SMK-RC-", status=CustomerReceipt.Status.PART_REVERSED).exists())
        self.assertTrue(CustomerReceiptAllocation.objects.filter(allocated_amount__lt=0).exists())
        self.assertTrue(CustomerReceiptReversal.objects.filter(reversal_no__startswith="RCR").exists())
        self.assertTrue(CustomerCreditBalance.objects.filter(status=CustomerCreditBalance.Status.CLOSED, remaining_amount=0).exists())
        self.assertTrue(SupplierPayment.objects.filter(payment_no__startswith="SMK-PY-", status=SupplierPayment.Status.PART_REVERSED).exists())
        self.assertTrue(SupplierPaymentAllocation.objects.filter(allocated_amount__lt=0).exists())
        self.assertTrue(SupplierPaymentReversal.objects.filter(reversal_no__startswith="RPY").exists())
        self.assertTrue(SupplierCreditBalance.objects.filter(status=SupplierCreditBalance.Status.CLOSED, remaining_amount=0).exists())


def _grant_permission(user, permission_code: str):
    permission, _ = Permission.objects.get_or_create(
        permission_code=permission_code,
        defaults={"permission_name": permission_code, "permission_type": Permission.PermissionType.ACTION},
    )
    role = Role.objects.create(role_code=f"role-{permission_code}-{user.id}", role_name=permission_code)
    role.permissions.add(permission)
    user.roles.add(role)
    return role


def _check_by_name(checks, name: str):
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"未找到预检项：{name}")


class ProductionSettingsGuardTests(TestCase):
    def test_production_settings_reject_debug_true(self):
        result = _import_settings_with_env(
            {
                "DJANGO_ENV": "production",
                "DJANGO_DEBUG": "true",
                "DJANGO_SECRET_KEY": "production-secret-key-with-more-than-50-characters-1234567890",
                "DJANGO_ALLOWED_HOSTS": "erp.example.com",
                "DB_ENGINE": "postgres",
                "POSTGRES_PASSWORD": "dummy",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_DEBUG must be false", result.stderr)

    def test_production_settings_reject_empty_postgres_password(self):
        result = _import_settings_with_env(
            {
                "DJANGO_ENV": "production",
                "DJANGO_DEBUG": "false",
                "DJANGO_SECRET_KEY": "production-secret-key-with-more-than-50-characters-1234567890",
                "DJANGO_ALLOWED_HOSTS": "erp.example.com",
                "DB_ENGINE": "postgres",
                "POSTGRES_PASSWORD": "",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("POSTGRES_PASSWORD must be set", result.stderr)

    def test_relative_path_environment_values_are_resolved_under_base_dir(self):
        result = _import_settings_with_env(
            {
                "DJANGO_ENV": "development",
                "DJANGO_STATIC_ROOT": "staticfiles-test",
                "ERP_ATTACHMENT_DIR": "media-test",
                "ERP_BACKUP_DIR": "backups-test",
                "DJANGO_LOG_DIR": "logs-test",
                "ERP_RELEASE_GATE_REPORT_FILE": "docs/test-release-gate.md",
            },
            script=(
                "from config import settings; "
                "print(settings.STATIC_ROOT); "
                "print(settings.MEDIA_ROOT); "
                "print(settings.ERP_BACKUP_DIR); "
                "print(settings.LOG_DIR); "
                "print(settings.ERP_RELEASE_GATE_REPORT_FILE)"
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.replace("\\", "/")
        base_dir = str(settings.BASE_DIR).replace("\\", "/")
        self.assertIn(f"{base_dir}/staticfiles-test", output)
        self.assertIn(f"{base_dir}/media-test", output)
        self.assertIn(f"{base_dir}/backups-test", output)
        self.assertIn(f"{base_dir}/logs-test", output)
        self.assertIn(f"{base_dir}/docs/test-release-gate.md", output)


def _import_settings_with_env(env_overrides: dict[str, str], script: str = "import config.settings") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _backup_record(backup_no: str, backup_dir: Path, days_ago: int, status: str = Backup.BackupStatus.SUCCESS) -> Backup:
    file_path = backup_dir / f"{backup_no}.zip"
    file_path.write_text(backup_no, encoding="utf-8")
    backup = Backup.objects.create(
        backup_no=backup_no,
        backup_type="daily",
        file_path=str(file_path),
        file_size=file_path.stat().st_size,
        checksum_sha256="a" * 64,
        status=status,
    )
    Backup.objects.filter(id=backup.id).update(created_at=timezone.now() - timedelta(days=days_ago))
    backup.refresh_from_db()
    return backup


def _write_release_gate_report(
    report_path: Path,
    overall_result: str,
    generated_at=None,
    step_count: int = 4,
    step_names: list[str] | None = None,
) -> None:
    generated_at = generated_at or timezone.now()
    generated_at_text = timezone.localtime(generated_at).strftime("%Y-%m-%d %H:%M:%S")
    step_names = step_names or ["Django 系统检查"]
    step_rows = [f"| {step_name} | OK | ok | `python manage.py check` |" for step_name in step_names]
    report_path.write_text(
        "\n".join(
            [
                "# ERP 发布前门禁报告",
                "",
                f"- 生成时间：{generated_at_text}",
                f"- 总体结果：{overall_result}",
                f"- 检查步骤数：{step_count}",
                "",
                "| 步骤 | 结果 | 摘要 | 命令 |",
                "| --- | --- | --- | --- |",
                *step_rows,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
