import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import CreateView

from accounts.permissions import PermissionCode, require_any_erp_permission, require_erp_permission, user_has_permission
from bom.services import UnitConversionMissing, required_component_qty_base
from files.services import csv_upload_validation_error, export_queryset_to_csv, record_print_log, uploaded_csv_text_file
from files.view_helpers import build_attachment_panel, export_file_response
from inventory.models import InventoryBatch, WarehouseLocation
from system.services import next_document_no, record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason, require_post_reason, require_second_verify

from .forms import ProductionMaterialRequisitionForm, ProductionMaterialRequisitionItemFormSet, ProductionOrderForm
from .forms import ProductionReceiptForm, ProductionReceiptItemFormSet
from .import_services import (
    MATERIAL_REQUISITION_IMPORT_TEMPLATE_ROWS,
    PRODUCTION_ORDER_IMPORT_TEMPLATE_ROWS,
    PRODUCTION_RECEIPT_IMPORT_TEMPLATE_ROWS,
    import_material_requisitions_from_csv,
    import_production_orders_from_csv,
    import_production_receipts_from_csv,
)
from .models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)
from .services import confirm_material_requisition, confirm_production_receipt


class ProductionOrderListView(ErpListView):
    model = ProductionOrder
    page_title = "生产指令"
    create_url_name = "production:production_order_create"
    create_permission_required = PermissionCode.PRODUCTION_PROCESS
    view_permission_required = (PermissionCode.PRODUCTION_VIEW, PermissionCode.PRODUCTION_PROCESS)
    permission_denied_message = "缺少生产数据查看权限"
    detail_url_name = "production:production_order_detail"
    columns = (
        ("生产单号", "production_order_no"),
        ("成品", "finished_material.material_code"),
        ("生产数量", "production_qty"),
        ("已入库", "received_qty"),
        ("状态", "get_status_display"),
    )
    ordering = ["-created_at"]
    page_actions = (
        ("导出CSV", "production:production_order_export", ""),
        ("下载导入模板", "production:production_order_import_template", ""),
        ("导入CSV", "production:production_order_import", "primary"),
    )
    page_action_permissions = {
        "production:production_order_import_template": PermissionCode.PRODUCTION_PROCESS,
        "production:production_order_import": PermissionCode.PRODUCTION_PROCESS,
    }
    search_fields = ("production_order_no", "finished_material__material_code", "finished_material__material_name")
    status_filter_field = "status"
    sortable_fields = {
        "production_order_no": "production_order_no",
        "finished_material.material_code": "finished_material__material_code",
        "production_qty": "production_qty",
        "received_qty": "received_qty",
        "get_status_display": "status",
    }


class ProductionOrderCreateView(LoginRequiredMixin, CreateView):
    model = ProductionOrder
    form_class = ProductionOrderForm
    template_name = "production/production_order_form.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建生产指令"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save(user=self.request.user)
        messages.success(self.request, "生产指令已保存")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("production:production_order_detail", kwargs={"pk": self.object.pk})


class ProductionOrderDetailView(LoginRequiredMixin, DetailView):
    model = ProductionOrder
    template_name = "production/production_order_detail.html"
    context_object_name = "production_order"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, ProductionOrderListView.view_permission_required, "缺少生产数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("finished_material", "locked_bom", "created_by", "sales_order_item")
            .prefetch_related(
                "locked_bom__items__component_material",
                "material_requisitions__items__material",
                "receipts__items__finished_material",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_production = _can_process_production(self.request.user)
        context["page_title"] = f"生产指令 {self.object.production_order_no}"
        context["can_process_production"] = can_process_production
        context["can_edit"] = can_process_production and _can_edit_production_order(self.object)
        context["can_cancel"] = can_process_production and _can_edit_production_order(self.object)
        context["can_create_requisition"] = can_process_production and self.object.status == ProductionOrder.Status.PENDING
        context["can_create_receipt"] = can_process_production and self.object.status in [
            ProductionOrder.Status.PENDING,
            ProductionOrder.Status.IN_PROGRESS,
        ] and self.object.production_qty > self.object.received_qty
        context["locations"] = WarehouseLocation.objects.filter(status=WarehouseLocation.LocationStatus.ACTIVE).order_by("location_code")
        context["today"] = timezone.localdate()
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "production_order",
            self.object.id,
            self.object.production_order_no,
        )
        return context


