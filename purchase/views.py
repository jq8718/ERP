import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.views.generic import TemplateView
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView
from django.views.generic.edit import CreateView
from django.utils import timezone

from accounts.permissions import PermissionCode, can_view_amount, require_any_erp_permission, require_erp_permission, user_has_permission
from files.services import csv_upload_validation_error, export_queryset_to_csv, record_print_log, uploaded_csv_text_file
from files.view_helpers import build_attachment_panel, export_file_response
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material, Supplier
from system.date_utils import parse_user_date
from system.services import next_document_no, record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason, require_post_reason, require_second_verify

from .forms import PurchaseOrderForm, PurchaseOrderItemFormSet, PurchaseReceiptForm, PurchaseReceiptItemFormSet
from .forms import PurchaseRequestForm, PurchaseRequestItemFormSet
from .forms import (
    SupplierReturnForm,
    SupplierReturnItemFormSet,
    recalculate_purchase_order_total,
    recalculate_supplier_return_total,
    supplier_return_receipt_item_label,
    supplier_returnable_qty,
)
from .forms import _default_purchase_price
from .import_services import (
    PURCHASE_ORDER_IMPORT_TEMPLATE_ROWS,
    PURCHASE_RECEIPT_IMPORT_TEMPLATE_ROWS,
    PURCHASE_REQUEST_IMPORT_TEMPLATE_ROWS,
    SUPPLIER_RETURN_IMPORT_TEMPLATE_ROWS,
    import_purchase_orders_from_csv,
    import_purchase_receipts_from_csv,
    import_purchase_requests_from_csv,
    import_supplier_returns_from_csv,
)
from .models import PurchaseOrder, PurchaseOrderItem, PurchaseReceipt, PurchaseReceiptItem, PurchaseRequest, PurchaseRequestItem, SupplierReturn, SupplierReturnItem
from .services import confirm_purchase_receipt, confirm_supplier_return_shipment, create_purchase_order_from_request


class PurchaseRequestListView(ErpListView):
    model = PurchaseRequest
    page_title = "采购需求"
    create_url_name = "purchase:purchase_request_create"
    create_permission_required = PermissionCode.PURCHASE_PROCESS
    view_permission_required = (PermissionCode.PURCHASE_VIEW, PermissionCode.PURCHASE_PROCESS)
    permission_denied_message = "缺少采购数据查看权限"
    detail_url_name = "purchase:purchase_request_detail"
    columns = (
        ("需求单号", "purchase_request_no"),
        ("来源", "get_source_type_display"),
        ("状态", "get_status_display"),
        ("需求日期", "needed_date"),
        ("创建时间", "created_at"),
    )
    ordering = ["-created_at"]
    page_actions = (
        ("导出CSV", "purchase:purchase_request_export", ""),
        ("下载导入模板", "purchase:purchase_request_import_template", ""),
        ("导入CSV", "purchase:purchase_request_import", "primary"),
    )
    page_action_permissions = {
        "purchase:purchase_request_import_template": PermissionCode.PURCHASE_PROCESS,
        "purchase:purchase_request_import": PermissionCode.PURCHASE_PROCESS,
    }
    search_fields = ("purchase_request_no",)
    status_filter_field = "status"
    field_filters = (
        {"label": "需求单号", "param": "purchase_request_no", "field": "purchase_request_no", "placeholder": "采购需求单号"},
        {
            "label": "来源",
            "param": "source_type",
            "field": "source_type",
            "lookup": "exact",
            "type": "select",
            "choices": PurchaseRequest.SourceType.choices,
        },
        {"label": "物料编码", "param": "material_code", "field": "items__material__material_code", "placeholder": "需求物料编码", "distinct": True},
        {"label": "物料名称", "param": "material_name", "field": "items__material__material_name", "placeholder": "需求物料名称", "distinct": True},
        {"label": "型号", "param": "material_spec", "field": "items__material__spec", "placeholder": "规格型号", "distinct": True},
        {"label": "建议供应商", "param": "supplier_name", "field": "items__suggested_supplier__supplier_name", "placeholder": "建议供应商", "distinct": True},
    )
    sortable_fields = {
        "purchase_request_no": "purchase_request_no",
        "get_source_type_display": "source_type",
        "get_status_display": "status",
        "needed_date": "needed_date",
        "created_at": "created_at",
    }


class PurchaseRequestImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(PURCHASE_REQUEST_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="purchase_request_import_template.csv"'
        return response


class PurchaseRequestImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入采购需求"
        context["list_url_name"] = "purchase:purchase_request_list"
        context["template_url_name"] = "purchase:purchase_request_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("purchase:purchase_request_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_purchase_requests_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("purchase:purchase_request_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class PurchaseRequestCreateView(LoginRequiredMixin, CreateView):
    model = PurchaseRequest
    form_class = PurchaseRequestForm
    template_name = "purchase/purchase_request_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建采购需求"
        context["can_submit_for_approval"] = _can_process_purchase(self.request.user)
        if self.request.POST:
            context["item_formset"] = PurchaseRequestItemFormSet(self.request.POST, instance=self.object)
        else:
            context["item_formset"] = PurchaseRequestItemFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data(form=form)
        item_formset = PurchaseRequestItemFormSet(self.request.POST, instance=self.object)
        submit_for_approval = self.request.POST.get("action") == "submit"
        if submit_for_approval:
            require_erp_permission(self.request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        if not item_formset.is_valid():
            context["item_formset"] = item_formset
            return self.render_to_response(context)

        with transaction.atomic():
            self.object = form.save(commit=False, user=self.request.user)
            self.object.source_type = PurchaseRequest.SourceType.MANUAL
            self.object.status = PurchaseRequest.Status.PENDING_APPROVAL if submit_for_approval else PurchaseRequest.Status.DRAFT
            self.object.save()
            item_formset.instance = self.object
            item_formset.save()

        messages.success(self.request, "采购需求已保存")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("purchase:purchase_request_detail", kwargs={"pk": self.object.pk})


class PurchaseRequestDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseRequest
    template_name = "purchase/purchase_request_detail.html"
    context_object_name = "purchase_request"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, PurchaseRequestListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("requested_by")
            .prefetch_related("items__material", "items__suggested_supplier", "items__source_shortage_alert", "items__source_sales_order_item")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_purchase = _can_process_purchase(self.request.user)
        context["page_title"] = f"采购需求 {self.object.purchase_request_no}"
        context["suppliers"] = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by("supplier_no")
        context["can_edit"] = can_process_purchase and self.object.source_type == PurchaseRequest.SourceType.MANUAL and self.object.status in [
            PurchaseRequest.Status.DRAFT,
            PurchaseRequest.Status.REJECTED,
        ]
        context["can_void"] = can_process_purchase and self.object.status in [
            PurchaseRequest.Status.DRAFT,
            PurchaseRequest.Status.PENDING_APPROVAL,
            PurchaseRequest.Status.REJECTED,
        ]
        context["can_create_order"] = can_process_purchase and self.object.items.filter(
            line_status__in=["open", "partial_ordered"]
        ).exists()
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "purchase_request",
            self.object.id,
            self.object.purchase_request_no,
        )
        return context


class PurchaseRequestUpdateView(LoginRequiredMixin, View):
    editable_statuses = [PurchaseRequest.Status.DRAFT, PurchaseRequest.Status.REJECTED]

    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        purchase_request = self._get_request(pk)
        if not purchase_request:
            messages.error(request, "采购需求不存在")
            return redirect("purchase:purchase_request_list")
        if not self._can_edit(purchase_request):
            messages.error(request, "只有人工创建的草稿或已驳回采购需求可以编辑")
            return redirect("purchase:purchase_request_detail", pk=pk)
        form = PurchaseRequestForm(instance=purchase_request)
        item_formset = PurchaseRequestItemFormSet(instance=purchase_request)
        return self._render(request, purchase_request, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        purchase_request = self._get_request(pk)
        if not purchase_request:
            messages.error(request, "采购需求不存在")
            return redirect("purchase:purchase_request_list")
        if not self._can_edit(purchase_request):
            messages.error(request, "只有人工创建的草稿或已驳回采购需求可以编辑")
            return redirect("purchase:purchase_request_detail", pk=pk)

        form = PurchaseRequestForm(request.POST, instance=purchase_request)
        item_formset = PurchaseRequestItemFormSet(request.POST, instance=purchase_request)
        submit_for_approval = request.POST.get("action") == "submit"
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, purchase_request, form, item_formset)
        submitted_request = form.save(commit=False, user=request.user)

        with transaction.atomic():
            purchase_request = PurchaseRequest.objects.select_for_update().prefetch_related("items__material").get(pk=pk)
            if not self._can_edit(purchase_request):
                messages.error(request, "只有人工创建的草稿或已驳回采购需求可以编辑")
                return redirect("purchase:purchase_request_detail", pk=pk)
            before_snapshot = _purchase_request_snapshot(purchase_request)
            purchase_request.needed_date = submitted_request.needed_date
            purchase_request.remark = submitted_request.remark
            purchase_request.status = PurchaseRequest.Status.PENDING_APPROVAL if submit_for_approval else PurchaseRequest.Status.DRAFT
            purchase_request.save(update_fields=["needed_date", "remark", "status"])
            item_formset.instance = purchase_request
            item_formset.save()
            after_snapshot = {
                **_purchase_request_snapshot(purchase_request),
                "operation_reason": optional_post_reason(request, default="页面编辑采购需求"),
            }

        record_audit_log_from_request(
            request,
            "purchase_request_update",
            "purchase_request",
            purchase_request.id,
            purchase_request.purchase_request_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "采购需求已更新")
        return redirect("purchase:purchase_request_detail", pk=purchase_request.pk)

    def _get_request(self, pk):
        return PurchaseRequest.objects.prefetch_related("items__material", "items__suggested_supplier").filter(pk=pk).first()

    def _can_edit(self, purchase_request):
        return (
            purchase_request.source_type == PurchaseRequest.SourceType.MANUAL
            and purchase_request.status in self.editable_statuses
            and not purchase_request.items.exclude(line_status=PurchaseRequestItem.LineStatus.OPEN).exists()
        )

    def _render(self, request, purchase_request, form, item_formset):
        return render(
            request,
            "purchase/purchase_request_form.html",
            {
                "page_title": f"编辑采购需求 {purchase_request.purchase_request_no}",
                "form": form,
                "item_formset": item_formset,
                "purchase_request": purchase_request,
                "can_submit_for_approval": _can_process_purchase(request.user),
                "is_edit": True,
            },
        )


class PurchaseRequestVoidView(LoginRequiredMixin, View):
    voidable_statuses = [PurchaseRequest.Status.DRAFT, PurchaseRequest.Status.PENDING_APPROVAL, PurchaseRequest.Status.REJECTED]

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        verification_response = require_second_verify(request, "purchase:purchase_request_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "purchase:purchase_request_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写采购需求作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                purchase_request = PurchaseRequest.objects.select_for_update().prefetch_related("items__material").get(pk=pk)
                if purchase_request.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿、待审核或已驳回采购需求可以作废")
                    return redirect("purchase:purchase_request_detail", pk=pk)
                if purchase_request.items.exclude(line_status=PurchaseRequestItem.LineStatus.OPEN).exists():
                    messages.error(request, "已有下游采购单的采购需求不能作废")
                    return redirect("purchase:purchase_request_detail", pk=pk)
                before_snapshot = _purchase_request_snapshot(purchase_request)
                purchase_request.status = PurchaseRequest.Status.VOIDED
                purchase_request.items.update(line_status=PurchaseRequestItem.LineStatus.CLOSED)
                purchase_request.save(update_fields=["status"])
                after_snapshot = _purchase_request_snapshot(purchase_request)
        except PurchaseRequest.DoesNotExist:
            messages.error(request, "采购需求不存在")
            return redirect("purchase:purchase_request_list")

        record_audit_log_from_request(
            request,
            "purchase_request_void",
            "purchase_request",
            purchase_request.id,
            purchase_request.purchase_request_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "采购需求已作废")
        return redirect("purchase:purchase_request_detail", pk=pk)


class PurchaseOrderListView(ErpListView):
    model = PurchaseOrder
    page_title = "采购单"
    create_url_name = "purchase:purchase_order_create"
    create_permission_required = PermissionCode.PURCHASE_PROCESS
    view_permission_required = (PermissionCode.PURCHASE_VIEW, PermissionCode.PURCHASE_PROCESS, PermissionCode.FINANCE_VIEW_AMOUNT)
    permission_denied_message = "缺少采购数据查看权限"
    detail_url_name = "purchase:purchase_order_detail"
    columns = (
        ("采购单号", "purchase_order_no"),
        ("供应商", "supplier.supplier_name"),
        ("订单日期", "order_date"),
        ("负责人", "purchase_owner"),
        ("状态", "get_status_display"),
        ("金额", "total_amount"),
    )
    sensitive_columns = ("total_amount",)
    ordering = ["-order_date", "-id"]
    page_actions = (
        ("导出CSV", "purchase:purchase_order_export", ""),
        ("下载导入模板", "purchase:purchase_order_import_template", ""),
        ("导入CSV", "purchase:purchase_order_import", "primary"),
    )
    page_action_permissions = {
        "purchase:purchase_order_import_template": PermissionCode.PURCHASE_PROCESS,
        "purchase:purchase_order_import": PermissionCode.PURCHASE_PROCESS,
    }
    search_fields = ("purchase_order_no", "supplier__supplier_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "采购单号", "param": "purchase_order_no", "field": "purchase_order_no", "placeholder": "采购单号"},
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
        {"label": "负责人", "param": "purchase_owner", "field": "purchase_owner__username", "placeholder": "负责人账号"},
        {"label": "物料编码", "param": "material_code", "field": "items__material__material_code", "placeholder": "采购物料编码", "distinct": True},
        {"label": "物料名称", "param": "material_name", "field": "items__material__material_name", "placeholder": "采购物料名称", "distinct": True},
        {"label": "型号", "param": "material_spec", "field": "items__material__spec", "placeholder": "规格型号", "distinct": True},
    )
    sortable_fields = {
        "purchase_order_no": "purchase_order_no",
        "supplier.supplier_name": "supplier__supplier_name",
        "order_date": "order_date",
        "purchase_owner": "purchase_owner__username",
        "get_status_display": "status",
        "total_amount": "total_amount",
    }

    def get_queryset(self):
        return _filter_purchase_order_queryset_for_user(super().get_queryset(), self.request.user).select_related("supplier", "purchase_owner")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not can_view_amount(self.request.user)
        return context

    def get_scope_filter_options(self):
        if _can_view_all_purchase(self.request.user):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            return queryset.filter(Q(purchase_owner=self.request.user) | Q(created_by=self.request.user)).distinct()
        if scope_value == "unassigned" and _can_view_all_purchase(self.request.user):
            return queryset.filter(purchase_owner__isnull=True)
        return queryset


class PurchaseCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()

    def dispatch(self, request, *args, **kwargs):
        required_permissions = getattr(self.list_view_class, "view_permission_required", ())
        if request.user.is_authenticated and required_permissions:
            require_any_erp_permission(request.user, required_permissions, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        list_view = self.list_view_class()
        list_view.request = self.request
        queryset = list_view.get_queryset()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        queryset = queryset.order_by(*self.get_ordering(list_view))
        return queryset

    def get_ordering(self, list_view):
        return list_view.current_ordering() or self.ordering

    def get_mask_fields(self):
        if can_view_amount(self.request.user):
            return ()
        return self.list_view_class.sensitive_columns

    def get(self, request):
        result = export_queryset_to_csv(
            self.module,
            self.get_queryset(),
            self.list_view_class.columns,
            request.user.id,
            filter_json={"ordering": ",".join(self.get_ordering(self._list_view_for_request())), "query": request.GET.dict()},
            mask_fields=self.get_mask_fields(),
        )
        return export_file_response(result)

    def _list_view_for_request(self):
        list_view = self.list_view_class()
        list_view.request = self.request
        return list_view


class PurchaseOrderExportView(PurchaseCsvExportView):
    module = "purchase_orders"
    list_view_class = PurchaseOrderListView
    ordering = ("-order_date", "-id")
    select_related = ("supplier",)


class PurchaseOrderImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(PURCHASE_ORDER_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="purchase_order_import_template.csv"'
        return response


class PurchaseOrderImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入采购单"
        context["list_url_name"] = "purchase:purchase_order_list"
        context["template_url_name"] = "purchase:purchase_order_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("purchase:purchase_order_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_purchase_orders_from_csv(
            text_file,
            request.user.id,
            can_import_amount=can_view_amount(request.user),
        )
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("purchase:purchase_order_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class PurchaseRequestExportView(PurchaseCsvExportView):
    module = "purchase_requests"
    list_view_class = PurchaseRequestListView
    ordering = ("-created_at",)


class PurchaseOrderCreateView(LoginRequiredMixin, CreateView):
    model = PurchaseOrder
    form_class = PurchaseOrderForm
    template_name = "purchase/purchase_order_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建采购单"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_submit_for_approval"] = _can_process_purchase(self.request.user)
        supplier = self._supplier_from_request()
        if self.request.POST:
            context["item_formset"] = PurchaseOrderItemFormSet(
                self.request.POST,
                instance=self.object,
                supplier=supplier,
                can_edit_amount=context["can_view_amount"],
            )
        else:
            context["item_formset"] = PurchaseOrderItemFormSet(
                instance=self.object,
                supplier=supplier,
                can_edit_amount=context["can_view_amount"],
            )
        return context

    def form_valid(self, form):
        supplier = form.cleaned_data.get("supplier")
        context = self.get_context_data(form=form)
        item_formset = PurchaseOrderItemFormSet(
            self.request.POST,
            instance=self.object,
            supplier=supplier,
            can_edit_amount=can_view_amount(self.request.user),
        )
        submit_for_approval = self.request.POST.get("action") == "submit"
        if submit_for_approval:
            require_erp_permission(self.request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        if not item_formset.is_valid():
            context["item_formset"] = item_formset
            return self.render_to_response(context)

        with transaction.atomic():
            self.object = form.save(commit=False, user=self.request.user)
            self.object.status = PurchaseOrder.Status.PENDING_APPROVAL if submit_for_approval else PurchaseOrder.Status.DRAFT
            self.object.save()
            item_formset.instance = self.object
            item_formset.save()
            recalculate_purchase_order_total(self.object)

        messages.success(self.request, "采购单已保存")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("purchase:purchase_order_detail", kwargs={"pk": self.object.pk})

    def _supplier_from_request(self):
        supplier_id = self.request.POST.get("supplier") or self.request.GET.get("supplier")
        if supplier_id:
            return Supplier.objects.filter(pk=supplier_id).first()
        return None


class PurchaseOrderUpdateView(LoginRequiredMixin, View):
    editable_statuses = [PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.REJECTED]

    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        order = self._get_order(pk)
        if not order:
            messages.error(request, "采购单不存在")
            return redirect("purchase:purchase_order_list")
        if order.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回采购单可以编辑")
            return redirect("purchase:purchase_order_detail", pk=pk)
        form = PurchaseOrderForm(instance=order)
        item_formset = PurchaseOrderItemFormSet(
            instance=order,
            supplier=order.supplier,
            can_edit_amount=can_view_amount(request.user),
        )
        return self._render(request, order, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        order = self._get_order(pk)
        if not order:
            messages.error(request, "采购单不存在")
            return redirect("purchase:purchase_order_list")
        if order.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回采购单可以编辑")
            return redirect("purchase:purchase_order_detail", pk=pk)

        form = PurchaseOrderForm(request.POST, instance=order)
        supplier = Supplier.objects.filter(pk=request.POST.get("supplier")).first() or order.supplier
        item_formset = PurchaseOrderItemFormSet(
            request.POST,
            instance=order,
            supplier=supplier,
            can_edit_amount=can_view_amount(request.user),
        )
        submit_for_approval = request.POST.get("action") == "submit"
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, order, form, item_formset)
        submitted_order = form.save(commit=False, user=request.user)

        with transaction.atomic():
            order = _filter_purchase_order_queryset_for_user(
                PurchaseOrder.objects.select_for_update().prefetch_related("items__material"), request.user
            ).get(pk=pk)
            if order.status not in self.editable_statuses:
                messages.error(request, "只有草稿或已驳回采购单可以编辑")
                return redirect("purchase:purchase_order_detail", pk=pk)
            before_snapshot = _purchase_order_snapshot(order)
            order.supplier = submitted_order.supplier
            order.order_date = submitted_order.order_date
            order.remark = submitted_order.remark
            order.status = PurchaseOrder.Status.PENDING_APPROVAL if submit_for_approval else PurchaseOrder.Status.DRAFT
            order.save()
            item_formset.instance = order
            item_formset.save()
            recalculate_purchase_order_total(order)
            after_snapshot = {
                **_purchase_order_snapshot(order),
                "operation_reason": optional_post_reason(request, default="页面编辑采购单"),
            }

        record_audit_log_from_request(
            request,
            "purchase_order_update",
            "purchase_order",
            order.id,
            order.purchase_order_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "采购单已更新")
        return redirect("purchase:purchase_order_detail", pk=order.pk)

    def _get_order(self, pk, for_update=False):
        queryset = PurchaseOrder.objects.prefetch_related("items__material")
        if for_update:
            queryset = queryset.select_for_update()
        return _filter_purchase_order_queryset_for_user(queryset, self.request.user).filter(pk=pk).first()

    def _render(self, request, order, form, item_formset):
        return render(
            request,
            "purchase/purchase_order_form.html",
            {
                "page_title": f"编辑采购单 {order.purchase_order_no}",
                "form": form,
                "item_formset": item_formset,
                "can_view_amount": can_view_amount(request.user),
                "can_submit_for_approval": _can_process_purchase(request.user),
                "purchase_order": order,
                "is_edit": True,
            },
        )


class PurchaseOrderDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseOrder
    template_name = "purchase/purchase_order_detail.html"
    context_object_name = "purchase_order"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, PurchaseOrderListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("supplier", "created_by")
            .prefetch_related("items__material", "items__purchase_request_item", "receipts")
        )
        return _filter_purchase_order_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_purchase = _can_process_purchase(self.request.user)
        context["page_title"] = f"采购单 {self.object.purchase_order_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_edit"] = can_process_purchase and self.object.status in [PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.REJECTED]
        context["can_void"] = can_process_purchase and self.object.status in [
            PurchaseOrder.Status.DRAFT,
            PurchaseOrder.Status.PENDING_APPROVAL,
            PurchaseOrder.Status.REJECTED,
        ]
        context["can_add_item"] = can_process_purchase and self.object.status in [PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.REJECTED]
        context["can_create_receipt"] = can_process_purchase and self.object.status in [
            PurchaseOrder.Status.APPROVED,
            PurchaseOrder.Status.PARTIAL_RECEIVED,
        ] and any(
            item.order_qty > item.received_qty and item.line_status != PurchaseOrderItem.LineStatus.CLOSED
            for item in self.object.items.all()
        )
        context["materials"] = Material.objects.filter(status=Material.MaterialStatus.ACTIVE).order_by("material_code")
        context["locations"] = WarehouseLocation.objects.filter(status=WarehouseLocation.LocationStatus.ACTIVE).order_by("location_code")
        context["today"] = timezone.localdate()
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "purchase_order",
            self.object.id,
            self.object.purchase_order_no,
        )
        return context


class PurchaseOrderPrintView(LoginRequiredMixin, DetailView):
    model = PurchaseOrder
    template_name = "purchase/purchase_order_print.html"
    context_object_name = "purchase_order"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, PurchaseOrderListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("supplier", "created_by")
            .prefetch_related("items__material", "items__purchase_request_item__purchase_request")
        )
        return _filter_purchase_order_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印采购单 {self.object.purchase_order_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        record_print_log(
            template_type="purchase_order",
            source_doc_type="purchase_order",
            source_doc_id=self.object.id,
            source_doc_no=self.object.purchase_order_no,
            printed_by_id=self.request.user.id,
        )
        return context


class PurchaseOrderVoidView(LoginRequiredMixin, View):
    voidable_statuses = [PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.PENDING_APPROVAL, PurchaseOrder.Status.REJECTED]

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        verification_response = require_second_verify(request, "purchase:purchase_order_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "purchase:purchase_order_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写采购单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                order = _filter_purchase_order_queryset_for_user(
                    PurchaseOrder.objects.select_for_update().prefetch_related("items__material"), request.user
                ).get(pk=pk)
                if order.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿、待审核或已驳回采购单可以作废")
                    return redirect("purchase:purchase_order_detail", pk=pk)
                before_snapshot = _purchase_order_snapshot(order)
                order.status = PurchaseOrder.Status.VOIDED
                order.save(update_fields=["status"])
                after_snapshot = _purchase_order_snapshot(order)
        except PurchaseOrder.DoesNotExist:
            messages.error(request, "采购单不存在")
            return redirect("purchase:purchase_order_list")

        record_audit_log_from_request(
            request,
            "purchase_order_void",
            "purchase_order",
            order.id,
            order.purchase_order_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "采购单已作废")
        return redirect("purchase:purchase_order_detail", pk=pk)


class PurchaseOrderItemCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        from decimal import Decimal, InvalidOperation

        try:
            order = _filter_purchase_order_queryset_for_user(PurchaseOrder.objects.all(), request.user).get(pk=pk)
        except PurchaseOrder.DoesNotExist:
            messages.error(request, "采购单不存在")
            return redirect("purchase:purchase_order_list")

        if order.status not in [PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.REJECTED]:
            messages.error(request, "当前采购单状态不能新增明细")
            return redirect("purchase:purchase_order_detail", pk=pk)

        material_id = request.POST.get("material")
        try:
            order_qty = Decimal(request.POST.get("order_qty", ""))
            unit_price_text = request.POST.get("unit_price", "")
            unit_price = Decimal(unit_price_text) if unit_price_text != "" else None
        except (InvalidOperation, TypeError):
            messages.error(request, "采购数量和单价必须正确填写")
            return redirect("purchase:purchase_order_detail", pk=pk)

        if not material_id or order_qty <= 0 or (unit_price is not None and unit_price < 0):
            messages.error(request, "物料、采购数量和单价必须正确填写")
            return redirect("purchase:purchase_order_detail", pk=pk)
        if order.items.filter(material_id=material_id).exists():
            messages.error(request, "同一采购单中同一物料不能重复")
            return redirect("purchase:purchase_order_detail", pk=pk)
        material = Material.objects.filter(pk=material_id, status=Material.MaterialStatus.ACTIVE).first()
        if not material:
            messages.error(request, "物料不存在或已停用")
            return redirect("purchase:purchase_order_detail", pk=pk)
        if unit_price is None or not can_view_amount(request.user):
            unit_price = _default_purchase_price(material, order.supplier)

        line_no = (order.items.order_by("-line_no").values_list("line_no", flat=True).first() or 0) + 1
        PurchaseOrderItem.objects.create(
            purchase_order=order,
            line_no=line_no,
            material=material,
            order_qty=order_qty,
            unit_price=unit_price,
            line_amount=(order_qty * unit_price).quantize(Decimal("0.01")),
            needed_date=parse_user_date(request.POST.get("needed_date")),
        )
        recalculate_purchase_order_total(order)
        messages.success(request, "采购明细已新增")
        return redirect("purchase:purchase_order_detail", pk=pk)


class PurchaseOrderCreateReceiptView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        location = WarehouseLocation.objects.filter(
            pk=request.POST.get("location"),
            status=WarehouseLocation.LocationStatus.ACTIVE,
        ).first()
        if not location:
            messages.error(request, "请选择有效入库库位")
            return redirect("purchase:purchase_order_detail", pk=pk)

        receipt_date = parse_user_date(request.POST.get("receipt_date"), default=timezone.localdate())

        with transaction.atomic():
            try:
                order = _filter_purchase_order_queryset_for_user(
                    PurchaseOrder.objects.select_for_update().select_related("supplier"), request.user
                ).get(pk=pk)
            except PurchaseOrder.DoesNotExist:
                messages.error(request, "采购单不存在")
                return redirect("purchase:purchase_order_list")

            if order.status not in [PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL_RECEIVED]:
                messages.error(request, "只有已通过或部分到货的采购单可以生成进货单")
                return redirect("purchase:purchase_order_detail", pk=pk)

            open_items = list(
                PurchaseOrderItem.objects.select_for_update()
                .filter(purchase_order=order, order_qty__gt=F("received_qty"))
                .exclude(line_status=PurchaseOrderItem.LineStatus.CLOSED)
                .select_related("material")
                .order_by("line_no")
            )
            if not open_items:
                messages.error(request, "采购单没有未到货明细")
                return redirect("purchase:purchase_order_detail", pk=pk)

            receipt = PurchaseReceipt.objects.create(
                purchase_receipt_no=next_document_no("GR"),
                purchase_order=order,
                supplier=order.supplier,
                receipt_date=receipt_date,
                status=PurchaseReceipt.Status.PENDING_RECEIVE,
                created_by=request.user,
                remark=request.POST.get("remark", "").strip(),
            )
            for item in open_items:
                remaining_qty = item.order_qty - item.received_qty
                PurchaseReceiptItem.objects.create(
                    purchase_receipt=receipt,
                    purchase_order_item=item,
                    material=item.material,
                    received_qty=remaining_qty,
                    accepted_qty=remaining_qty,
                    rejected_qty=0,
                    unit_price=item.unit_price,
                    location=location,
                )

        messages.success(request, "进货单已生成")
        return redirect("purchase:purchase_receipt_detail", pk=receipt.pk)


class PurchaseRequestCreateOrderView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        supplier_id = request.POST.get("supplier")
        if not supplier_id:
            messages.error(request, "请选择供应商")
            return redirect("purchase:purchase_request_detail", pk=pk)
        result = create_purchase_order_from_request(
            pk,
            int(supplier_id),
            request.user.id,
            idempotency_key=f"pr-to-po:{pk}:{supplier_id}:{request.user.id}",
        )
        if result.success:
            messages.success(request, result.message)
            return redirect("purchase:purchase_order_detail", pk=result.data["purchase_order_id"])
        messages.error(request, result.message or result.error_code or "采购单生成失败")
        return redirect("purchase:purchase_request_detail", pk=pk)


class PurchaseReceiptListView(ErpListView):
    model = PurchaseReceipt
    page_title = "进货单"
    view_permission_required = (PermissionCode.PURCHASE_VIEW, PermissionCode.PURCHASE_PROCESS, PermissionCode.FINANCE_VIEW_AMOUNT)
    permission_denied_message = "缺少采购数据查看权限"
    detail_url_name = "purchase:purchase_receipt_detail"
    columns = (
        ("进货单号", "purchase_receipt_no"),
        ("采购单", "purchase_order.purchase_order_no"),
        ("供应商", "supplier.supplier_name"),
        ("进货日期", "receipt_date"),
        ("状态", "get_status_display"),
    )
    ordering = ["-receipt_date", "-id"]
    page_actions = (
        ("导出CSV", "purchase:purchase_receipt_export", ""),
        ("下载导入模板", "purchase:purchase_receipt_import_template", ""),
        ("导入CSV", "purchase:purchase_receipt_import", "primary"),
    )
    page_action_permissions = {
        "purchase:purchase_receipt_import_template": PermissionCode.PURCHASE_PROCESS,
        "purchase:purchase_receipt_import": PermissionCode.PURCHASE_PROCESS,
    }
    search_fields = ("purchase_receipt_no", "purchase_order__purchase_order_no", "supplier__supplier_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "进货单号", "param": "purchase_receipt_no", "field": "purchase_receipt_no", "placeholder": "进货单号"},
        {"label": "采购单号", "param": "purchase_order_no", "field": "purchase_order__purchase_order_no", "placeholder": "采购单号"},
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
        {"label": "物料编码", "param": "material_code", "field": "items__material__material_code", "placeholder": "进货物料编码", "distinct": True},
        {"label": "物料名称", "param": "material_name", "field": "items__material__material_name", "placeholder": "进货物料名称", "distinct": True},
        {"label": "型号", "param": "material_spec", "field": "items__material__spec", "placeholder": "规格型号", "distinct": True},
        {"label": "库位", "param": "location_code", "field": "items__location__location_code", "placeholder": "入库库位", "distinct": True},
    )
    sortable_fields = {
        "purchase_receipt_no": "purchase_receipt_no",
        "purchase_order.purchase_order_no": "purchase_order__purchase_order_no",
        "supplier.supplier_name": "supplier__supplier_name",
        "receipt_date": "receipt_date",
        "get_status_display": "status",
    }

    def get_queryset(self):
        return _filter_purchase_receipt_queryset_for_user(super().get_queryset(), self.request.user).select_related(
            "purchase_order", "supplier"
        )

    def get_scope_filter_options(self):
        if _can_view_all_purchase(self.request.user):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            return queryset.filter(
                Q(purchase_order__purchase_owner=self.request.user)
                | Q(purchase_order__created_by=self.request.user)
                | Q(created_by=self.request.user)
            ).distinct()
        if scope_value == "unassigned" and _can_view_all_purchase(self.request.user):
            return queryset.filter(purchase_order__purchase_owner__isnull=True)
        return queryset


class PurchaseReceiptExportView(PurchaseCsvExportView):
    module = "purchase_receipts"
    list_view_class = PurchaseReceiptListView
    ordering = ("-receipt_date", "-id")
    select_related = ("purchase_order", "supplier")


class PurchaseReceiptImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(PURCHASE_RECEIPT_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="purchase_receipt_import_template.csv"'
        return response


class PurchaseReceiptImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入进货单"
        context["list_url_name"] = "purchase:purchase_receipt_list"
        context["template_url_name"] = "purchase:purchase_receipt_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("purchase:purchase_receipt_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_purchase_receipts_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("purchase:purchase_receipt_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class PurchaseReceiptDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseReceipt
    template_name = "purchase/purchase_receipt_detail.html"
    context_object_name = "receipt"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, PurchaseReceiptListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("purchase_order", "supplier", "created_by")
            .prefetch_related("items__purchase_order_item", "items__material", "items__location", "items__batch")
        )
        return _filter_purchase_receipt_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_purchase = _can_process_purchase(self.request.user)
        context["page_title"] = f"进货单 {self.object.purchase_receipt_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_edit"] = can_process_purchase and self.object.status in [
            PurchaseReceipt.Status.PENDING_RECEIVE,
            PurchaseReceipt.Status.PARTIAL_RECEIVED,
        ] and not self.object.items.filter(batch__isnull=False).exists()
        context["can_confirm"] = can_process_purchase and self.object.status in [
            PurchaseReceipt.Status.PENDING_RECEIVE,
            PurchaseReceipt.Status.PARTIAL_RECEIVED,
        ]
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "purchase_receipt",
            self.object.id,
            self.object.purchase_receipt_no,
        )
        return context


class PurchaseReceiptUpdateView(LoginRequiredMixin, View):
    editable_statuses = [PurchaseReceipt.Status.PENDING_RECEIVE, PurchaseReceipt.Status.PARTIAL_RECEIVED]

    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        receipt = self._get_receipt(pk)
        if not receipt:
            messages.error(request, "进货单不存在")
            return redirect("purchase:purchase_receipt_list")
        if not self._can_edit(receipt):
            messages.error(request, "只有未确认入库且未生成批次的进货单可以编辑")
            return redirect("purchase:purchase_receipt_detail", pk=pk)
        form = PurchaseReceiptForm(instance=receipt)
        item_formset = PurchaseReceiptItemFormSet(instance=receipt)
        return self._render(request, receipt, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        receipt = self._get_receipt(pk)
        if not receipt:
            messages.error(request, "进货单不存在")
            return redirect("purchase:purchase_receipt_list")
        if not self._can_edit(receipt):
            messages.error(request, "只有未确认入库且未生成批次的进货单可以编辑")
            return redirect("purchase:purchase_receipt_detail", pk=pk)

        form = PurchaseReceiptForm(request.POST, instance=receipt)
        item_formset = PurchaseReceiptItemFormSet(request.POST, instance=receipt)
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, receipt, form, item_formset)
        submitted_receipt = form.save(commit=False)

        with transaction.atomic():
            receipt = (
                _filter_purchase_receipt_queryset_for_user(
                    PurchaseReceipt.objects.select_for_update(),
                    request.user,
                )
                .prefetch_related("items__material", "items__location", "items__purchase_order_item")
                .get(pk=pk)
            )
            if not self._can_edit(receipt):
                messages.error(request, "只有未确认入库且未生成批次的进货单可以编辑")
                return redirect("purchase:purchase_receipt_detail", pk=pk)
            before_snapshot = _purchase_receipt_snapshot(receipt)
            receipt.receipt_date = submitted_receipt.receipt_date
            receipt.remark = submitted_receipt.remark
            receipt.save(update_fields=["receipt_date", "remark"])
            item_formset.instance = receipt
            item_formset.save()
            after_snapshot = {
                **_purchase_receipt_snapshot(receipt),
                "operation_reason": optional_post_reason(request, default="页面编辑进货单"),
            }

        record_audit_log_from_request(
            request,
            "purchase_receipt_update",
            "purchase_receipt",
            receipt.id,
            receipt.purchase_receipt_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "进货单已更新")
        return redirect("purchase:purchase_receipt_detail", pk=receipt.pk)

    def _get_receipt(self, pk):
        queryset = (
            PurchaseReceipt.objects.select_related("purchase_order", "supplier", "created_by")
            .prefetch_related("items__material", "items__location", "items__purchase_order_item")
        )
        return _filter_purchase_receipt_queryset_for_user(queryset, self.request.user).filter(pk=pk).first()

    def _can_edit(self, receipt):
        return receipt.status in self.editable_statuses and not receipt.items.filter(batch__isnull=False).exists()

    def _render(self, request, receipt, form, item_formset):
        return render(
            request,
            "purchase/purchase_receipt_form.html",
            {
                "page_title": f"编辑进货单 {receipt.purchase_receipt_no}",
                "form": form,
                "item_formset": item_formset,
                "receipt": receipt,
                "can_view_amount": can_view_amount(request.user),
            },
        )


class PurchaseReceiptPrintView(LoginRequiredMixin, DetailView):
    model = PurchaseReceipt
    template_name = "purchase/purchase_receipt_print.html"
    context_object_name = "receipt"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, PurchaseReceiptListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("purchase_order", "supplier", "created_by")
            .prefetch_related("items__purchase_order_item", "items__material", "items__location", "items__batch")
        )
        return _filter_purchase_receipt_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印进货单 {self.object.purchase_receipt_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        record_print_log(
            template_type="purchase_receipt",
            source_doc_type="purchase_receipt",
            source_doc_id=self.object.id,
            source_doc_no=self.object.purchase_receipt_no,
            printed_by_id=self.request.user.id,
        )
        return context


class PurchaseReceiptConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        if not _filter_purchase_receipt_queryset_for_user(PurchaseReceipt.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "进货单不存在")
            return redirect("purchase:purchase_receipt_list")
        verification_response = require_second_verify(request, "purchase:purchase_receipt_detail", pk)
        if verification_response:
            return verification_response
        result = confirm_purchase_receipt(pk, request.user.id, f"purchase-receipt:{pk}")
        if result.success:
            record_audit_log_from_request(request, "purchase_receipt_confirm", "purchase_receipt", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "进货入库确认失败")
        return redirect("purchase:purchase_receipt_detail", pk=pk)


class SupplierReturnListView(ErpListView):
    model = SupplierReturn
    page_title = "供应商退货"
    create_url_name = "purchase:supplier_return_create"
    create_permission_required = PermissionCode.PURCHASE_PROCESS
    view_permission_required = (PermissionCode.PURCHASE_VIEW, PermissionCode.PURCHASE_PROCESS, PermissionCode.FINANCE_VIEW_AMOUNT)
    permission_denied_message = "缺少采购数据查看权限"
    detail_url_name = "purchase:supplier_return_detail"
    columns = (
        ("退货单号", "supplier_return_no"),
        ("供应商", "supplier.supplier_name"),
        ("退货日期", "return_date"),
        ("状态", "get_status_display"),
        ("金额", "return_amount"),
    )
    sensitive_columns = ("return_amount",)
    ordering = ["-return_date", "-id"]
    page_actions = (
        ("导出CSV", "purchase:supplier_return_export", ""),
        ("下载导入模板", "purchase:supplier_return_import_template", ""),
        ("导入CSV", "purchase:supplier_return_import", "primary"),
    )
    page_action_permissions = {
        "purchase:supplier_return_import_template": PermissionCode.PURCHASE_PROCESS,
        "purchase:supplier_return_import": PermissionCode.PURCHASE_PROCESS,
    }
    search_fields = ("supplier_return_no", "supplier__supplier_name", "purchase_receipt__purchase_receipt_no")
    status_filter_field = "status"
    field_filters = (
        {"label": "退货单号", "param": "supplier_return_no", "field": "supplier_return_no", "placeholder": "供应商退货单号"},
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
        {"label": "进货单号", "param": "purchase_receipt_no", "field": "purchase_receipt__purchase_receipt_no", "placeholder": "进货单号"},
        {"label": "物料编码", "param": "material_code", "field": "items__material__material_code", "placeholder": "退货物料编码", "distinct": True},
        {"label": "物料名称", "param": "material_name", "field": "items__material__material_name", "placeholder": "退货物料名称", "distinct": True},
        {"label": "型号", "param": "material_spec", "field": "items__material__spec", "placeholder": "规格型号", "distinct": True},
    )

    def get_queryset(self):
        return _filter_supplier_return_queryset_for_user(
            super().get_queryset(),
            self.request.user,
        ).select_related("supplier", "purchase_receipt", "purchase_receipt__purchase_order")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not can_view_amount(self.request.user)
        return context

    def get_scope_filter_options(self):
        if _can_view_all_purchase(self.request.user):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            return queryset.filter(
                Q(purchase_receipt__purchase_order__purchase_owner=self.request.user)
                | Q(purchase_receipt__purchase_order__created_by=self.request.user)
                | Q(purchase_receipt__created_by=self.request.user)
                | Q(created_by=self.request.user)
            ).distinct()
        if scope_value == "unassigned" and _can_view_all_purchase(self.request.user):
            return queryset.filter(Q(purchase_receipt__purchase_order__purchase_owner__isnull=True) | Q(purchase_receipt__isnull=True))
        return queryset


class SupplierReturnExportView(PurchaseCsvExportView):
    module = "supplier_returns"
    list_view_class = SupplierReturnListView
    ordering = ("-return_date", "-id")
    select_related = ("supplier", "purchase_receipt")


class SupplierReturnImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(SUPPLIER_RETURN_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="supplier_return_import_template.csv"'
        return response


class SupplierReturnImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入供应商退货"
        context["list_url_name"] = "purchase:supplier_return_list"
        context["template_url_name"] = "purchase:supplier_return_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("purchase:supplier_return_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_supplier_returns_from_csv(
            text_file,
            request.user.id,
            can_import_amount=can_view_amount(request.user),
        )
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("purchase:supplier_return_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class SupplierReturnCreateView(LoginRequiredMixin, CreateView):
    model = SupplierReturn
    form_class = SupplierReturnForm
    template_name = "purchase/supplier_return_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["purchase_receipt_queryset"] = _supplier_return_receipt_queryset(self.request.user)
        kwargs.setdefault("initial", {})
        if self.request.GET.get("show_all_receipts") == "1":
            kwargs["initial"]["show_all_receipts"] = True
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建供应商退货"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_submit_for_approval"] = _can_process_purchase(self.request.user)
        if "item_formset" in context:
            return context
        if self.request.POST:
            form = context.get("form")
            form_valid = form and form.is_valid()
            supplier = form.cleaned_data.get("supplier") if form_valid else None
            receipt = form.cleaned_data.get("purchase_receipt") if form_valid else None
            context["item_formset"] = SupplierReturnItemFormSet(
                self.request.POST,
                instance=self.object,
                supplier=supplier,
                purchase_receipt=receipt,
                require_ready=self.request.POST.get("action") == "submit",
                can_edit_amount=context["can_view_amount"],
            )
        else:
            context["item_formset"] = SupplierReturnItemFormSet(
                instance=self.object,
                can_edit_amount=context["can_view_amount"],
            )
        return context

    def form_valid(self, form):
        submit_for_approval = self.request.POST.get("action") == "submit"
        if submit_for_approval:
            require_erp_permission(self.request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        item_formset = SupplierReturnItemFormSet(
            self.request.POST,
            instance=self.object,
            supplier=form.cleaned_data.get("supplier"),
            purchase_receipt=form.cleaned_data.get("purchase_receipt"),
            require_ready=submit_for_approval,
            can_edit_amount=can_view_amount(self.request.user),
        )
        if not item_formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, item_formset=item_formset))

        with transaction.atomic():
            self.object = form.save(commit=False, user=self.request.user)
            self.object.status = SupplierReturn.Status.PENDING_APPROVAL if submit_for_approval else SupplierReturn.Status.DRAFT
            self.object.save()
            item_formset.instance = self.object
            item_formset.save()
            recalculate_supplier_return_total(self.object)

        messages.success(self.request, "供应商退货单已保存")
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        item_formset = SupplierReturnItemFormSet(
            self.request.POST,
            instance=self.object,
            can_edit_amount=can_view_amount(self.request.user),
        )
        return self.render_to_response(self.get_context_data(form=form, item_formset=item_formset))

    def get_success_url(self):
        return reverse("purchase:supplier_return_detail", kwargs={"pk": self.object.pk})


class SupplierReturnReceiptItemsView(LoginRequiredMixin, View):
    def get(self, request):
        require_any_erp_permission(request.user, SupplierReturnListView.view_permission_required, "缺少采购数据查看权限")
        receipt_id = request.GET.get("purchase_receipt")
        if not receipt_id:
            return JsonResponse({"items": []})
        receipt = (
            _supplier_return_receipt_queryset(request.user)
            .filter(pk=receipt_id)
            .select_related("supplier")
            .first()
        )
        if not receipt:
            return JsonResponse({"items": []})
        items = (
            PurchaseReceiptItem.objects.select_related("material", "batch", "location")
            .filter(purchase_receipt=receipt, accepted_qty__gt=0)
            .order_by("id")
        )
        return JsonResponse(
            {
                "supplier": {
                    "id": receipt.supplier_id,
                    "name": receipt.supplier.supplier_name,
                },
                "items": [_supplier_return_receipt_item_payload(item) for item in items],
            }
        )


class SupplierReturnDetailView(LoginRequiredMixin, DetailView):
    model = SupplierReturn
    template_name = "purchase/supplier_return_detail.html"
    context_object_name = "supplier_return"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SupplierReturnListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("supplier", "purchase_receipt", "purchase_receipt__purchase_order", "created_by")
            .prefetch_related("items__material", "items__batch", "items__location", "items__purchase_receipt_item__purchase_receipt")
        )
        return _filter_supplier_return_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_purchase = _can_process_purchase(self.request.user)
        context["page_title"] = f"供应商退货 {self.object.supplier_return_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_edit"] = can_process_purchase and self.object.status in [SupplierReturn.Status.DRAFT, SupplierReturn.Status.REJECTED]
        context["can_void"] = can_process_purchase and self.object.status in [
            SupplierReturn.Status.DRAFT,
            SupplierReturn.Status.PENDING_APPROVAL,
            SupplierReturn.Status.REJECTED,
        ]
        context["can_confirm_out"] = can_process_purchase and self.object.status == SupplierReturn.Status.CONFIRMED
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "supplier_return",
            self.object.id,
            self.object.supplier_return_no,
        )
        return context


class SupplierReturnPrintView(LoginRequiredMixin, DetailView):
    model = SupplierReturn
    template_name = "purchase/supplier_return_print.html"
    context_object_name = "supplier_return"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SupplierReturnListView.view_permission_required, "缺少采购数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("supplier", "purchase_receipt", "purchase_receipt__purchase_order", "created_by")
            .prefetch_related("items__material", "items__batch", "items__location", "items__purchase_receipt_item__purchase_receipt")
        )
        return _filter_supplier_return_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印供应商退货 {self.object.supplier_return_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        record_print_log(
            template_type="supplier_return",
            source_doc_type="supplier_return",
            source_doc_id=self.object.id,
            source_doc_no=self.object.supplier_return_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SupplierReturnUpdateView(LoginRequiredMixin, View):
    editable_statuses = [SupplierReturn.Status.DRAFT, SupplierReturn.Status.REJECTED]

    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        supplier_return = self._get_supplier_return(request, pk)
        if not supplier_return:
            return redirect("purchase:supplier_return_list")
        if supplier_return.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回供应商退货单可以编辑")
            return redirect("purchase:supplier_return_detail", pk=pk)
        form = SupplierReturnForm(
            instance=supplier_return,
            purchase_receipt_queryset=_supplier_return_receipt_queryset(request.user),
            initial={"show_all_receipts": request.GET.get("show_all_receipts") == "1"},
        )
        item_formset = SupplierReturnItemFormSet(
            instance=supplier_return,
            supplier=supplier_return.supplier,
            purchase_receipt=supplier_return.purchase_receipt,
            can_edit_amount=can_view_amount(request.user),
        )
        return self._render(request, supplier_return, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        supplier_return = self._get_supplier_return(request, pk, for_update=True)
        if not supplier_return:
            return redirect("purchase:supplier_return_list")
        if supplier_return.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回供应商退货单可以编辑")
            return redirect("purchase:supplier_return_detail", pk=pk)

        before_snapshot = _supplier_return_snapshot(supplier_return)
        form = SupplierReturnForm(
            request.POST,
            instance=supplier_return,
            purchase_receipt_queryset=_supplier_return_receipt_queryset(request.user),
        )
        submit_for_approval = request.POST.get("action") == "submit"
        if not form.is_valid():
            item_formset = SupplierReturnItemFormSet(
                request.POST,
                instance=supplier_return,
                can_edit_amount=can_view_amount(request.user),
            )
            return self._render(request, supplier_return, form, item_formset)
        item_formset = SupplierReturnItemFormSet(
            request.POST,
            instance=supplier_return,
            supplier=form.cleaned_data.get("supplier"),
            purchase_receipt=form.cleaned_data.get("purchase_receipt"),
            require_ready=submit_for_approval,
            can_edit_amount=can_view_amount(request.user),
        )
        if not item_formset.is_valid():
            return self._render(request, supplier_return, form, item_formset)

        with transaction.atomic():
            supplier_return = form.save(commit=False, user=request.user)
            supplier_return.status = SupplierReturn.Status.PENDING_APPROVAL if submit_for_approval else SupplierReturn.Status.DRAFT
            supplier_return.save()
            item_formset.instance = supplier_return
            item_formset.save()
            recalculate_supplier_return_total(supplier_return)
            after_snapshot = {
                **_supplier_return_snapshot(supplier_return),
                "operation_reason": optional_post_reason(request, default="页面编辑供应商退货单"),
            }

        record_audit_log_from_request(
            request,
            "supplier_return_update",
            "supplier_return",
            supplier_return.id,
            supplier_return.supplier_return_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "供应商退货单已更新")
        return redirect("purchase:supplier_return_detail", pk=supplier_return.pk)

    def _get_supplier_return(self, request, pk, for_update=False):
        select_related = ("supplier",) if for_update else ("supplier", "purchase_receipt", "created_by")
        queryset = (
            SupplierReturn.objects.select_related(*select_related)
            .prefetch_related("items__material", "items__batch", "items__location", "items__purchase_receipt_item")
        )
        if for_update:
            queryset = queryset.select_for_update()
        queryset = _filter_supplier_return_queryset_for_user(queryset, request.user)
        try:
            return queryset.get(pk=pk)
        except SupplierReturn.DoesNotExist:
            messages.error(request, "供应商退货单不存在")
            return None

    def _render(self, request, supplier_return, form, item_formset):
        return render(
            request,
            "purchase/supplier_return_form.html",
            {
                "page_title": f"编辑供应商退货 {supplier_return.supplier_return_no}",
                "form": form,
                "item_formset": item_formset,
                "can_view_amount": can_view_amount(request.user),
                "can_submit_for_approval": _can_process_purchase(request.user),
                "supplier_return": supplier_return,
                "is_edit": True,
            },
        )


class SupplierReturnVoidView(LoginRequiredMixin, View):
    voidable_statuses = [SupplierReturn.Status.DRAFT, SupplierReturn.Status.PENDING_APPROVAL, SupplierReturn.Status.REJECTED]

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        verification_response = require_second_verify(request, "purchase:supplier_return_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "purchase:supplier_return_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写供应商退货单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                supplier_return = (
                    _filter_supplier_return_queryset_for_user(
                        SupplierReturn.objects.select_for_update(),
                        request.user,
                    )
                    .prefetch_related("items__material")
                    .get(pk=pk)
                )
                if supplier_return.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿、待审核或已驳回供应商退货单可以作废")
                    return redirect("purchase:supplier_return_detail", pk=pk)
                before_snapshot = _supplier_return_snapshot(supplier_return)
                supplier_return.status = SupplierReturn.Status.VOIDED
                supplier_return.save(update_fields=["status"])
                after_snapshot = _supplier_return_snapshot(supplier_return)
        except SupplierReturn.DoesNotExist:
            messages.error(request, "供应商退货单不存在")
            return redirect("purchase:supplier_return_list")

        record_audit_log_from_request(
            request,
            "supplier_return_void",
            "supplier_return",
            supplier_return.id,
            supplier_return.supplier_return_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "供应商退货单已作废")
        return redirect("purchase:supplier_return_detail", pk=pk)


class SupplierReturnConfirmOutView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        if not _filter_supplier_return_queryset_for_user(SupplierReturn.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "供应商退货单不存在")
            return redirect("purchase:supplier_return_list")
        verification_response = require_second_verify(request, "purchase:supplier_return_detail", pk)
        if verification_response:
            return verification_response
        result = confirm_supplier_return_shipment(pk, request.user.id, f"supplier-return-out:{pk}")
        if result.success:
            record_audit_log_from_request(request, "supplier_return_confirm_out", "supplier_return", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "供应商退货出库确认失败")
        return redirect("purchase:supplier_return_detail", pk=pk)


def _can_process_purchase(user) -> bool:
    return user_has_permission(user, PermissionCode.PURCHASE_PROCESS)


def _can_view_all_purchase(user) -> bool:
    return can_view_amount(user) or user_has_permission(user, PermissionCode.PURCHASE_VIEW)


def _filter_purchase_order_queryset_for_user(queryset, user):
    if _can_view_all_purchase(user):
        return queryset
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset.filter(Q(purchase_owner=user) | Q(created_by=user)).distinct()
    return queryset.none()


def _filter_purchase_receipt_queryset_for_user(queryset, user):
    if _can_view_all_purchase(user):
        return queryset
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset.filter(Q(purchase_order__purchase_owner=user) | Q(purchase_order__created_by=user) | Q(created_by=user)).distinct()
    return queryset.none()


def _filter_supplier_return_queryset_for_user(queryset, user):
    if _can_view_all_purchase(user):
        return queryset
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset.filter(
            Q(purchase_receipt__purchase_order__purchase_owner=user)
            | Q(purchase_receipt__purchase_order__created_by=user)
            | Q(purchase_receipt__created_by=user)
            | Q(created_by=user)
        ).distinct()
    return queryset.none()


def _purchase_request_snapshot(purchase_request: PurchaseRequest) -> dict:
    purchase_request.refresh_from_db()
    items = purchase_request.items.select_related("material", "suggested_supplier").order_by("line_no")
    return {
        "purchase_request_no": purchase_request.purchase_request_no,
        "source_type": purchase_request.source_type,
        "status": purchase_request.status,
        "needed_date": purchase_request.needed_date.isoformat() if purchase_request.needed_date else None,
        "remark": purchase_request.remark,
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "material_id": item.material_id,
                "material_code": item.material.material_code if item.material_id else "",
                "request_qty": str(item.request_qty),
                "suggested_supplier_id": item.suggested_supplier_id,
                "needed_date": item.needed_date.isoformat() if item.needed_date else None,
                "line_status": item.line_status,
            }
            for item in items
        ],
    }


def _purchase_order_snapshot(order: PurchaseOrder) -> dict:
    order.refresh_from_db()
    items = order.items.select_related("material").order_by("line_no")
    return {
        "purchase_order_no": order.purchase_order_no,
        "supplier_id": order.supplier_id,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "purchase_owner_id": order.purchase_owner_id,
        "status": order.status,
        "total_amount": str(order.total_amount),
        "remark": order.remark,
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "material_id": item.material_id,
                "material_code": item.material.material_code if item.material_id else "",
                "order_qty": str(item.order_qty),
                "received_qty": str(item.received_qty),
                "unit_price": str(item.unit_price),
                "line_amount": str(item.line_amount),
                "needed_date": item.needed_date.isoformat() if item.needed_date else None,
                "line_status": item.line_status,
            }
            for item in items
        ],
    }


