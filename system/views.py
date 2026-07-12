from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import connection
from django.db.models import Count, Q, Sum
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import TemplateView

from accounts.permissions import ErpPermissionRequiredMixin, PermissionCode, user_has_any_permission, user_has_permission
from inventory.models import InventoryBatch
from notifications.models import SystemMessage
from notifications.services import refresh_due_snoozed_messages
from purchase.models import PurchaseReceipt, PurchaseRequest
from sales.models import SalesOrder, ShortageAlert
from system.models import AuditLog, BackgroundJob, Backup, PendingEvent, ReleaseRecord, SavedFilter
from system.release_gate_status import get_release_gate_report_status
from system.view_helpers import ErpListView, filter_json_from_query_string
from system.version import get_app_version


def _safe_return_to(request) -> str:
    return_to = request.POST.get("return_to") or "/"
    if url_has_allowed_host_and_scheme(
        return_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return return_to
    return "/"


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        refresh_due_snoozed_messages(user.id)
        active_messages = SystemMessage.objects.filter(receiver=user).exclude(
            status=SystemMessage.Status.SNOOZED,
            snoozed_until__gt=timezone.now(),
        )
        sales_orders = _dashboard_sales_orders_for_user(SalesOrder.objects.all(), user)
        shortages = _dashboard_shortages_for_user(ShortageAlert.objects.all(), user)
        can_view_purchase_dashboard = user_has_any_permission(user, (PermissionCode.PURCHASE_VIEW, PermissionCode.PURCHASE_PROCESS))
        can_view_inventory_dashboard = user_has_any_permission(user, (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS))
        pending_purchase_requests = 0
        pending_purchase_receipts = 0
        inventory_qty = 0
        if can_view_purchase_dashboard:
            pending_purchase_requests = PurchaseRequest.objects.filter(
                status=PurchaseRequest.Status.PENDING_APPROVAL
            ).count()
            pending_purchase_receipts = PurchaseReceipt.objects.filter(
                status=PurchaseReceipt.Status.PENDING_RECEIVE
            ).count()
        if can_view_inventory_dashboard:
            inventory_qty = (
                InventoryBatch.objects.filter(batch_status=InventoryBatch.BatchStatus.IN_STOCK).aggregate(
                    total=Sum("remaining_qty")
                )["total"]
                or 0
            )
        context.update(
            {
                "page_title": "工作台",
                "unread_messages": active_messages.filter(status=SystemMessage.Status.UNREAD).count(),
                "pending_sales_orders": sales_orders.filter(status=SalesOrder.Status.PENDING_APPROVAL).count(),
                "active_shortages": shortages.filter(
                    status__in=[
                        ShortageAlert.Status.UNPROCESSED,
                        ShortageAlert.Status.PURCHASE_REQUESTED,
                        ShortageAlert.Status.PARTIAL_RECEIVED,
                    ]
                ).count(),
                "can_view_purchase_dashboard": can_view_purchase_dashboard,
                "can_view_inventory_dashboard": can_view_inventory_dashboard,
                "pending_purchase_requests": pending_purchase_requests,
                "pending_purchase_receipts": pending_purchase_receipts,
                "inventory_qty": inventory_qty,
                "recent_messages": active_messages.order_by("-created_at")[:8],
                "recent_sales_orders": sales_orders.select_related("customer").order_by("-created_at")[:8],
                "shortages": shortages.select_related("sales_order", "material").order_by("-created_at")[:8],
            }
        )
        return context


class HealthCheckView(LoginRequiredMixin, ErpPermissionRequiredMixin, TemplateView):
    template_name = "system/health_check.html"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少系统健康检查权限"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        backup_dir = Path(getattr(settings, "ERP_BACKUP_DIR", settings.BASE_DIR / "backups"))
        context.update(
            {
                "page_title": "系统健康检查",
                "database_status": _check_database(),
                "media_status": _check_directory_writable(Path(settings.MEDIA_ROOT)),
                "backup_status": _check_directory_writable(backup_dir),
                "current_version": get_app_version(),
                "media_root": settings.MEDIA_ROOT,
                "backup_dir": backup_dir,
                "latest_backup": Backup.objects.order_by("-created_at").first(),
                "latest_release": ReleaseRecord.objects.order_by("-released_at").first(),
                "latest_job": BackgroundJob.objects.order_by("-created_at").first(),
                "failed_jobs": BackgroundJob.objects.filter(status=BackgroundJob.JobStatus.FAILED).order_by("-created_at")[:5],
                "stale_running_job_count": _stale_running_job_count(),
                "background_job_running_timeout_minutes": _background_job_running_timeout_minutes(),
                "pending_event_counts": _pending_event_counts(),
                "stale_running_event_count": _stale_running_event_count(),
                "pending_event_running_timeout_minutes": _pending_event_running_timeout_minutes(),
                "failed_events": PendingEvent.objects.filter(status=PendingEvent.EventStatus.FAILED).order_by("-updated_at")[:5],
                "release_gate_status": get_release_gate_report_status(),
            }
        )
        return context


class AuditLogListView(ErpPermissionRequiredMixin, ErpListView):
    model = AuditLog
    page_title = "审计日志"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看审计日志权限"
    columns = (
        ("日志号", "log_no"),
        ("操作", "action"),
        ("来源类型", "source_doc_type"),
        ("来源单号", "source_doc_no"),
        ("操作人", "operator.username"),
        ("操作时间", "created_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("log_no", "action", "source_doc_type", "source_doc_no", "operator__username")
    field_filters = (
        {"label": "日志号", "param": "log_no", "field": "log_no", "placeholder": "日志号"},
        {"label": "操作", "param": "action", "field": "action", "placeholder": "操作"},
        {"label": "来源类型", "param": "source_doc_type", "field": "source_doc_type", "placeholder": "来源类型"},
        {"label": "来源单号", "param": "source_doc_no", "field": "source_doc_no", "placeholder": "来源单号"},
        {"label": "操作人", "param": "operator", "field": "operator__username", "placeholder": "操作人账号"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("operator")


class BackupListView(ErpPermissionRequiredMixin, ErpListView):
    model = Backup
    page_title = "备份记录"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看备份记录权限"
    columns = (
        ("备份号", "backup_no"),
        ("类型", "backup_type"),
        ("状态", "get_status_display"),
        ("文件大小", "file_size"),
        ("创建人", "created_by.username"),
        ("创建时间", "created_at"),
    )
    ordering = ["-created_at"]
    search_fields = ("backup_no", "backup_type", "file_path", "created_by__username")
    status_filter_field = "status"
    field_filters = (
        {"label": "备份号", "param": "backup_no", "field": "backup_no", "placeholder": "备份号"},
        {"label": "类型", "param": "backup_type", "field": "backup_type", "placeholder": "备份类型"},
        {"label": "创建人", "param": "created_by", "field": "created_by__username", "placeholder": "创建人账号"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("created_by")


class BackgroundJobListView(ErpPermissionRequiredMixin, ErpListView):
    model = BackgroundJob
    page_title = "后台任务"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看后台任务权限"
    columns = (
        ("任务号", "job_no"),
        ("类型", "job_type"),
        ("触发来源", "trigger_type"),
        ("状态", "get_status_display"),
        ("开始时间", "started_at"),
        ("结束时间", "finished_at"),
        ("错误", "error_message"),
    )
    ordering = ["-created_at"]
    search_fields = ("job_no", "job_type", "trigger_type", "error_message")
    status_filter_field = "status"
    field_filters = (
        {"label": "任务号", "param": "job_no", "field": "job_no", "placeholder": "任务号"},
        {"label": "类型", "param": "job_type", "field": "job_type", "placeholder": "任务类型"},
        {"label": "触发来源", "param": "trigger_type", "field": "trigger_type", "placeholder": "触发来源"},
        {"label": "错误", "param": "error_message", "field": "error_message", "placeholder": "错误信息"},
    )


class ReleaseRecordListView(ErpPermissionRequiredMixin, ErpListView):
    model = ReleaseRecord
    page_title = "发布记录"
    permission_required = PermissionCode.ADMIN_PERMISSION_MANAGE
    permission_denied_message = "缺少查看发布记录权限"
    columns = (
        ("版本号", "version_no"),
        ("发布时间", "released_at"),
        ("发布人", "released_by.username"),
        ("摘要", "summary"),
    )
    ordering = ["-released_at"]
    search_fields = ("version_no", "summary", "released_by__username")
    field_filters = (
        {"label": "版本号", "param": "version_no", "field": "version_no", "placeholder": "版本号"},
        {"label": "发布人", "param": "released_by", "field": "released_by__username", "placeholder": "发布人账号"},
        {"label": "摘要", "param": "summary", "field": "summary", "placeholder": "摘要"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("released_by")


class SavedFilterSaveView(LoginRequiredMixin, View):
    def post(self, request):
        module = request.POST.get("module", "").strip()
        filter_name = request.POST.get("filter_name", "").strip()
        query_string = request.POST.get("query_string", "").strip()
        return_to = _safe_return_to(request)
        is_default = request.POST.get("is_default") == "on"
        if not module or not filter_name:
            messages.error(request, "筛选名称不能为空")
            return redirect(return_to)

        saved_filter, _created = SavedFilter.objects.update_or_create(
            user=request.user,
            module=module,
            filter_name=filter_name,
            defaults={
                "filter_json": filter_json_from_query_string(query_string),
                "is_default": is_default,
            },
        )
        if is_default:
            SavedFilter.objects.filter(user=request.user, module=module).exclude(id=saved_filter.id).update(is_default=False)
        messages.success(request, "筛选条件已保存")
        return redirect(return_to)


class SavedFilterDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        return_to = _safe_return_to(request)
        deleted, _ = SavedFilter.objects.filter(id=pk, user=request.user).delete()
        if deleted:
            messages.success(request, "筛选条件已删除")
        else:
            messages.error(request, "筛选条件不存在")
        return redirect(return_to)


class SavedFilterSetDefaultView(LoginRequiredMixin, View):
    def post(self, request, pk):
        return_to = _safe_return_to(request)
        saved_filter = SavedFilter.objects.filter(id=pk, user=request.user).first()
        if saved_filter is None:
            messages.error(request, "筛选条件不存在")
            return redirect(return_to)
        SavedFilter.objects.filter(user=request.user, module=saved_filter.module).update(is_default=False)
        saved_filter.is_default = True
        saved_filter.save(update_fields=["is_default"])
        messages.success(request, "默认筛选已更新")
        return redirect(return_to)


def permission_denied_view(request, exception=None):
    message = str(exception) if exception else "你没有权限访问此页面"
    return render(
        request,
        "errors/403.html",
        {"page_title": "无权限", "error_message": message or "你没有权限访问此页面"},
        status=403,
    )


def page_not_found_view(request, exception=None):
    return render(
        request,
        "errors/404.html",
        {"page_title": "页面不存在"},
        status=404,
    )


def bad_request_view(request, exception=None):
    return render(
        request,
        "errors/400.html",
        {"page_title": "请求无法处理"},
        status=400,
    )


def server_error_view(request):
    return render(
        request,
        "errors/500.html",
        {"page_title": "系统异常"},
        status=500,
    )


def _dashboard_sales_orders_for_user(queryset, user):
    if getattr(user, "is_superuser", False) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(created_by=user)).distinct()


def _dashboard_shortages_for_user(queryset, user):
    if getattr(user, "is_superuser", False) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset
    return queryset.filter(Q(sales_order__customer__sales_owner=user) | Q(sales_order__created_by=user)).distinct()


def _check_database() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"ok": True, "message": "正常"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def _check_directory_writable(directory: Path) -> dict:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".erp_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"ok": True, "message": "可写"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def _pending_event_counts() -> dict[str, int]:
    counts = {status: 0 for status in PendingEvent.EventStatus.values}
    rows = PendingEvent.objects.values("status").annotate(total=Count("id"))
    for row in rows:
        counts[row["status"]] = row["total"]
    return counts


def _pending_event_running_timeout_minutes() -> int:
    return max(1, int(getattr(settings, "ERP_PENDING_EVENT_RUNNING_TIMEOUT_MINUTES", 30)))


def _stale_running_event_count() -> int:
    stale_before = timezone.now() - timedelta(minutes=_pending_event_running_timeout_minutes())
    return PendingEvent.objects.filter(
        status=PendingEvent.EventStatus.RUNNING,
        updated_at__lte=stale_before,
    ).count()


def _background_job_running_timeout_minutes() -> int:
    return max(1, int(getattr(settings, "ERP_BACKGROUND_JOB_RUNNING_TIMEOUT_MINUTES", 120)))


def _stale_running_job_count() -> int:
    stale_before = timezone.now() - timedelta(minutes=_background_job_running_timeout_minutes())
    return BackgroundJob.objects.filter(
        status=BackgroundJob.JobStatus.RUNNING,
        started_at__lte=stale_before,
    ).count()