class ProductionOrderPrintView(LoginRequiredMixin, DetailView):
    model = ProductionOrder
    template_name = "production/production_order_print.html"
    context_object_name = "production_order"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, ProductionOrderListView.view_permission_required, "缺少生产数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("finished_material", "locked_bom", "created_by", "sales_order_item")
            .prefetch_related("locked_bom__items__component_material")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印生产指令 {self.object.production_order_no}"
        record_print_log(
            template_type="production_order",
            source_doc_type="production_order",
            source_doc_id=self.object.id,
            source_doc_no=self.object.production_order_no,
            printed_by_id=self.request.user.id,
        )
        return context


class ProductionOrderUpdateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        production_order = self._get_order(pk)
        if not production_order:
            messages.error(request, "生产指令不存在")
            return redirect("production:production_order_list")
        if not _can_edit_production_order(production_order):
            messages.error(request, "只有待生产且无领料/入库单的生产指令可以编辑")
            return redirect("production:production_order_detail", pk=pk)
        form = ProductionOrderForm(instance=production_order)
        return self._render(request, production_order, form)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        production_order = self._get_order(pk)
        if not production_order:
            messages.error(request, "生产指令不存在")
            return redirect("production:production_order_list")
        if not _can_edit_production_order(production_order):
            messages.error(request, "只有待生产且无领料/入库单的生产指令可以编辑")
            return redirect("production:production_order_detail", pk=pk)
        form = ProductionOrderForm(request.POST, instance=production_order)
        if not form.is_valid():
            return self._render(request, production_order, form)
        submitted_order = form.save(commit=False, user=request.user)

        with transaction.atomic():
            production_order = (
                ProductionOrder.objects.select_for_update()
                .select_related("finished_material", "locked_bom")
                .prefetch_related("material_requisitions", "receipts")
                .get(pk=pk)
            )
            if not _can_edit_production_order(production_order):
                messages.error(request, "只有待生产且无领料/入库单的生产指令可以编辑")
                return redirect("production:production_order_detail", pk=pk)
            before_snapshot = _production_order_snapshot(production_order)
            production_order.finished_material = submitted_order.finished_material
            production_order.production_qty = submitted_order.production_qty
            production_order.locked_bom = submitted_order.locked_bom
            production_order.locked_bom_version = submitted_order.locked_bom_version
            production_order.planned_start_date = submitted_order.planned_start_date
            production_order.planned_finish_date = submitted_order.planned_finish_date
            production_order.remark = submitted_order.remark
            production_order.updated_by = request.user
            production_order.version += 1
            production_order.save(
                update_fields=[
                    "finished_material",
                    "production_qty",
                    "locked_bom",
                    "locked_bom_version",
                    "planned_start_date",
                    "planned_finish_date",
                    "remark",
                    "updated_by",
                    "updated_at",
                    "version",
                ]
            )
            after_snapshot = {
                **_production_order_snapshot(production_order),
                "operation_reason": optional_post_reason(request, default="页面编辑生产指令"),
            }

        record_audit_log_from_request(
            request,
            "production_order_update",
            "production_order",
            production_order.id,
            production_order.production_order_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "生产指令已更新")
        return redirect("production:production_order_detail", pk=production_order.pk)

    def _get_order(self, pk):
        return (
            ProductionOrder.objects.select_related("finished_material", "locked_bom")
            .prefetch_related("material_requisitions", "receipts")
            .filter(pk=pk)
            .first()
        )

    def _render(self, request, production_order, form):
        return render(
            request,
            "production/production_order_form.html",
            {
                "page_title": f"编辑生产指令 {production_order.production_order_no}",
                "form": form,
                "production_order": production_order,
                "is_edit": True,
            },
        )


class ProductionOrderCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        verification_response = require_second_verify(request, "production:production_order_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "production:production_order_detail",
            pk,
            field_names=("cancel_reason", "reason"),
            message="请填写生产指令取消原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                production_order = (
                    ProductionOrder.objects.select_for_update()
                    .select_related("finished_material", "locked_bom")
                    .prefetch_related("material_requisitions", "receipts")
                    .get(pk=pk)
                )
                if not _can_edit_production_order(production_order):
                    messages.error(request, "只有待生产且无领料/入库单的生产指令可以取消")
                    return redirect("production:production_order_detail", pk=pk)
                before_snapshot = _production_order_snapshot(production_order)
                production_order.status = ProductionOrder.Status.CANCELLED
                production_order.updated_by = request.user
                production_order.version += 1
                production_order.save(update_fields=["status", "updated_by", "updated_at", "version"])
                after_snapshot = _production_order_snapshot(production_order)
        except ProductionOrder.DoesNotExist:
            messages.error(request, "生产指令不存在")
            return redirect("production:production_order_list")

        record_audit_log_from_request(
            request,
            "production_order_cancel",
            "production_order",
            production_order.id,
            production_order.production_order_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "生产指令已取消")
        return redirect("production:production_order_detail", pk=pk)


class ProductionOrderCreateRequisitionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        try:
            with transaction.atomic():
                production_order = (
                    ProductionOrder.objects.select_for_update()
                    .select_related("locked_bom", "finished_material")
                    .prefetch_related("locked_bom__items__component_material")
                    .get(pk=pk)
                )
                if production_order.status != ProductionOrder.Status.PENDING:
                    messages.error(request, "只有待生产的生产指令可以生成领料单")
                    return redirect("production:production_order_detail", pk=pk)
                if production_order.material_requisitions.exists():
                    messages.error(request, "该生产指令已存在领料单")
                    return redirect("production:production_order_detail", pk=pk)

                requisition = ProductionMaterialRequisition.objects.create(
                    requisition_no=next_document_no("MR"),
                    production_order=production_order,
                    requisition_date=timezone.localdate(),
                    status=ProductionMaterialRequisition.Status.PENDING_CONFIRM,
                    created_by=request.user,
                    remark=request.POST.get("remark", "").strip(),
                )
                line_no = 1
                for bom_item in production_order.locked_bom.items.select_related("bom", "component_material").order_by("line_no"):
                    required_qty = required_component_qty_base(bom_item, production_order.production_qty)
                    allocations = _allocate_fifo_batches(bom_item.component_material_id, required_qty)
                    if not allocations:
                        raise ValueError(f"{bom_item.component_material.material_code} 可用库存不足，无法生成领料单")
                    for batch, qty in allocations:
                        ProductionMaterialRequisitionItem.objects.create(
                            requisition=requisition,
                            production_order=production_order,
                            line_no=line_no,
                            material=bom_item.component_material,
                            required_qty=qty,
                            issued_qty=qty,
                            batch=batch,
                            location=batch.location,
                        )
                        line_no += 1
        except ProductionOrder.DoesNotExist:
            messages.error(request, "生产指令不存在")
            return redirect("production:production_order_list")
        except UnitConversionMissing as exc:
            messages.error(request, f"BOM 单位换算缺失：{exc}")
            return redirect("production:production_order_detail", pk=pk)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("production:production_order_detail", pk=pk)

        messages.success(request, "生产领料单已生成")
        return redirect("production:material_requisition_detail", pk=requisition.pk)


class ProductionOrderCreateReceiptView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        location = WarehouseLocation.objects.filter(
            pk=request.POST.get("location"),
            status=WarehouseLocation.LocationStatus.ACTIVE,
        ).first()
        if not location:
            messages.error(request, "请选择有效入库库位")
            return redirect("production:production_order_detail", pk=pk)

        try:
            with transaction.atomic():
                production_order = ProductionOrder.objects.select_for_update().select_related("finished_material").get(pk=pk)
                remaining_qty = production_order.production_qty - production_order.received_qty
                if production_order.status not in [ProductionOrder.Status.PENDING, ProductionOrder.Status.IN_PROGRESS] or remaining_qty <= 0:
                    messages.error(request, "当前生产指令不能生成入库单")
                    return redirect("production:production_order_detail", pk=pk)
                receipt = ProductionReceipt.objects.create(
                    production_receipt_no=next_document_no("PI"),
                    production_order=production_order,
                    receipt_date=timezone.localdate(),
                    status=ProductionReceipt.Status.PENDING_CONFIRM,
                    created_by=request.user,
                    remark=request.POST.get("remark", "").strip(),
                )
                ProductionReceiptItem.objects.create(
                    production_receipt=receipt,
                    production_order=production_order,
                    line_no=1,
                    finished_material=production_order.finished_material,
                    receipt_qty=remaining_qty,
                    location=location,
                )
        except ProductionOrder.DoesNotExist:
            messages.error(request, "生产指令不存在")
            return redirect("production:production_order_list")

        messages.success(request, "生产入库单已生成")
        return redirect("production:production_receipt_detail", pk=receipt.pk)