def _purchase_receipt_snapshot(receipt: PurchaseReceipt) -> dict:
    receipt.refresh_from_db()
    items = receipt.items.select_related("material", "location", "purchase_order_item", "batch").order_by("id")
    return {
        "purchase_receipt_no": receipt.purchase_receipt_no,
        "purchase_order_id": receipt.purchase_order_id,
        "supplier_id": receipt.supplier_id,
        "receipt_date": receipt.receipt_date.isoformat() if receipt.receipt_date else None,
        "status": receipt.status,
        "remark": receipt.remark,
        "items": [
            {
                "id": item.id,
                "purchase_order_item_id": item.purchase_order_item_id,
                "material_id": item.material_id,
                "material_code": item.material.material_code if item.material_id else "",
                "received_qty": str(item.received_qty),
                "accepted_qty": str(item.accepted_qty),
                "rejected_qty": str(item.rejected_qty),
                "unit_price": str(item.unit_price),
                "location_id": item.location_id,
                "batch_id": item.batch_id,
            }
            for item in items
        ],
    }


def _supplier_return_receipt_queryset(user=None):
    queryset = (
        PurchaseReceipt.objects.select_related("supplier", "purchase_order")
        .filter(
            status__in=[PurchaseReceipt.Status.PARTIAL_RECEIVED, PurchaseReceipt.Status.RECEIVED],
            items__accepted_qty__gt=0,
        )
        .distinct()
    )
    if user is not None:
        queryset = _filter_purchase_receipt_queryset_for_user(queryset, user)
    return queryset


