import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from decimal import Decimal, InvalidOperation
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import CreateView, UpdateView

from accounts.permissions import PermissionCode, require_any_erp_permission, require_erp_permission, user_has_permission
from files.services import csv_upload_validation_error, export_queryset_to_csv, record_print_log, uploaded_csv_text_file
from files.view_helpers import build_attachment_panel, export_file_response
from masterdata.models import Material
from system.display import set_form_labels
from system.services import record_audit_log_from_request
from system.view_helpers import ErpListView, require_post_reason, require_second_verify

from .forms import InitialInventoryManualForm, LocationTransferForm, StockCountForm
from .import_services import (
    INITIAL_INVENTORY_IMPORT_TEMPLATE_ROWS,
    WAREHOUSE_LOCATION_IMPORT_TEMPLATE_ROWS,
    cancel_initial_inventory_import,
    confirm_initial_inventory_import,
    import_warehouse_locations_from_csv,
    preview_initial_inventory_rows,
    preview_initial_inventory_from_csv,
)
from .models import Inventory, InventoryBatch, InventoryTransaction, LocationTransfer, StockCount, StockCountItem, WarehouseLocation
from .services import confirm_location_transfer, confirm_stock_count_adjustment, create_stock_count_from_batches


class WarehouseLocationListView(ErpListView):
    model = WarehouseLocation
    page_title = "库位"
    create_url_name = "inventory:warehouse_location_create"
    create_permission_required = PermissionCode.INVENTORY_PROCESS
    view_permission_required = (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS)
    permission_denied_message = "缺少库存数据查看权限"
    detail_url_name = "inventory:warehouse_location_detail"
    columns = (
        ("库位编码", "location_code"),
        ("库位名称", "location_name"),
        ("状态", "get_status_display"),
    )
    ordering = ["location_code"]
    page_actions = (
        ("导出CSV", "inventory:warehouse_location_export", ""),
        ("下载导入模板", "inventory:warehouse_location_import_template", ""),
        ("导入CSV", "inventory:warehouse_location_import", "primary"),
    )
    page_action_permissions = {
        "inventory:warehouse_location_import_template": PermissionCode.INVENTORY_PROCESS,
        "inventory:warehouse_location_import": PermissionCode.INVENTORY_PROCESS,
    }
    search_fields = ("location_code", "location_name")
    status_filter_field = "status"


class WarehouseLocationCreateView(LoginRequiredMixin, CreateView):
    model = WarehouseLocation
    template_name = "inventory/warehouse_location_form.html"
    fields = ["location_code", "location_name", "status", "remark"]

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建库位"
        return context

    def form_valid(self, form):
        messages.success(self.request, "库位已创建")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/inventory/locations/{self.object.pk}/"


class WarehouseLocationUpdateView(LoginRequiredMixin, UpdateView):
    model = WarehouseLocation
    template_name = "inventory/warehouse_location_form.html"
    fields = WarehouseLocationCreateView.fields

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        set_form_labels(form)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"编辑库位 {self.object.location_code}"
        context["location"] = self.object
        context["is_edit"] = True
        return context

    def form_valid(self, form):
        messages.success(self.request, "库位已更新")
        return super().form_valid(form)

    def get_success_url(self):
        return f"/inventory/locations/{self.object.pk}/"


class WarehouseLocationDetailView(LoginRequiredMixin, DetailView):
    model = WarehouseLocation
    template_name = "inventory/warehouse_location_detail.html"
    context_object_name = "location"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, WarehouseLocationListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"库位 {self.object.location_code}"
        context["can_process_inventory"] = user_has_permission(self.request.user, PermissionCode.INVENTORY_PROCESS)
        return context


class WarehouseLocationImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(WAREHOUSE_LOCATION_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="warehouse_location_import_template.csv"'
        return response


class WarehouseLocationImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入库位"
        context["list_url_name"] = "inventory:warehouse_location_list"
        context["template_url_name"] = "inventory:warehouse_location_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("inventory:warehouse_location_import")

        text_file = uploaded_csv_text_file(upload)
        result = import_warehouse_locations_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 行")
            return redirect("inventory:warehouse_location_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class InventoryListView(ErpListView):
    model = Inventory
    page_title = "库存汇总"
    view_permission_required = (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS)
    permission_denied_message = "缺少库存数据查看权限"
    detail_url_name = "inventory:inventory_detail"
    columns = (
        ("物料", "material.material_code"),
        ("库位", "location.location_code"),
        ("库存类型", "get_inventory_type_display"),
        ("数量", "qty"),
    )
    ordering = ["material_id", "location_id"]
    page_actions = (
        ("导出CSV", "inventory:inventory_export", ""),
        ("手工期初建账", "inventory:initial_inventory_manual", "primary"),
        ("下载期初模板", "inventory:initial_inventory_import_template", ""),
        ("导入期初库存", "inventory:initial_inventory_import", ""),
    )
    page_action_permissions = {
        "inventory:initial_inventory_manual": PermissionCode.INVENTORY_PROCESS,
        "inventory:initial_inventory_import_template": PermissionCode.INVENTORY_PROCESS,
        "inventory:initial_inventory_import": PermissionCode.INVENTORY_PROCESS,
    }
    search_fields = ("material__material_code", "material__material_name", "location__location_code", "location__location_name")


class InventoryDetailView(LoginRequiredMixin, DetailView):
    model = Inventory
    template_name = "inventory/inventory_detail.html"
    context_object_name = "inventory"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, InventoryListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("material", "location")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"库存汇总 {self.object.material.material_code}"
        context["batches"] = (
            InventoryBatch.objects.filter(
                material=self.object.material,
                location=self.object.location,
                inventory_type=self.object.inventory_type,
            )
            .select_related("material", "location")
            .order_by("batch_status", "received_at", "batch_no")
        )
        context["transactions"] = (
            InventoryTransaction.objects.filter(
                material=self.object.material,
                location=self.object.location,
            )
            .select_related("batch", "created_by")
            .order_by("-created_at")[:50]
        )
        return context


class InventoryBatchListView(ErpListView):
    model = InventoryBatch
    page_title = "库存批次"
    view_permission_required = (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS)
    permission_denied_message = "缺少库存数据查看权限"
    detail_url_name = "inventory:inventory_batch_detail"
    columns = (
        ("批次号", "batch_no"),
        ("物料", "material.material_code"),
        ("库位", "location.location_code"),
        ("库存类型", "get_inventory_type_display"),
        ("剩余数量", "remaining_qty"),
        ("状态", "get_batch_status_display"),
    )
    ordering = ["material_id", "location_id", "received_at"]
    page_actions = (("导出CSV", "inventory:inventory_batch_export", ""),)
    search_fields = ("batch_no", "material__material_code", "material__material_name", "location__location_code")
    status_filter_field = "batch_status"
    sortable_fields = {
        "batch_no": "batch_no",
        "material.material_code": "material__material_code",
        "location.location_code": "location__location_code",
        "get_inventory_type_display": "inventory_type",
        "remaining_qty": "remaining_qty",
        "get_batch_status_display": "batch_status",
    }


class InventoryBatchDetailView(LoginRequiredMixin, DetailView):
    model = InventoryBatch
    template_name = "inventory/inventory_batch_detail.html"
    context_object_name = "batch"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, InventoryBatchListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("material", "location")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"库存批次 {self.object.batch_no}"
        context["transactions"] = (
            InventoryTransaction.objects.filter(batch=self.object)
            .select_related("material", "location", "created_by")
            .order_by("-created_at")[:30]
        )
        return context


class InventoryTransactionListView(ErpListView):
    model = InventoryTransaction
    page_title = "库存流水"
    view_permission_required = (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS)
    permission_denied_message = "缺少库存数据查看权限"
    detail_url_name = "inventory:inventory_transaction_detail"
    columns = (
        ("流水号", "transaction_no"),
        ("类型", "get_transaction_type_display"),
        ("物料", "material.material_code"),
        ("库位", "location.location_code"),
        ("数量变化", "qty_delta"),
        ("创建时间", "created_at"),
    )
    ordering = ["-created_at"]
    page_actions = (("导出CSV", "inventory:inventory_transaction_export", ""),)
    search_fields = (
        "transaction_no",
        "source_doc_no",
        "source_doc_type",
        "material__material_code",
        "material__material_name",
        "location__location_code",
        "batch__batch_no",
    )
    filter_fields = (("流水类型", "transaction_type", InventoryTransaction.TransactionType.choices),)
    sortable_fields = {
        "transaction_no": "transaction_no",
        "get_transaction_type_display": "transaction_type",
        "material.material_code": "material__material_code",
        "location.location_code": "location__location_code",
        "qty_delta": "qty_delta",
        "created_at": "created_at",
    }


class InventoryTransactionDetailView(LoginRequiredMixin, DetailView):
    model = InventoryTransaction
    template_name = "inventory/inventory_transaction_detail.html"
    context_object_name = "transaction"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, InventoryTransactionListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("material", "batch", "location", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"库存流水 {self.object.transaction_no}"
        context["inventory_summary"] = Inventory.objects.filter(
            material=self.object.material,
            location=self.object.location,
            inventory_type=self.object.batch.inventory_type if self.object.batch else InventoryBatch.InventoryType.AVAILABLE,
        ).first()
        return context


class InventoryCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()

    def dispatch(self, request, *args, **kwargs):
        required_permissions = getattr(self.list_view_class, "view_permission_required", ())
        if request.user.is_authenticated and required_permissions:
            require_any_erp_permission(request.user, required_permissions, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        list_view = self.list_view_class()
        list_view.request = self.request
        queryset = self.list_view_class.model.objects.all()
        queryset = list_view.apply_search(queryset)
        queryset = list_view.apply_status_filter(queryset)
        queryset = list_view.apply_extra_filters(queryset)
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        queryset = queryset.order_by(*self.get_ordering(list_view))
        return queryset

    def get_ordering(self, list_view):
        return list_view.current_ordering() or self.ordering

    def get(self, request):
        result = export_queryset_to_csv(
            self.module,
            self.get_queryset(),
            self.list_view_class.columns,
            request.user.id,
            filter_json={"ordering": ",".join(self.get_ordering(self._list_view_for_request())), "query": request.GET.dict()},
        )
        return export_file_response(result)

    def _list_view_for_request(self):
        list_view = self.list_view_class()
        list_view.request = self.request
        return list_view


class WarehouseLocationExportView(InventoryCsvExportView):
    module = "warehouse_locations"
    list_view_class = WarehouseLocationListView
    ordering = ("location_code",)


class InventoryExportView(InventoryCsvExportView):
    module = "inventory"
    list_view_class = InventoryListView
    ordering = ("material_id", "location_id")
    select_related = ("material", "location")


class InventoryBatchExportView(InventoryCsvExportView):
    module = "inventory_batches"
    list_view_class = InventoryBatchListView
    ordering = ("material_id", "location_id", "received_at")
    select_related = ("material", "location")


class InventoryTransactionExportView(InventoryCsvExportView):
    module = "inventory_transactions"
    list_view_class = InventoryTransactionListView
    ordering = ("-created_at",)
    select_related = ("material", "location", "batch")


class InitialInventoryImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(INITIAL_INVENTORY_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="initial_inventory_import_template.csv"'
        return response


class InitialInventoryImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入期初库存"
        context["list_url_name"] = "inventory:inventory_list"
        context["template_url_name"] = "inventory:initial_inventory_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("inventory:initial_inventory_import")

        text_file = uploaded_csv_text_file(upload)
        result = preview_initial_inventory_from_csv(text_file, request.user.id)
        if result.success:
            record_audit_log_from_request(
                request,
                "initial_inventory_preview",
                "initialization_job",
                result.data["initialization_job_id"],
                after_snapshot=result.data,
            )
            messages.success(request, f"{result.message}，共 {result.data['success_count']} 行")
            return redirect("files:initialization_job_detail", pk=result.data["initialization_job_id"])
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("initialization_job_id"),
            )
        )


class InitialInventoryManualView(LoginRequiredMixin, TemplateView):
    template_name = "inventory/initial_inventory_manual_form.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "手工期初库存建账"
        context["form"] = kwargs.get("form") or InitialInventoryManualForm()
        return context

    def post(self, request):
        form = InitialInventoryManualForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        result = preview_initial_inventory_rows([form.to_import_row()], request.user.id)
        if result.success:
            record_audit_log_from_request(
                request,
                "initial_inventory_manual_preview",
                "initialization_job",
                result.data["initialization_job_id"],
                after_snapshot=result.data,
            )
            messages.success(request, "期初库存已生成待确认任务，请确认后入账")
            return redirect("files:initialization_job_detail", pk=result.data["initialization_job_id"])

        return self.render_to_response(
            self.get_context_data(
                form=form,
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("initialization_job_id"),
            )
        )


class InitialInventoryConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        verification_response = require_second_verify(request, "files:initialization_job_detail", pk)
        if verification_response:
            return verification_response
        result = confirm_initial_inventory_import(pk, request.user.id)
        if result.success:
            record_audit_log_from_request(
                request,
                "initial_inventory_import",
                "initialization_job",
                pk,
                after_snapshot=result.data,
            )
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 行")
        else:
            messages.error(request, result.message or "期初库存确认失败")
        return redirect("files:initialization_job_detail", pk=pk)


class InitialInventoryCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        verification_response = require_second_verify(request, "files:initialization_job_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "files:initialization_job_detail",
            pk,
            field_names=("cancel_reason", "reason"),
            message="请填写期初库存撤销原因",
        )
        if reason_response:
            return reason_response
        result = cancel_initial_inventory_import(pk, request.user.id)
        if result.success:
            record_audit_log_from_request(
                request,
                "initial_inventory_cancel",
                "initialization_job",
                pk,
                after_snapshot={**result.data, "operation_reason": reason},
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or "期初库存撤销失败")
        return redirect("files:initialization_job_detail", pk=pk)


class LocationTransferListView(ErpListView):
    model = LocationTransfer
    page_title = "库位移库"
    create_url_name = "inventory:location_transfer_create"
    create_permission_required = PermissionCode.INVENTORY_PROCESS
    view_permission_required = (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS)
    permission_denied_message = "缺少库存数据查看权限"
    detail_url_name = "inventory:location_transfer_detail"
    columns = (
        ("移库单号", "transfer_no"),
        ("物料", "material.material_code"),
        ("批次", "batch.batch_no"),
        ("原库位", "from_location.location_code"),
        ("目标库位", "to_location.location_code"),
        ("数量", "transfer_qty"),
        ("状态", "get_status_display"),
    )
    ordering = ["-created_at"]
    page_actions = (("导出CSV", "inventory:location_transfer_export", ""),)
    search_fields = ("transfer_no", "material__material_code", "batch__batch_no", "from_location__location_code", "to_location__location_code")
    status_filter_field = "status"


class LocationTransferExportView(InventoryCsvExportView):
    module = "location_transfers"
    list_view_class = LocationTransferListView
    ordering = ("-created_at",)
    select_related = ("material", "batch", "from_location", "to_location")


class LocationTransferCreateView(LoginRequiredMixin, CreateView):
    model = LocationTransfer
    form_class = LocationTransferForm
    template_name = "inventory/location_transfer_form.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建库位移库"
        return context

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, "移库单已创建")
        return redirect("inventory:location_transfer_detail", pk=self.object.pk)


class LocationTransferDetailView(LoginRequiredMixin, DetailView):
    model = LocationTransfer
    template_name = "inventory/location_transfer_detail.html"
    context_object_name = "transfer"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, LocationTransferListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("material", "batch", "from_location", "to_location")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"移库 {self.object.transfer_no}"
        context["can_confirm"] = _can_process_inventory(self.request.user) and self.object.status == LocationTransfer.TransferStatus.DRAFT
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "location_transfer",
            self.object.id,
            self.object.transfer_no,
        )
        return context


class LocationTransferPrintView(LoginRequiredMixin, DetailView):
    model = LocationTransfer
    template_name = "inventory/location_transfer_print.html"
    context_object_name = "transfer"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, LocationTransferListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("material", "batch", "from_location", "to_location")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印移库单 {self.object.transfer_no}"
        record_print_log(
            template_type="location_transfer",
            source_doc_type="location_transfer",
            source_doc_id=self.object.id,
            source_doc_no=self.object.transfer_no,
            printed_by_id=self.request.user.id,
        )
        return context


class LocationTransferConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        verification_response = require_second_verify(request, "inventory:location_transfer_detail", pk)
        if verification_response:
            return verification_response
        result = confirm_location_transfer(pk, request.user.id, f"location-transfer:{pk}")
        if result.success:
            record_audit_log_from_request(request, "location_transfer_confirm", "location_transfer", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "库位移库确认失败")
        return redirect("inventory:location_transfer_detail", pk=pk)


class StockCountListView(ErpListView):
    model = StockCount
    page_title = "盘点"
    create_url_name = "inventory:stock_count_create"
    create_permission_required = PermissionCode.INVENTORY_PROCESS
    view_permission_required = (PermissionCode.INVENTORY_VIEW, PermissionCode.INVENTORY_PROCESS)
    permission_denied_message = "缺少库存数据查看权限"
    detail_url_name = "inventory:stock_count_detail"
    columns = (
        ("盘点单号", "stock_count_no"),
        ("范围类型", "scope_type"),
        ("范围值", "scope_value"),
        ("状态", "get_status_display"),
        ("快照时间", "snapshot_at"),
    )
    ordering = ["-created_at"]
    page_actions = (("导出CSV", "inventory:stock_count_export", ""),)
    search_fields = ("stock_count_no", "scope_value")
    status_filter_field = "status"


class StockCountExportView(InventoryCsvExportView):
    module = "stock_counts"
    list_view_class = StockCountListView
    ordering = ("-created_at",)


class StockCountCreateView(LoginRequiredMixin, CreateView):
    model = StockCount
    form_class = StockCountForm
    template_name = "inventory/stock_count_form.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建盘点单"
        return context

    def form_valid(self, form):
        result = create_stock_count_from_batches(
            operator_id=self.request.user.id,
            scope_type=form.cleaned_data.get("scope_type") or "batch",
            scope_value=form.cleaned_data.get("scope_value") or "",
            location_id=form.cleaned_data.get("location").id if form.cleaned_data.get("location") else None,
        )
        if result.success:
            messages.success(self.request, result.message)
            return redirect("inventory:stock_count_detail", pk=result.data["stock_count_id"])
        messages.error(self.request, result.message or result.error_code or "盘点单创建失败")
        return self.form_invalid(form)


class StockCountDetailView(LoginRequiredMixin, DetailView):
    model = StockCount
    template_name = "inventory/stock_count_detail.html"
    context_object_name = "stock_count"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, StockCountListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("created_by")
            .prefetch_related("items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"盘点 {self.object.stock_count_no}"
        context["can_confirm"] = (
            _can_process_inventory(self.request.user)
            and self.object.status == StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT
        )
        context["can_add_item"] = _can_process_inventory(self.request.user) and self.object.status in [
            StockCount.CountStatus.DRAFT,
            StockCount.CountStatus.COUNTING,
            StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT,
        ]
        context["materials"] = Material.objects.filter(status=Material.MaterialStatus.ACTIVE).order_by("material_code")
        context["locations"] = WarehouseLocation.objects.filter(status=WarehouseLocation.LocationStatus.ACTIVE).order_by("location_code")
        context["batches"] = (
            InventoryBatch.objects.filter(batch_status=InventoryBatch.BatchStatus.IN_STOCK, remaining_qty__gt=0)
            .select_related("material", "location")
            .order_by("material__material_code", "location__location_code", "batch_no")
        )
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "stock_count",
            self.object.id,
            self.object.stock_count_no,
        )
        return context


class StockCountPrintView(LoginRequiredMixin, DetailView):
    model = StockCount
    template_name = "inventory/stock_count_print.html"
    context_object_name = "stock_count"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, StockCountListView.view_permission_required, "缺少库存数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("created_by")
            .prefetch_related("items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印盘点单 {self.object.stock_count_no}"
        record_print_log(
            template_type="stock_count",
            source_doc_type="stock_count",
            source_doc_id=self.object.id,
            source_doc_no=self.object.stock_count_no,
            printed_by_id=self.request.user.id,
        )
        return context


class StockCountConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        verification_response = require_second_verify(request, "inventory:stock_count_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "inventory:stock_count_detail",
            pk,
            field_names=("adjust_reason", "reason"),
            message="请填写盘点调整原因",
        )
        if reason_response:
            return reason_response
        result = confirm_stock_count_adjustment(pk, request.user.id, f"stock-count:{pk}")
        if result.success:
            record_audit_log_from_request(
                request,
                "stock_count_confirm_adjustment",
                "stock_count",
                pk,
                after_snapshot={**result.data, "operation_reason": reason},
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "盘点调整确认失败")
        return redirect("inventory:stock_count_detail", pk=pk)


class StockCountItemCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.INVENTORY_PROCESS, "缺少库存单据处理权限")
        try:
            stock_count = StockCount.objects.get(pk=pk)
        except StockCount.DoesNotExist:
            messages.error(request, "盘点单不存在")
            return redirect("inventory:stock_count_list")

        if stock_count.status not in [
            StockCount.CountStatus.DRAFT,
            StockCount.CountStatus.COUNTING,
            StockCount.CountStatus.APPROVED_PENDING_ADJUSTMENT,
        ]:
            messages.error(request, "当前盘点单状态不能新增明细")
            return redirect("inventory:stock_count_detail", pk=pk)

        material_id = request.POST.get("material")
        location_id = request.POST.get("location")
        batch_id = request.POST.get("batch") or None
        try:
            book_qty = Decimal(request.POST.get("book_qty", ""))
            counted_qty = Decimal(request.POST.get("counted_qty", ""))
        except (InvalidOperation, TypeError):
            messages.error(request, "账面数量和实盘数量必须正确填写")
            return redirect("inventory:stock_count_detail", pk=pk)

        if not material_id or not location_id or book_qty < 0 or counted_qty < 0:
            messages.error(request, "物料、库位、账面数量和实盘数量必须正确填写")
            return redirect("inventory:stock_count_detail", pk=pk)

        if batch_id:
            batch = InventoryBatch.objects.filter(id=batch_id).first()
            if not batch or batch.material_id != int(material_id) or batch.location_id != int(location_id):
                messages.error(request, "批次必须与物料和库位一致")
                return redirect("inventory:stock_count_detail", pk=pk)

        if StockCountItem.objects.filter(
            stock_count=stock_count,
            material_id=material_id,
            location_id=location_id,
            batch_id=batch_id,
        ).exists():
            messages.error(request, "同一盘点单中相同物料、批次和库位不能重复")
            return redirect("inventory:stock_count_detail", pk=pk)

        StockCountItem.objects.create(
            stock_count=stock_count,
            material_id=material_id,
            location_id=location_id,
            batch_id=batch_id,
            book_qty=book_qty,
            counted_qty=counted_qty,
            difference_qty=counted_qty - book_qty,
            difference_reason=request.POST.get("difference_reason", "").strip(),
        )
        messages.success(request, "盘点明细已新增")
        return redirect("inventory:stock_count_detail", pk=pk)


def _can_process_inventory(user) -> bool:
    return user_has_permission(user, PermissionCode.INVENTORY_PROCESS)