class ProductionMaterialRequisitionListView(ErpListView):
    model = ProductionMaterialRequisition
    page_title = "生产领料"
    view_permission_required = (PermissionCode.PRODUCTION_VIEW, PermissionCode.PRODUCTION_PROCESS)
    permission_denied_message = "缺少生产数据查看权限"
    detail_url_name = "production:material_requisition_detail"
    columns = (
        ("领料单号", "requisition_no"),
        ("生产单", "production_order.production_order_no"),
        ("领料日期", "requisition_date"),
        ("状态", "get_status_display"),
    )
    ordering = ["-requisition_date", "-id"]
    page_actions = (
        ("导出CSV", "production:material_requisition_export", ""),
        ("下载导入模板", "production:material_requisition_import_template", ""),
        ("导入CSV", "production:material_requisition_import", "primary"),
    )
    page_action_permissions = {
        "production:material_requisition_import_template": PermissionCode.PRODUCTION_PROCESS,
        "production:material_requisition_import": PermissionCode.PRODUCTION_PROCESS,
    }
    search_fields = ("requisition_no", "production_order__production_order_no")
    status_filter_field = "status"


class ProductionCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()

    def dispatch(self, request, *args, **kwargs):
        required_permissions = getattr(self.list_view_class, "view_permission_required", ())
        if request.user.is_authenticated and required_permissions:
            require_any_erp_permission(request.user, required_permissions, "缺少生产数据查看权限")
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


class ProductionMaterialRequisitionExportView(ProductionCsvExportView):
    module = "production_material_requisitions"
    list_view_class = ProductionMaterialRequisitionListView
    ordering = ("-requisition_date", "-id")
    select_related = ("production_order",)


class ProductionMaterialRequisitionImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(MATERIAL_REQUISITION_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="material_requisition_import_template.csv"'
        return response


class ProductionMaterialRequisitionImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入生产领料"
        context["list_url_name"] = "production:material_requisition_list"
        context["template_url_name"] = "production:material_requisition_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("production:material_requisition_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_material_requisitions_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("production:material_requisition_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class ProductionOrderExportView(ProductionCsvExportView):
    module = "production_orders"
    list_view_class = ProductionOrderListView
    ordering = ("-created_at",)
    select_related = ("finished_material",)


class ProductionOrderImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(PRODUCTION_ORDER_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="production_order_import_template.csv"'
        return response


class ProductionOrderImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入生产指令"
        context["list_url_name"] = "production:production_order_list"
        context["template_url_name"] = "production:production_order_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("production:production_order_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_production_orders_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("production:production_order_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class ProductionMaterialRequisitionDetailView(LoginRequiredMixin, DetailView):
    model = ProductionMaterialRequisition
    template_name = "production/material_requisition_detail.html"
    context_object_name = "requisition"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(
                request.user,
                ProductionMaterialRequisitionListView.view_permission_required,
                "缺少生产数据查看权限",
            )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("production_order", "created_by")
            .prefetch_related("items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_production = _can_process_production(self.request.user)
        context["page_title"] = f"生产领料 {self.object.requisition_no}"
        context["can_edit"] = can_process_production and self.object.status == ProductionMaterialRequisition.Status.PENDING_CONFIRM
        context["can_confirm"] = (
            can_process_production
            and self.object.status == ProductionMaterialRequisition.Status.PENDING_CONFIRM
        )
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "production_material_requisition",
            self.object.id,
            self.object.requisition_no,
        )
        return context


class ProductionMaterialRequisitionUpdateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        requisition = self._get_requisition(pk)
        if not requisition:
            messages.error(request, "生产领料单不存在")
            return redirect("production:material_requisition_list")
        if requisition.status != ProductionMaterialRequisition.Status.PENDING_CONFIRM:
            messages.error(request, "只有待确认的生产领料单可以编辑")
            return redirect("production:material_requisition_detail", pk=pk)
        form = ProductionMaterialRequisitionForm(instance=requisition)
        item_formset = ProductionMaterialRequisitionItemFormSet(instance=requisition)
        return self._render(request, requisition, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        requisition = self._get_requisition(pk)
        if not requisition:
            messages.error(request, "生产领料单不存在")
            return redirect("production:material_requisition_list")
        if requisition.status != ProductionMaterialRequisition.Status.PENDING_CONFIRM:
            messages.error(request, "只有待确认的生产领料单可以编辑")
            return redirect("production:material_requisition_detail", pk=pk)

        form = ProductionMaterialRequisitionForm(request.POST, instance=requisition)
        item_formset = ProductionMaterialRequisitionItemFormSet(request.POST, instance=requisition)
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, requisition, form, item_formset)
        submitted_requisition = form.save(commit=False)

        with transaction.atomic():
            requisition = (
                ProductionMaterialRequisition.objects.select_for_update()
                .select_related("production_order", "created_by")
                .prefetch_related("items__material", "items__batch", "items__location")
                .get(pk=pk)
            )
            if requisition.status != ProductionMaterialRequisition.Status.PENDING_CONFIRM:
                messages.error(request, "只有待确认的生产领料单可以编辑")
                return redirect("production:material_requisition_detail", pk=pk)
            before_snapshot = _material_requisition_snapshot(requisition)
            requisition.requisition_date = submitted_requisition.requisition_date
            requisition.remark = submitted_requisition.remark
            requisition.save(update_fields=["requisition_date", "remark"])
            item_formset.instance = requisition
            item_formset.save()
            after_snapshot = {
                **_material_requisition_snapshot(requisition),
                "operation_reason": optional_post_reason(request, default="页面编辑生产领料单"),
            }

        record_audit_log_from_request(
            request,
            "production_material_requisition_update",
            "production_material_requisition",
            requisition.id,
            requisition.requisition_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "生产领料单已更新")
        return redirect("production:material_requisition_detail", pk=requisition.pk)

    def _get_requisition(self, pk):
        return (
            ProductionMaterialRequisition.objects.select_related("production_order", "created_by")
            .prefetch_related("items__material", "items__batch", "items__location")
            .filter(pk=pk)
            .first()
        )

    def _render(self, request, requisition, form, item_formset):
        return render(
            request,
            "production/material_requisition_form.html",
            {
                "page_title": f"编辑生产领料 {requisition.requisition_no}",
                "form": form,
                "item_formset": item_formset,
                "requisition": requisition,
            },
        )


class ProductionMaterialRequisitionPrintView(LoginRequiredMixin, DetailView):
    model = ProductionMaterialRequisition
    template_name = "production/material_requisition_print.html"
    context_object_name = "requisition"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(
                request.user,
                ProductionMaterialRequisitionListView.view_permission_required,
                "缺少生产数据查看权限",
            )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("production_order", "created_by")
            .prefetch_related("items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印生产领料 {self.object.requisition_no}"
        record_print_log(
            template_type="production_material_requisition",
            source_doc_type="production_material_requisition",
            source_doc_id=self.object.id,
            source_doc_no=self.object.requisition_no,
            printed_by_id=self.request.user.id,
        )
        return context


class ProductionMaterialRequisitionConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        verification_response = require_second_verify(request, "production:material_requisition_detail", pk)
        if verification_response:
            return verification_response
        result = confirm_material_requisition(pk, request.user.id, f"material-requisition:{pk}")
        if result.success:
            record_audit_log_from_request(
                request,
                "production_material_requisition_confirm",
                "production_material_requisition",
                pk,
                after_snapshot=result.data,
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "生产领料确认失败")
        return redirect("production:material_requisition_detail", pk=pk)


class ProductionReceiptListView(ErpListView):
    model = ProductionReceipt
    page_title = "生产入库"
    view_permission_required = (PermissionCode.PRODUCTION_VIEW, PermissionCode.PRODUCTION_PROCESS)
    permission_denied_message = "缺少生产数据查看权限"
    detail_url_name = "production:production_receipt_detail"
    columns = (
        ("入库单号", "production_receipt_no"),
        ("生产单", "production_order.production_order_no"),
        ("入库日期", "receipt_date"),
        ("状态", "get_status_display"),
    )
    ordering = ["-receipt_date", "-id"]
    page_actions = (
        ("导出CSV", "production:production_receipt_export", ""),
        ("下载导入模板", "production:production_receipt_import_template", ""),
        ("导入CSV", "production:production_receipt_import", "primary"),
    )
    page_action_permissions = {
        "production:production_receipt_import_template": PermissionCode.PRODUCTION_PROCESS,
        "production:production_receipt_import": PermissionCode.PRODUCTION_PROCESS,
    }
    search_fields = ("production_receipt_no", "production_order__production_order_no")
    status_filter_field = "status"


class ProductionReceiptExportView(ProductionCsvExportView):
    module = "production_receipts"
    list_view_class = ProductionReceiptListView
    ordering = ("-receipt_date", "-id")
    select_related = ("production_order",)


class ProductionReceiptImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(PRODUCTION_RECEIPT_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="production_receipt_import_template.csv"'
        return response


class ProductionReceiptImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入生产入库"
        context["list_url_name"] = "production:production_receipt_list"
        context["template_url_name"] = "production:production_receipt_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("production:production_receipt_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_production_receipts_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("production:production_receipt_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class ProductionReceiptDetailView(LoginRequiredMixin, DetailView):
    model = ProductionReceipt
    template_name = "production/production_receipt_detail.html"
    context_object_name = "receipt"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, ProductionReceiptListView.view_permission_required, "缺少生产数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("production_order", "created_by")
            .prefetch_related("items__finished_material", "items__location", "items__batch")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_production = _can_process_production(self.request.user)
        context["page_title"] = f"生产入库 {self.object.production_receipt_no}"
        context["can_edit"] = (
            can_process_production
            and
            self.object.status == ProductionReceipt.Status.PENDING_CONFIRM
            and not self.object.items.filter(batch__isnull=False).exists()
        )
        context["can_confirm"] = can_process_production and self.object.status == ProductionReceipt.Status.PENDING_CONFIRM
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "production_receipt",
            self.object.id,
            self.object.production_receipt_no,
        )
        return context


class ProductionReceiptUpdateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        receipt = self._get_receipt(pk)
        if not receipt:
            messages.error(request, "生产入库单不存在")
            return redirect("production:production_receipt_list")
        if not self._can_edit(receipt):
            messages.error(request, "只有待确认且未生成批次的生产入库单可以编辑")
            return redirect("production:production_receipt_detail", pk=pk)
        form = ProductionReceiptForm(instance=receipt)
        item_formset = ProductionReceiptItemFormSet(instance=receipt)
        return self._render(request, receipt, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        receipt = self._get_receipt(pk)
        if not receipt:
            messages.error(request, "生产入库单不存在")
            return redirect("production:production_receipt_list")
        if not self._can_edit(receipt):
            messages.error(request, "只有待确认且未生成批次的生产入库单可以编辑")
            return redirect("production:production_receipt_detail", pk=pk)
        form = ProductionReceiptForm(request.POST, instance=receipt)
        item_formset = ProductionReceiptItemFormSet(request.POST, instance=receipt)
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, receipt, form, item_formset)
        submitted_receipt = form.save(commit=False)

        with transaction.atomic():
            receipt = (
                ProductionReceipt.objects.select_for_update()
                .select_related("production_order", "created_by")
                .prefetch_related("items__finished_material", "items__location", "items__batch")
                .get(pk=pk)
            )
            if not self._can_edit(receipt):
                messages.error(request, "只有待确认且未生成批次的生产入库单可以编辑")
                return redirect("production:production_receipt_detail", pk=pk)
            before_snapshot = _production_receipt_snapshot(receipt)
            receipt.receipt_date = submitted_receipt.receipt_date
            receipt.remark = submitted_receipt.remark
            receipt.save(update_fields=["receipt_date", "remark"])
            item_formset.instance = receipt
            item_formset.save()
            after_snapshot = {
                **_production_receipt_snapshot(receipt),
                "operation_reason": optional_post_reason(request, default="页面编辑生产入库单"),
            }

        record_audit_log_from_request(
            request,
            "production_receipt_update",
            "production_receipt",
            receipt.id,
            receipt.production_receipt_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "生产入库单已更新")
        return redirect("production:production_receipt_detail", pk=receipt.pk)

    def _get_receipt(self, pk):
        return (
            ProductionReceipt.objects.select_related("production_order", "created_by")
            .prefetch_related("items__finished_material", "items__location", "items__batch")
            .filter(pk=pk)
            .first()
        )

    def _can_edit(self, receipt):
        return receipt.status == ProductionReceipt.Status.PENDING_CONFIRM and not receipt.items.filter(batch__isnull=False).exists()

    def _render(self, request, receipt, form, item_formset):
        return render(
            request,
            "production/production_receipt_form.html",
            {
                "page_title": f"编辑生产入库 {receipt.production_receipt_no}",
                "form": form,
                "item_formset": item_formset,
                "receipt": receipt,
            },
        )


class ProductionReceiptPrintView(LoginRequiredMixin, DetailView):
    model = ProductionReceipt
    template_name = "production/production_receipt_print.html"
    context_object_name = "receipt"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, ProductionReceiptListView.view_permission_required, "缺少生产数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("production_order", "created_by")
            .prefetch_related("items__finished_material", "items__location", "items__batch")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印生产入库 {self.object.production_receipt_no}"
        record_print_log(
            template_type="production_receipt",
            source_doc_type="production_receipt",
            source_doc_id=self.object.id,
            source_doc_no=self.object.production_receipt_no,
            printed_by_id=self.request.user.id,
        )
        return context


class ProductionReceiptConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PRODUCTION_PROCESS, "缺少生产单据处理权限")
        verification_response = require_second_verify(request, "production:production_receipt_detail", pk)
        if verification_response:
            return verification_response
        result = confirm_production_receipt(pk, request.user.id, f"production-receipt:{pk}")
        if result.success:
            record_audit_log_from_request(request, "production_receipt_confirm", "production_receipt", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "生产入库确认失败")
        return redirect("production:production_receipt_detail", pk=pk)


def _can_process_production(user) -> bool:
    return user_has_permission(user, PermissionCode.PRODUCTION_PROCESS)


def _can_edit_production_order(production_order: ProductionOrder) -> bool:
    return (
        production_order.status == ProductionOrder.Status.PENDING
        and production_order.received_qty == 0
        and not production_order.material_requisitions.exists()
        and not production_order.receipts.exists()
    )


def _production_order_snapshot(production_order: ProductionOrder) -> dict:
    production_order.refresh_from_db()
    return {
        "production_order_no": production_order.production_order_no,
        "sales_order_item_id": production_order.sales_order_item_id,
        "finished_material_id": production_order.finished_material_id,
        "production_qty": str(production_order.production_qty),
        "received_qty": str(production_order.received_qty),
        "locked_bom_id": production_order.locked_bom_id,
        "locked_bom_version": production_order.locked_bom_version,
        "status": production_order.status,
        "planned_start_date": production_order.planned_start_date.isoformat() if production_order.planned_start_date else None,
        "planned_finish_date": production_order.planned_finish_date.isoformat() if production_order.planned_finish_date else None,
        "remark": production_order.remark,
        "version": production_order.version,
    }


def _material_requisition_snapshot(requisition: ProductionMaterialRequisition) -> dict:
    requisition.refresh_from_db()
    items = requisition.items.select_related("material", "batch", "location").order_by("line_no")
    return {
        "requisition_no": requisition.requisition_no,
        "production_order_id": requisition.production_order_id,
        "requisition_date": requisition.requisition_date.isoformat() if requisition.requisition_date else None,
        "status": requisition.status,
        "remark": requisition.remark,
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "material_id": item.material_id,
                "material_code": item.material.material_code if item.material_id else "",
                "required_qty": str(item.required_qty),
                "issued_qty": str(item.issued_qty),
                "batch_id": item.batch_id,
                "location_id": item.location_id,
                "adjust_reason": item.adjust_reason,
            }
            for item in items
        ],
    }


def _production_receipt_snapshot(receipt: ProductionReceipt) -> dict:
    receipt.refresh_from_db()
    items = receipt.items.select_related("finished_material", "location", "batch").order_by("line_no")
    return {
        "production_receipt_no": receipt.production_receipt_no,
        "production_order_id": receipt.production_order_id,
        "receipt_date": receipt.receipt_date.isoformat() if receipt.receipt_date else None,
        "status": receipt.status,
        "remark": receipt.remark,
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "finished_material_id": item.finished_material_id,
                "finished_material_code": item.finished_material.material_code if item.finished_material_id else "",
                "receipt_qty": str(item.receipt_qty),
                "location_id": item.location_id,
                "batch_id": item.batch_id,
                "batch_no": item.batch_no,
                "quality_status": item.quality_status,
            }
            for item in items
        ],
    }


def _allocate_fifo_batches(material_id: int, required_qty):
    remaining_qty = required_qty
    allocations = []
    batches = (
        InventoryBatch.objects.select_for_update()
        .select_related("location")
        .filter(
            material_id=material_id,
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
            remaining_qty__gt=0,
        )
        .order_by("received_at", "batch_no", "id")
    )
    for batch in batches:
        if remaining_qty <= 0:
            break
        qty = min(batch.remaining_qty, remaining_qty)
        allocations.append((batch, qty))
        remaining_qty -= qty
    if remaining_qty > 0:
        return []
    return allocations