def _supplier_return_receipt_item_payload(item: PurchaseReceiptItem) -> dict:
    material = item.material
    return {
        "id": item.id,
        "material_id": material.id,
        "unit_price": str(item.unit_price),
        "returnable_qty": str(supplier_returnable_qty(item)),
        "batch_id": item.batch_id or "",
        "location_id": item.location_id or "",
        "label": supplier_return_receipt_item_label(item),
    }


def _supplier_return_snapshot(supplier_return: SupplierReturn) -> dict:
    supplier_return.refresh_from_db()
    items = supplier_return.items.select_related("material", "batch", "location", "purchase_receipt_item").order_by("id")
    return {
        "supplier_return_no": supplier_return.supplier_return_no,
        "supplier_id": supplier_return.supplier_id,
        "purchase_receipt_id": supplier_return.purchase_receipt_id,
        "return_date": supplier_return.return_date.isoformat() if supplier_return.return_date else None,
        "status": supplier_return.status,
        "return_amount": str(supplier_return.return_amount),
        "remark": supplier_return.remark,
        "items": [
            {
                "id": item.id,
                "purchase_receipt_item_id": item.purchase_receipt_item_id,
                "material_id": item.material_id,
                "material_code": item.material.material_code if item.material_id else "",
                "return_qty": str(item.return_qty),
                "unit_price": str(item.unit_price),
                "return_amount": str(item.return_amount),
                "batch_id": item.batch_id,
                "location_id": item.location_id,
            }
            for item in items
        ],
    }
