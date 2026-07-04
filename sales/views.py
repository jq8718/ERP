import csv
from io import StringIO

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import CreateView

from accounts.permissions import PermissionCode, can_view_amount, require_any_erp_permission, require_erp_permission, user_has_permission
from files.services import csv_upload_validation_error, export_queryset_to_csv, record_print_log, uploaded_csv_text_file
from files.view_helpers import build_attachment_panel, export_file_response
from inventory.models import InventoryBatch
from system.date_utils import parse_user_date
from system.services import next_document_no, record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason, require_post_reason, require_second_verify

from .forms import (
    CustomerReturnForm,
    CustomerReturnItemFormSet,
    SampleLoanForm,
    SampleLoanItemFormSet,
    SampleLoanReturnForm,
    SampleLoanReturnItemFormSet,
    SalesShipmentForm,
    SalesShipmentItemFormSet,
    SalesOrderForm,
    SalesOrderItemFormSet,
    recalculate_customer_return_total,
    recalculate_sales_order_total,
)
from .import_services import (
    CUSTOMER_RETURN_IMPORT_TEMPLATE_ROWS,
    SALES_ORDER_IMPORT_TEMPLATE_ROWS,
    SALES_SHIPMENT_IMPORT_TEMPLATE_ROWS,
    SAMPLE_LOAN_IMPORT_TEMPLATE_ROWS,
    import_customer_returns_from_csv,
    import_sample_loans_from_csv,
    import_sales_orders_from_csv,
    import_sales_shipments_from_csv,
)
from .models import (
    CustomerReturn,
    CustomerReturnItem,
    SalesOrder,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
    SampleLoan,
    SampleLoanItem,
    SampleLoanReturn,
    SampleLoanReturnItem,
    ShortageAlert,
)
from .services import (
    confirm_customer_return_receipt,
    confirm_sales_order,
    confirm_sales_shipment,
    confirm_sample_loan_out,
    confirm_sample_return,
    convert_sample_loan_item_to_sales_order,
    recheck_sales_order_inventory,
)


class SalesOrderListView(ErpListView):
    model = SalesOrder
    page_title = "销售订单"
    view_permission_required = (PermissionCode.SALES_VIEW, PermissionCode.SALES_PROCESS, PermissionCode.SALES_VIEW_ALL)
    permission_denied_message = "缺少销售数据查看权限"
    create_url_name = "sales:sales_order_create"
    create_permission_required = PermissionCode.SALES_PROCESS
    detail_url_name = "sales:sales_order_detail"
    columns = (
        ("订单号", "sales_order_no"),
        ("客户", "customer.customer_name"),
        ("订单日期", "order_date"),
        ("交期", "delivery_date"),
        ("状态", "get_status_display"),
        ("金额", "total_amount"),
    )
    sensitive_columns = ("total_amount",)
    ordering = ["-created_at"]
    page_actions = (
        ("导出CSV", "sales:sales_order_export", ""),
        ("下载导入模板", "sales:sales_order_import_template", ""),
        ("导入CSV", "sales:sales_order_import", "primary"),
    )
    page_action_permissions = {
        "sales:sales_order_import_template": PermissionCode.SALES_PROCESS,
        "sales:sales_order_import": PermissionCode.SALES_PROCESS,
    }
    search_fields = ("sales_order_no", "customer__customer_name")
    status_filter_field = "status"
    sortable_fields = {
        "sales_order_no": "sales_order_no",
        "customer.customer_name": "customer__customer_name",
        "order_date": "order_date",
        "delivery_date": "delivery_date",
        "get_status_display": "status",
        "total_amount": "total_amount",
    }

    def get_queryset(self):
        return _filter_sales_order_queryset_for_user(super().get_queryset(), self.request.user).select_related("customer")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not can_view_amount(self.request.user)
        return context


class SalesCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()
    view_permission_required = (PermissionCode.SALES_VIEW, PermissionCode.SALES_PROCESS, PermissionCode.SALES_VIEW_ALL)
    permission_denied_message = "缺少销售数据查看权限"

    def dispatch(self, request, *args, **kwargs):
        required_permissions = getattr(self.list_view_class, "view_permission_required", self.view_permission_required)
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, required_permissions, self.permission_denied_message)
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
        queryset = self.apply_scope(queryset)
        queryset = queryset.order_by(*self.get_ordering(list_view))
        return queryset

    def get_ordering(self, list_view):
        return list_view.current_ordering() or self.ordering

    def apply_scope(self, queryset):
        return queryset

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


class SalesOrderExportView(SalesCsvExportView):
    module = "sales_orders"
    list_view_class = SalesOrderListView
    ordering = ("-created_at",)
    select_related = ("customer",)

    def apply_scope(self, queryset):
        return _filter_sales_order_queryset_for_user(queryset, self.request.user)


class SalesOrderImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(SALES_ORDER_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="sales_order_import_template.csv"'
        return response


class SalesOrderImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入销售订单"
        context["list_url_name"] = "sales:sales_order_list"
        context["template_url_name"] = "sales:sales_order_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("sales:sales_order_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_sales_orders_from_csv(text_file, request.user.id, can_import_amount=can_view_amount(request.user))
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("sales:sales_order_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class SalesOrderCreateView(LoginRequiredMixin, CreateView):
    model = SalesOrder
    form_class = SalesOrderForm
    template_name = "sales/sales_order_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建销售订单"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_submit_for_approval"] = _can_process_sales(self.request.user)
        if self.request.POST:
            context["item_formset"] = SalesOrderItemFormSet(
                self.request.POST,
                instance=self.object,
                can_edit_amount=context["can_view_amount"],
            )
        else:
            context["item_formset"] = SalesOrderItemFormSet(instance=self.object, can_edit_amount=context["can_view_amount"])
        return context

    def form_valid(self, form):
        context = self.get_context_data(form=form)
        item_formset = context["item_formset"]
        submit_for_approval = self.request.POST.get("action") == "submit"
        if submit_for_approval:
            require_erp_permission(self.request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        if not item_formset.is_valid():
            return self.form_invalid(form)

        with transaction.atomic():
            self.object = form.save(commit=False, user=self.request.user)
            self.object.status = SalesOrder.Status.PENDING_APPROVAL if submit_for_approval else SalesOrder.Status.DRAFT
            self.object.save()
            item_formset.instance = self.object
            item_formset.save()
            recalculate_sales_order_total(self.object)

        messages.success(self.request, "销售订单已保存")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("sales:sales_order_detail", kwargs={"pk": self.object.pk})


class SalesOrderUpdateView(LoginRequiredMixin, View):
    editable_statuses = [SalesOrder.Status.DRAFT, SalesOrder.Status.REJECTED]

    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        order = self._get_order(request, pk)
        if not order:
            return redirect("sales:sales_order_list")
        if order.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回销售订单可以编辑")
            return redirect("sales:sales_order_detail", pk=pk)
        form = SalesOrderForm(instance=order)
        item_formset = SalesOrderItemFormSet(instance=order, can_edit_amount=can_view_amount(request.user))
        return self._render(request, order, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        order = self._get_order(request, pk, for_update=True)
        if not order:
            return redirect("sales:sales_order_list")
        if order.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回销售订单可以编辑")
            return redirect("sales:sales_order_detail", pk=pk)
        before_snapshot = _sales_order_snapshot(order)
        form = SalesOrderForm(request.POST, instance=order)
        item_formset = SalesOrderItemFormSet(
            request.POST,
            instance=order,
            can_edit_amount=can_view_amount(request.user),
        )
        submit_for_approval = request.POST.get("action") == "submit"
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, order, form, item_formset)

        with transaction.atomic():
            order = form.save(commit=False, user=request.user)
            order.status = SalesOrder.Status.PENDING_APPROVAL if submit_for_approval else SalesOrder.Status.DRAFT
            order.version += 1
            order.save()
            item_formset.instance = order
            item_formset.save()
            recalculate_sales_order_total(order)
            operation_reason = optional_post_reason(request, default="页面编辑销售订单")
            after_snapshot = {**_sales_order_snapshot(order), "operation_reason": operation_reason}
            order.change_logs.create(
                changed_by=request.user,
                change_reason=operation_reason,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
            )

        record_audit_log_from_request(
            request,
            "sales_order_update",
            "sales_order",
            order.id,
            order.sales_order_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "销售订单已更新")
        return redirect("sales:sales_order_detail", pk=order.pk)

    def _get_order(self, request, pk, for_update=False):
        queryset = _filter_sales_order_queryset_for_user(SalesOrder.objects.all(), request.user).prefetch_related("items")
        if for_update:
            queryset = queryset.select_for_update()
        try:
            return queryset.get(pk=pk)
        except SalesOrder.DoesNotExist:
            messages.error(request, "销售订单不存在或无权限操作")
            return None

    def _render(self, request, order, form, item_formset):
        return render(
            request,
            "sales/sales_order_form.html",
            {
                "page_title": f"编辑销售订单 {order.sales_order_no}",
                "form": form,
                "item_formset": item_formset,
                "can_view_amount": can_view_amount(request.user),
                "can_submit_for_approval": _can_process_sales(request.user),
                "order": order,
                "is_edit": True,
            },
        )


class SalesOrderDetailView(LoginRequiredMixin, DetailView):
    model = SalesOrder
    template_name = "sales/sales_order_detail.html"
    context_object_name = "order"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SalesOrderListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sales_order_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("customer", "customer_address", "created_by", "approved_by")
            .prefetch_related("items__customer_product", "items__finished_material", "items__locked_bom", "shortage_alerts__material")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_sales = _can_process_sales(self.request.user)
        context["page_title"] = f"销售订单 {self.object.sales_order_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_submit"] = can_process_sales and self.object.status == SalesOrder.Status.DRAFT
        context["can_edit"] = can_process_sales and self.object.status in [SalesOrder.Status.DRAFT, SalesOrder.Status.REJECTED]
        context["can_void"] = can_process_sales and self.object.status in [
            SalesOrder.Status.DRAFT,
            SalesOrder.Status.PENDING_APPROVAL,
            SalesOrder.Status.REJECTED,
        ]
        context["can_process_sales"] = can_process_sales
        context["can_confirm"] = can_process_sales and self.object.status == SalesOrder.Status.PENDING_APPROVAL
        context["can_recheck_bom"] = can_process_sales and self.object.status == SalesOrder.Status.PENDING_BOM
        context["can_create_shipment"] = can_process_sales and _sales_order_has_shippable_items(self.object)
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "sales_order",
            self.object.id,
            self.object.sales_order_no,
        )
        return context


class SalesOrderPrintView(LoginRequiredMixin, DetailView):
    model = SalesOrder
    template_name = "sales/sales_order_print.html"
    context_object_name = "order"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SalesOrderListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sales_order_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("customer", "customer_address", "created_by", "approved_by")
            .prefetch_related("items__customer_product", "items__finished_material")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印销售订单 {self.object.sales_order_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        record_print_log(
            template_type="sales_order",
            source_doc_type="sales_order",
            source_doc_id=self.object.id,
            source_doc_no=self.object.sales_order_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SalesOrderSubmitView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        try:
            with transaction.atomic():
                order = _filter_sales_order_queryset_for_user(SalesOrder.objects.select_for_update(), request.user).get(pk=pk)
                if order.status != SalesOrder.Status.DRAFT:
                    messages.error(request, "只有草稿销售订单可以提交审核")
                    return redirect("sales:sales_order_detail", pk=pk)
                if not order.items.exists():
                    messages.error(request, "销售订单没有明细，不能提交审核")
                    return redirect("sales:sales_order_detail", pk=pk)
                order.status = SalesOrder.Status.PENDING_APPROVAL
                order.updated_by = request.user
                order.version += 1
                order.save(update_fields=["status", "updated_by", "updated_at", "version"])
                order.items.update(line_status="pending_approval")
        except SalesOrder.DoesNotExist:
            messages.error(request, "销售订单不存在")
            return redirect("sales:sales_order_list")

        messages.success(request, "销售订单已提交审核")
        return redirect("sales:sales_order_detail", pk=pk)


class SalesOrderVoidView(LoginRequiredMixin, View):
    voidable_statuses = [SalesOrder.Status.DRAFT, SalesOrder.Status.PENDING_APPROVAL, SalesOrder.Status.REJECTED]

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sales_order_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "sales:sales_order_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写销售订单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                order = _filter_sales_order_queryset_for_user(SalesOrder.objects.select_for_update(), request.user).get(pk=pk)
                if order.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿、待审核或已驳回销售订单可以作废")
                    return redirect("sales:sales_order_detail", pk=pk)
                before_snapshot = _sales_order_snapshot(order)
                order.status = SalesOrder.Status.VOIDED
                order.updated_by = request.user
                order.version += 1
                order.save(update_fields=["status", "updated_by", "updated_at", "version"])
                after_snapshot = _sales_order_snapshot(order)
                order.change_logs.create(
                    changed_by=request.user,
                    change_reason=reason,
                    before_snapshot=before_snapshot,
                    after_snapshot=after_snapshot,
                )
        except SalesOrder.DoesNotExist:
            messages.error(request, "销售订单不存在或无权限操作")
            return redirect("sales:sales_order_list")

        record_audit_log_from_request(
            request,
            "sales_order_void",
            "sales_order",
            order.id,
            order.sales_order_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "销售订单已作废")
        return redirect("sales:sales_order_detail", pk=pk)


class SalesOrderConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sales_order_detail", pk)
        if verification_response:
            return verification_response
        if not _filter_sales_order_queryset_for_user(SalesOrder.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "销售订单不存在或无权限操作")
            return redirect("sales:sales_order_list")
        result = confirm_sales_order(pk, request.user.id)
        if result.success:
            record_audit_log_from_request(
                request,
                "sales_order_confirm",
                "sales_order",
                pk,
                after_snapshot=result.data,
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "销售订单审核失败")
        return redirect("sales:sales_order_detail", pk=pk)


class SalesOrderRecheckShortageView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        try:
            order = (
                _filter_sales_order_queryset_for_user(SalesOrder.objects.prefetch_related("items"), request.user)
                .get(pk=pk)
            )
        except SalesOrder.DoesNotExist:
            messages.error(request, "销售订单不存在或无权限操作")
            return redirect("sales:sales_order_list")

        if order.status != SalesOrder.Status.PENDING_BOM:
            messages.error(request, "只有待 BOM 处理的销售订单需要重新检查欠料")
            return redirect("sales:sales_order_detail", pk=pk)

        item_ids = list(order.items.values_list("id", flat=True))
        result = recheck_sales_order_inventory(
            item_ids,
            trigger=f"manual-bom-recheck:{order.id}",
            operator_id=request.user.id,
        )
        if result.success:
            record_audit_log_from_request(
                request,
                "sales_order_recheck_shortage",
                "sales_order",
                order.id,
                order.sales_order_no,
                after_snapshot=result.data,
            )
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "欠料重新检查失败")
        return redirect("sales:sales_order_detail", pk=pk)


class SalesOrderCreateShipmentView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        if not _filter_sales_order_queryset_for_user(SalesOrder.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "销售订单不存在或无权限操作")
            return redirect("sales:sales_order_list")

        try:
            with transaction.atomic():
                order = (
                    SalesOrder.objects.select_for_update()
                    .select_related("customer")
                    .prefetch_related("items__finished_material")
                    .get(pk=pk)
                )
                shippable_items = [
                    item
                    for item in order.items.all()
                    if item.line_status == SalesOrderItem.LineStatus.CONFIRMED
                    and item.inventory_check_status == SalesOrderItem.InventoryCheckStatus.SUFFICIENT
                    and item.order_qty > item.shipped_qty
                ]
                if not shippable_items:
                    messages.error(request, "销售订单没有可生成出库单的明细")
                    return redirect("sales:sales_order_detail", pk=pk)

                shipment = SalesShipment.objects.create(
                    shipment_no=next_document_no("SS"),
                    sales_order=order,
                    customer=order.customer,
                    shipment_date=timezone.localdate(),
                    customer_contract_no=order.customer_contract_no,
                    customer_address_text=order.customer_address.address_encrypted if order.customer_address_id else "",
                    customer_contact_name=order.customer_address.receiver_name if order.customer_address_id else "",
                    customer_contact_phone=order.customer_address.receiver_phone_encrypted if order.customer_address_id else "",
                    settlement_method=order.settlement_method or order.customer.settlement_method,
                    status=SalesShipment.Status.PENDING_CONFIRM,
                    created_by=request.user,
                    remark=request.POST.get("remark", "").strip(),
                )
                for item in shippable_items:
                    remaining_qty = item.order_qty - item.shipped_qty
                    allocations = _allocate_fifo_batches(item.finished_material_id, remaining_qty)
                    if not allocations:
                        raise ValueError(f"{item.finished_material.material_code} 可用库存不足")
                    for batch, qty in allocations:
                        SalesShipmentItem.objects.create(
                            shipment=shipment,
                            sales_order_item=item,
                            material=item.finished_material,
                            shipment_qty=qty,
                            batch=batch,
                            location=batch.location,
                            cost_price=batch.cost_price,
                        )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("sales:sales_order_detail", pk=pk)

        messages.success(request, "销售出库单已生成")
        return redirect("sales:sales_shipment_detail", pk=shipment.pk)


class ShortageCreatePurchaseRequestView(LoginRequiredMixin, TemplateView):
    template_name = "sales/shortage_create_purchase_request.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.PURCHASE_PROCESS, "缺少采购单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "欠料生成采购需求"
        context["shortages"] = self._queryset()
        return context

    def post(self, request):
        from purchase.services import create_purchase_request_from_shortages

        shortage_ids = [int(value) for value in request.POST.getlist("shortage_ids") if value.isdigit()]
        merge_mode = request.POST.get("merge_mode", "by_material")
        result = create_purchase_request_from_shortages(
            shortage_ids,
            operator_id=request.user.id,
            merge_mode=merge_mode,
            idempotency_key=f"shortage-pr:{request.user.id}:{','.join(map(str, sorted(shortage_ids)))}:{merge_mode}",
        )
        if not result.success:
            messages.error(request, result.message or result.error_code or "采购需求生成失败")
            return redirect("sales:shortage_create_purchase_request")

        messages.success(request, result.message)
        return redirect("purchase:purchase_request_detail", pk=result.data["purchase_request_id"])

    def _queryset(self):
        return (
            _filter_shortage_queryset_for_user(ShortageAlert.objects, self.request.user)
            .select_related("sales_order", "sales_order_item", "material")
            .filter(status=ShortageAlert.Status.UNPROCESSED, shortage_qty__gt=0)
            .order_by("material__material_code", "sales_order__sales_order_no", "id")
        )


class ShortageAlertListView(ErpListView):
    model = ShortageAlert
    page_title = "欠料提醒"
    view_permission_required = (
        PermissionCode.SALES_VIEW,
        PermissionCode.SALES_PROCESS,
        PermissionCode.SALES_VIEW_ALL,
        PermissionCode.PURCHASE_VIEW,
        PermissionCode.PURCHASE_PROCESS,
    )
    permission_denied_message = "缺少欠料提醒查看权限"
    page_actions = (
        ("导出CSV", "sales:shortage_alert_export", ""),
        ("生成采购需求", "sales:shortage_create_purchase_request", "primary"),
    )
    page_action_permissions = {
        "sales:shortage_create_purchase_request": PermissionCode.PURCHASE_PROCESS,
    }
    columns = (
        ("欠料号", "shortage_no"),
        ("销售订单", "sales_order.sales_order_no"),
        ("物料", "material.material_code"),
        ("需求数量", "required_qty"),
        ("可用数量", "available_qty"),
        ("欠料数量", "shortage_qty"),
        ("状态", "get_status_display"),
    )
    ordering = ["-created_at"]
    search_fields = ("shortage_no", "sales_order__sales_order_no", "material__material_code", "material__material_name")
    status_filter_field = "status"
    sortable_fields = {
        "shortage_no": "shortage_no",
        "sales_order.sales_order_no": "sales_order__sales_order_no",
        "material.material_code": "material__material_code",
        "required_qty": "required_qty",
        "available_qty": "available_qty",
        "shortage_qty": "shortage_qty",
        "get_status_display": "status",
    }

    def get_queryset(self):
        return _filter_shortage_queryset_for_user(super().get_queryset(), self.request.user).select_related("sales_order", "material")


class ShortageAlertExportView(SalesCsvExportView):
    module = "shortage_alerts"
    list_view_class = ShortageAlertListView
    ordering = ("-created_at",)
    select_related = ("sales_order", "material")

    def apply_scope(self, queryset):
        return _filter_shortage_queryset_for_user(queryset, self.request.user)


class SalesShipmentListView(ErpListView):
    model = SalesShipment
    page_title = "销售出库"
    view_permission_required = (PermissionCode.SALES_VIEW, PermissionCode.SALES_PROCESS, PermissionCode.SALES_VIEW_ALL)
    permission_denied_message = "缺少销售数据查看权限"
    detail_url_name = "sales:sales_shipment_detail"
    columns = (
        ("出库单号", "shipment_no"),
        ("销售订单", "sales_order.sales_order_no"),
        ("客户", "customer.customer_name"),
        ("出库日期", "shipment_date"),
        ("状态", "get_status_display"),
    )
    ordering = ["-created_at"]
    page_actions = (
        ("导出CSV", "sales:sales_shipment_export", ""),
        ("下载导入模板", "sales:sales_shipment_import_template", ""),
        ("导入CSV", "sales:sales_shipment_import", "primary"),
    )
    page_action_permissions = {
        "sales:sales_shipment_import_template": PermissionCode.SALES_PROCESS,
        "sales:sales_shipment_import": PermissionCode.SALES_PROCESS,
    }
    search_fields = ("shipment_no", "sales_order__sales_order_no", "customer__customer_name")
    status_filter_field = "status"
    sortable_fields = {
        "shipment_no": "shipment_no",
        "sales_order.sales_order_no": "sales_order__sales_order_no",
        "customer.customer_name": "customer__customer_name",
        "shipment_date": "shipment_date",
        "get_status_display": "status",
    }

    def get_queryset(self):
        return _filter_sales_shipment_queryset_for_user(super().get_queryset(), self.request.user).select_related("sales_order", "customer")


class SalesShipmentExportView(SalesCsvExportView):
    module = "sales_shipments"
    list_view_class = SalesShipmentListView
    ordering = ("-created_at",)
    select_related = ("sales_order", "customer")

    def apply_scope(self, queryset):
        return _filter_sales_shipment_queryset_for_user(queryset, self.request.user)


class SalesShipmentImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(SALES_SHIPMENT_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="sales_shipment_import_template.csv"'
        return response


class SalesShipmentImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入销售出库"
        context["list_url_name"] = "sales:sales_shipment_list"
        context["template_url_name"] = "sales:sales_shipment_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("sales:sales_shipment_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_sales_shipments_from_csv(
            text_file,
            request.user.id,
            can_view_all=user_has_permission(request.user, PermissionCode.SALES_VIEW_ALL),
        )
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("sales:sales_shipment_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class SalesShipmentDetailView(LoginRequiredMixin, DetailView):
    model = SalesShipment
    template_name = "sales/sales_shipment_detail.html"
    context_object_name = "shipment"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SalesShipmentListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sales_shipment_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("sales_order", "customer", "created_by")
            .prefetch_related("items__sales_order_item", "items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"销售出库 {self.object.shipment_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_process_sales"] = _can_process_sales(self.request.user)
        context["can_confirm"] = context["can_process_sales"] and self.object.status == SalesShipment.Status.PENDING_CONFIRM
        context["can_edit"] = context["can_process_sales"] and self.object.status == SalesShipment.Status.PENDING_CONFIRM
        context["can_void"] = context["can_process_sales"] and self.object.status == SalesShipment.Status.PENDING_CONFIRM
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "sales_shipment",
            self.object.id,
            self.object.shipment_no,
        )
        return context


class SalesShipmentUpdateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        shipment = self._get_shipment(request, pk)
        if not shipment:
            return redirect("sales:sales_shipment_list")
        if shipment.status != SalesShipment.Status.PENDING_CONFIRM:
            messages.error(request, "只有待确认销售出库单可以编辑")
            return redirect("sales:sales_shipment_detail", pk=pk)
        form = SalesShipmentForm(instance=shipment)
        item_formset = SalesShipmentItemFormSet(instance=shipment)
        return self._render(request, shipment, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        shipment = self._get_shipment(request, pk, for_update=True)
        if not shipment:
            return redirect("sales:sales_shipment_list")
        if shipment.status != SalesShipment.Status.PENDING_CONFIRM:
            messages.error(request, "只有待确认销售出库单可以编辑")
            return redirect("sales:sales_shipment_detail", pk=pk)
        before_snapshot = _sales_shipment_snapshot(shipment)
        form = SalesShipmentForm(request.POST, instance=shipment)
        item_formset = SalesShipmentItemFormSet(request.POST, instance=shipment)
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, shipment, form, item_formset)

        with transaction.atomic():
            shipment = form.save()
            item_formset.instance = shipment
            item_formset.save()
            after_snapshot = {
                **_sales_shipment_snapshot(shipment),
                "operation_reason": optional_post_reason(request, default="页面编辑销售出库单"),
            }

        record_audit_log_from_request(
            request,
            "sales_shipment_update",
            "sales_shipment",
            shipment.id,
            shipment.shipment_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "销售出库单已更新")
        return redirect("sales:sales_shipment_detail", pk=shipment.pk)

    def _get_shipment(self, request, pk, for_update=False):
        select_related = ("sales_order", "customer") if for_update else ("sales_order", "customer", "created_by")
        queryset = (
            _filter_sales_shipment_queryset_for_user(SalesShipment.objects.all(), request.user)
            .select_related(*select_related)
            .prefetch_related("items__sales_order_item", "items__material", "items__batch", "items__location")
        )
        if for_update:
            queryset = queryset.select_for_update()
        try:
            return queryset.get(pk=pk)
        except SalesShipment.DoesNotExist:
            messages.error(request, "销售出库单不存在或无权限操作")
            return None

    def _render(self, request, shipment, form, item_formset):
        return render(
            request,
            "sales/sales_shipment_form.html",
            {
                "page_title": f"编辑销售出库 {shipment.shipment_no}",
                "shipment": shipment,
                "form": form,
                "item_formset": item_formset,
            },
        )


class SalesShipmentVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sales_shipment_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "sales:sales_shipment_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写销售出库单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                shipment = _filter_sales_shipment_queryset_for_user(
                    SalesShipment.objects.select_for_update().prefetch_related("items__sales_order_item", "items__material"),
                    request.user,
                ).get(pk=pk)
                if shipment.status != SalesShipment.Status.PENDING_CONFIRM:
                    messages.error(request, "只有待确认销售出库单可以作废")
                    return redirect("sales:sales_shipment_detail", pk=pk)
                before_snapshot = _sales_shipment_snapshot(shipment)
                shipment.status = SalesShipment.Status.VOIDED
                shipment.save(update_fields=["status"])
                after_snapshot = _sales_shipment_snapshot(shipment)
        except SalesShipment.DoesNotExist:
            messages.error(request, "销售出库单不存在或无权限操作")
            return redirect("sales:sales_shipment_list")

        record_audit_log_from_request(
            request,
            "sales_shipment_void",
            "sales_shipment",
            shipment.id,
            shipment.shipment_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "销售出库单已作废")
        return redirect("sales:sales_shipment_detail", pk=pk)


class SalesShipmentPrintView(LoginRequiredMixin, DetailView):
    model = SalesShipment
    template_name = "sales/sales_shipment_print.html"
    context_object_name = "shipment"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SalesShipmentListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sales_shipment_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("sales_order", "customer", "created_by")
            .prefetch_related("items__sales_order_item", "items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印销售出库 {self.object.shipment_no}"
        context["company_info"] = {
            "name": settings.ERP_COMPANY_NAME,
            "address": settings.ERP_COMPANY_ADDRESS,
            "phone": settings.ERP_COMPANY_PHONE,
            "contact": settings.ERP_COMPANY_CONTACT,
        }
        record_print_log(
            template_type="sales_shipment",
            source_doc_type="sales_shipment",
            source_doc_id=self.object.id,
            source_doc_no=self.object.shipment_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SalesShipmentConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sales_shipment_detail", pk)
        if verification_response:
            return verification_response
        if not _filter_sales_shipment_queryset_for_user(SalesShipment.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "销售出库单不存在或无权限操作")
            return redirect("sales:sales_shipment_list")
        result = confirm_sales_shipment(pk, request.user.id, f"sales-shipment:{pk}")
        if result.success:
            record_audit_log_from_request(request, "sales_shipment_confirm", "sales_shipment", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "销售出库确认失败")
        return redirect("sales:sales_shipment_detail", pk=pk)


class CustomerReturnListView(ErpListView):
    model = CustomerReturn
    page_title = "客户退货"
    view_permission_required = (PermissionCode.SALES_VIEW, PermissionCode.SALES_PROCESS, PermissionCode.SALES_VIEW_ALL)
    permission_denied_message = "缺少销售数据查看权限"
    create_url_name = "sales:customer_return_create"
    detail_url_name = "sales:customer_return_detail"
    columns = (
        ("退货单号", "return_no"),
        ("客户", "customer.customer_name"),
        ("退货日期", "return_date"),
        ("状态", "get_status_display"),
        ("金额", "return_amount"),
    )
    sensitive_columns = ("return_amount",)
    ordering = ["-return_date", "-id"]
    page_actions = (
        ("导出CSV", "sales:customer_return_export", ""),
        ("下载导入模板", "sales:customer_return_import_template", ""),
        ("导入CSV", "sales:customer_return_import", "primary"),
    )
    page_action_permissions = {
        "sales:customer_return_import_template": PermissionCode.SALES_PROCESS,
        "sales:customer_return_import": PermissionCode.SALES_PROCESS,
    }
    search_fields = ("return_no", "customer__customer_name", "sales_order__sales_order_no")
    status_filter_field = "status"

    def get_queryset(self):
        return _filter_customer_return_queryset_for_user(super().get_queryset(), self.request.user).select_related("customer", "sales_order")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not can_view_amount(self.request.user)
        return context


class CustomerReturnExportView(SalesCsvExportView):
    module = "customer_returns"
    list_view_class = CustomerReturnListView
    ordering = ("-return_date", "-id")
    select_related = ("customer", "sales_order")

    def apply_scope(self, queryset):
        return _filter_customer_return_queryset_for_user(queryset, self.request.user)


class CustomerReturnImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(CUSTOMER_RETURN_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="customer_return_import_template.csv"'
        return response


class CustomerReturnImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入客户退货"
        context["list_url_name"] = "sales:customer_return_list"
        context["template_url_name"] = "sales:customer_return_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("sales:customer_return_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_customer_returns_from_csv(
            text_file,
            request.user.id,
            can_import_amount=can_view_amount(request.user),
        )
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("sales:customer_return_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class CustomerReturnCreateView(LoginRequiredMixin, CreateView):
    model = CustomerReturn
    form_class = CustomerReturnForm
    template_name = "sales/customer_return_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建客户退货"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_submit_for_approval"] = _can_process_sales(self.request.user)
        if "item_formset" in context:
            return context
        if self.request.POST:
            form = context.get("form")
            form_valid = form and form.is_valid()
            customer = form.cleaned_data.get("customer") if form_valid else None
            sales_order = form.cleaned_data.get("sales_order") if form_valid else None
            context["item_formset"] = CustomerReturnItemFormSet(
                self.request.POST,
                instance=self.object,
                customer=customer,
                sales_order=sales_order,
                require_ready=self.request.POST.get("action") == "submit",
                can_edit_amount=context["can_view_amount"],
            )
        else:
            context["item_formset"] = CustomerReturnItemFormSet(
                instance=self.object,
                can_edit_amount=context["can_view_amount"],
            )
        return context

    def form_valid(self, form):
        submit_for_approval = self.request.POST.get("action") == "submit"
        if submit_for_approval:
            require_erp_permission(self.request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        item_formset = CustomerReturnItemFormSet(
            self.request.POST,
            instance=self.object,
            customer=form.cleaned_data.get("customer"),
            sales_order=form.cleaned_data.get("sales_order"),
            require_ready=submit_for_approval,
            can_edit_amount=can_view_amount(self.request.user),
        )
        if not item_formset.is_valid():
            return self.render_to_response(
                self.get_context_data(form=form, item_formset=item_formset)
            )

        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.status = CustomerReturn.Status.PENDING_APPROVAL if submit_for_approval else CustomerReturn.Status.DRAFT
            self.object.save()
            item_formset.instance = self.object
            item_formset.save()
            recalculate_customer_return_total(self.object)

        messages.success(self.request, "客户退货单已保存")
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        item_formset = CustomerReturnItemFormSet(
            self.request.POST,
            instance=self.object,
            can_edit_amount=can_view_amount(self.request.user),
        )
        return self.render_to_response(self.get_context_data(form=form, item_formset=item_formset))

    def get_success_url(self):
        return reverse("sales:customer_return_detail", kwargs={"pk": self.object.pk})


class CustomerReturnDetailView(LoginRequiredMixin, DetailView):
    model = CustomerReturn
    template_name = "sales/customer_return_detail.html"
    context_object_name = "customer_return"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, CustomerReturnListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_customer_return_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("customer", "sales_order")
            .prefetch_related("items__material", "items__location", "items__sales_order_item")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_sales = _can_process_sales(self.request.user)
        context["page_title"] = f"客户退货 {self.object.return_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_edit"] = can_process_sales and self.object.status in [CustomerReturn.Status.DRAFT, CustomerReturn.Status.REJECTED]
        context["can_void"] = can_process_sales and self.object.status in [
            CustomerReturn.Status.DRAFT,
            CustomerReturn.Status.PENDING_APPROVAL,
            CustomerReturn.Status.REJECTED,
        ]
        context["can_confirm_receipt"] = can_process_sales and self.object.status == CustomerReturn.Status.CONFIRMED
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "customer_return",
            self.object.id,
            self.object.return_no,
        )
        return context


class CustomerReturnPrintView(LoginRequiredMixin, DetailView):
    model = CustomerReturn
    template_name = "sales/customer_return_print.html"
    context_object_name = "customer_return"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, CustomerReturnListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_customer_return_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("customer", "sales_order")
            .prefetch_related("items__material", "items__location", "items__sales_order_item__sales_order")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印客户退货 {self.object.return_no}"
        context["can_view_amount"] = can_view_amount(self.request.user)
        record_print_log(
            template_type="customer_return",
            source_doc_type="customer_return",
            source_doc_id=self.object.id,
            source_doc_no=self.object.return_no,
            printed_by_id=self.request.user.id,
        )
        return context


class CustomerReturnUpdateView(LoginRequiredMixin, View):
    editable_statuses = [CustomerReturn.Status.DRAFT, CustomerReturn.Status.REJECTED]

    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        customer_return = self._get_customer_return(request, pk)
        if not customer_return:
            return redirect("sales:customer_return_list")
        if customer_return.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回客户退货单可以编辑")
            return redirect("sales:customer_return_detail", pk=pk)
        form = CustomerReturnForm(instance=customer_return)
        item_formset = CustomerReturnItemFormSet(
            instance=customer_return,
            customer=customer_return.customer,
            sales_order=customer_return.sales_order,
            can_edit_amount=can_view_amount(request.user),
        )
        return self._render(request, customer_return, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        customer_return = self._get_customer_return(request, pk, for_update=True)
        if not customer_return:
            return redirect("sales:customer_return_list")
        if customer_return.status not in self.editable_statuses:
            messages.error(request, "只有草稿或已驳回客户退货单可以编辑")
            return redirect("sales:customer_return_detail", pk=pk)

        before_snapshot = _customer_return_snapshot(customer_return)
        form = CustomerReturnForm(request.POST, instance=customer_return)
        submit_for_approval = request.POST.get("action") == "submit"
        if not form.is_valid():
            item_formset = CustomerReturnItemFormSet(
                request.POST,
                instance=customer_return,
                can_edit_amount=can_view_amount(request.user),
            )
            return self._render(request, customer_return, form, item_formset)
        item_formset = CustomerReturnItemFormSet(
            request.POST,
            instance=customer_return,
            customer=form.cleaned_data.get("customer"),
            sales_order=form.cleaned_data.get("sales_order"),
            require_ready=submit_for_approval,
            can_edit_amount=can_view_amount(request.user),
        )
        if not item_formset.is_valid():
            return self._render(request, customer_return, form, item_formset)

        with transaction.atomic():
            customer_return = form.save(commit=False)
            customer_return.status = CustomerReturn.Status.PENDING_APPROVAL if submit_for_approval else CustomerReturn.Status.DRAFT
            customer_return.save()
            item_formset.instance = customer_return
            item_formset.save()
            recalculate_customer_return_total(customer_return)
            after_snapshot = {
                **_customer_return_snapshot(customer_return),
                "operation_reason": optional_post_reason(request, default="页面编辑客户退货单"),
            }

        record_audit_log_from_request(
            request,
            "customer_return_update",
            "customer_return",
            customer_return.id,
            customer_return.return_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "客户退货单已更新")
        return redirect("sales:customer_return_detail", pk=customer_return.pk)

    def _get_customer_return(self, request, pk, for_update=False):
        select_related = ("customer",) if for_update else ("customer", "sales_order")
        queryset = (
            _filter_customer_return_queryset_for_user(CustomerReturn.objects.all(), request.user)
            .select_related(*select_related)
            .prefetch_related("items__sales_order_item", "items__material", "items__location")
        )
        if for_update:
            queryset = queryset.select_for_update()
        try:
            return queryset.get(pk=pk)
        except CustomerReturn.DoesNotExist:
            messages.error(request, "客户退货单不存在或无权限操作")
            return None

    def _render(self, request, customer_return, form, item_formset):
        return render(
            request,
            "sales/customer_return_form.html",
            {
                "page_title": f"编辑客户退货 {customer_return.return_no}",
                "form": form,
                "item_formset": item_formset,
                "can_view_amount": can_view_amount(request.user),
                "can_submit_for_approval": _can_process_sales(request.user),
                "customer_return": customer_return,
                "is_edit": True,
            },
        )


class CustomerReturnVoidView(LoginRequiredMixin, View):
    voidable_statuses = [CustomerReturn.Status.DRAFT, CustomerReturn.Status.PENDING_APPROVAL, CustomerReturn.Status.REJECTED]

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:customer_return_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "sales:customer_return_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写客户退货单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                customer_return = (
                    _filter_customer_return_queryset_for_user(CustomerReturn.objects.select_for_update(), request.user)
                    .prefetch_related("items__material", "items__sales_order_item")
                    .get(pk=pk)
                )
                if customer_return.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿、待审核或已驳回客户退货单可以作废")
                    return redirect("sales:customer_return_detail", pk=pk)
                before_snapshot = _customer_return_snapshot(customer_return)
                customer_return.status = CustomerReturn.Status.VOIDED
                customer_return.save(update_fields=["status"])
                after_snapshot = _customer_return_snapshot(customer_return)
        except CustomerReturn.DoesNotExist:
            messages.error(request, "客户退货单不存在或无权限操作")
            return redirect("sales:customer_return_list")

        record_audit_log_from_request(
            request,
            "customer_return_void",
            "customer_return",
            customer_return.id,
            customer_return.return_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "客户退货单已作废")
        return redirect("sales:customer_return_detail", pk=pk)


class CustomerReturnConfirmReceiptView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:customer_return_detail", pk)
        if verification_response:
            return verification_response
        if not _filter_customer_return_queryset_for_user(CustomerReturn.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "客户退货单不存在或无权限操作")
            return redirect("sales:customer_return_list")
        result = confirm_customer_return_receipt(pk, request.user.id, f"customer-return-in:{pk}")
        if result.success:
            record_audit_log_from_request(request, "customer_return_confirm_receipt", "customer_return", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "客户退货入库确认失败")
        return redirect("sales:customer_return_detail", pk=pk)


class SampleLoanListView(ErpListView):
    model = SampleLoan
    page_title = "借样"
    view_permission_required = (PermissionCode.SALES_VIEW, PermissionCode.SALES_PROCESS, PermissionCode.SALES_VIEW_ALL)
    permission_denied_message = "缺少销售数据查看权限"
    create_url_name = "sales:sample_loan_create"
    create_permission_required = PermissionCode.SALES_PROCESS
    detail_url_name = "sales:sample_loan_detail"
    columns = (
        ("借样单号", "sample_loan_no"),
        ("客户", "customer.customer_name"),
        ("借出日期", "loan_date"),
        ("预计归还", "expected_return_date"),
        ("状态", "get_status_display"),
        ("逾期状态", "get_overdue_status_display"),
    )
    ordering = ["-loan_date", "-id"]
    page_actions = (
        ("导出CSV", "sales:sample_loan_export", ""),
        ("下载导入模板", "sales:sample_loan_import_template", ""),
        ("导入CSV", "sales:sample_loan_import", "primary"),
    )
    page_action_permissions = {
        "sales:sample_loan_import_template": PermissionCode.SALES_PROCESS,
        "sales:sample_loan_import": PermissionCode.SALES_PROCESS,
    }
    search_fields = ("sample_loan_no", "customer__customer_name")
    status_filter_field = "status"

    def get_queryset(self):
        return _filter_sample_loan_queryset_for_user(super().get_queryset(), self.request.user).select_related("customer")


class SampleLoanExportView(SalesCsvExportView):
    module = "sample_loans"
    list_view_class = SampleLoanListView
    ordering = ("-loan_date", "-id")
    select_related = ("customer",)

    def apply_scope(self, queryset):
        return _filter_sample_loan_queryset_for_user(queryset, self.request.user)


class SampleLoanImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(SAMPLE_LOAN_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="sample_loan_import_template.csv"'
        return response


class SampleLoanImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入借样单"
        context["list_url_name"] = "sales:sample_loan_list"
        context["template_url_name"] = "sales:sample_loan_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("sales:sample_loan_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_sample_loans_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("sales:sample_loan_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class SampleLoanReturnListView(ErpListView):
    model = SampleLoanReturn
    page_title = "借样归还"
    view_permission_required = (PermissionCode.SALES_VIEW, PermissionCode.SALES_PROCESS, PermissionCode.SALES_VIEW_ALL)
    permission_denied_message = "缺少销售数据查看权限"
    detail_url_name = "sales:sample_loan_return_detail"
    columns = (
        ("归还单号", "sample_return_no"),
        ("借样单号", "sample_loan.sample_loan_no"),
        ("客户", "customer.customer_name"),
        ("归还日期", "return_date"),
        ("状态", "get_status_display"),
    )
    ordering = ["-return_date", "-id"]
    page_actions = (("导出CSV", "sales:sample_loan_return_export", ""),)
    search_fields = ("sample_return_no", "sample_loan__sample_loan_no", "customer__customer_name")
    status_filter_field = "status"

    def get_queryset(self):
        return _filter_sample_return_queryset_for_user(super().get_queryset(), self.request.user).select_related("sample_loan", "customer")


class SampleLoanReturnExportView(SalesCsvExportView):
    module = "sample_loan_returns"
    list_view_class = SampleLoanReturnListView
    ordering = ("-return_date", "-id")
    select_related = ("sample_loan", "customer")

    def apply_scope(self, queryset):
        return _filter_sample_return_queryset_for_user(queryset, self.request.user)


class SampleLoanCreateView(LoginRequiredMixin, CreateView):
    model = SampleLoan
    form_class = SampleLoanForm
    template_name = "sales/sample_loan_form.html"

    def dispatch(self, request, *args, **kwargs):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建借样单"
        if self.request.POST:
            context["item_formset"] = SampleLoanItemFormSet(self.request.POST, instance=self.object)
        else:
            context["item_formset"] = SampleLoanItemFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data(form=form)
        item_formset = context["item_formset"]
        if not item_formset.is_valid():
            return self.render_to_response(context)

        with transaction.atomic():
            self.object = form.save(commit=False, user=self.request.user)
            self.object.status = SampleLoan.Status.PENDING_APPROVAL
            self.object.save()
            item_formset.instance = self.object
            item_formset.save()

        messages.success(self.request, "借样单已保存")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("sales:sample_loan_detail", kwargs={"pk": self.object.pk})


class SampleLoanDetailView(LoginRequiredMixin, DetailView):
    model = SampleLoan
    template_name = "sales/sample_loan_detail.html"
    context_object_name = "loan"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SampleLoanListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sample_loan_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("customer", "created_by")
            .prefetch_related(
                "items__material",
                "items__batch",
                "items__location",
                "returns__items__material",
                "returns__items__location",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_sales = _can_process_sales(self.request.user)
        context["page_title"] = f"借样 {self.object.sample_loan_no}"
        context["can_add_item"] = can_process_sales and self.object.status == SampleLoan.Status.PENDING_APPROVAL
        context["can_process_sales"] = can_process_sales
        context["can_confirm_out"] = can_process_sales and self.object.status == SampleLoan.Status.PENDING_APPROVAL
        context["can_view_amount"] = can_view_amount(self.request.user)
        context["can_convert_to_sales"] = context["can_view_amount"] and can_process_sales and self.object.status in [
            SampleLoan.Status.OUT,
            SampleLoan.Status.PART_RETURNED,
            SampleLoan.Status.PART_SOLD,
        ]
        context["can_create_return"] = can_process_sales and self.object.status in [
            SampleLoan.Status.OUT,
            SampleLoan.Status.PART_RETURNED,
            SampleLoan.Status.PART_SOLD,
        ]
        context["convertible_items"] = [
            {"item": item, "available_qty": item.loan_qty - item.returned_qty - item.sold_qty}
            for item in self.object.items.all()
            if item.loan_qty - item.returned_qty - item.sold_qty > 0
        ]
        context["item_formset"] = SampleLoanItemFormSet(instance=self.object)
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "sample_loan",
            self.object.id,
            self.object.sample_loan_no,
        )
        return context


class SampleLoanPrintView(LoginRequiredMixin, DetailView):
    model = SampleLoan
    template_name = "sales/sample_loan_print.html"
    context_object_name = "loan"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SampleLoanListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sample_loan_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("customer", "created_by")
            .prefetch_related("items__material", "items__batch", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印借样单 {self.object.sample_loan_no}"
        record_print_log(
            template_type="sample_loan",
            source_doc_type="sample_loan",
            source_doc_id=self.object.id,
            source_doc_no=self.object.sample_loan_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SampleLoanItemCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        from decimal import Decimal, InvalidOperation

        try:
            loan = _filter_sample_loan_queryset_for_user(SampleLoan.objects.all(), request.user).get(pk=pk)
        except SampleLoan.DoesNotExist:
            messages.error(request, "借样单不存在")
            return redirect("sales:sample_loan_list")

        if loan.status != SampleLoan.Status.PENDING_APPROVAL:
            messages.error(request, "只有待审核借样单可以新增明细")
            return redirect("sales:sample_loan_detail", pk=pk)

        material_id = request.POST.get("material") or request.POST.get("items-0-material")
        batch_id = request.POST.get("batch") or request.POST.get("items-0-batch") or None
        location_id = request.POST.get("location") or request.POST.get("items-0-location") or None
        expected_return_date = parse_user_date(
            request.POST.get("expected_return_date") or request.POST.get("items-0-expected_return_date"),
            default=loan.expected_return_date,
        )
        try:
            loan_qty = Decimal(request.POST.get("loan_qty") or request.POST.get("items-0-loan_qty") or "")
        except (InvalidOperation, TypeError):
            messages.error(request, "借出数量必须正确填写")
            return redirect("sales:sample_loan_detail", pk=pk)

        if not material_id or loan_qty <= 0:
            messages.error(request, "样品物料和借出数量必须正确填写")
            return redirect("sales:sample_loan_detail", pk=pk)
        if batch_id:
            from inventory.models import InventoryBatch

            batch = InventoryBatch.objects.filter(id=batch_id).first()
            if not batch or batch.material_id != int(material_id):
                messages.error(request, "批次物料必须与借样物料一致")
                return redirect("sales:sample_loan_detail", pk=pk)
            if location_id and batch.location_id != int(location_id):
                messages.error(request, "库位必须与批次库位一致")
                return redirect("sales:sample_loan_detail", pk=pk)
            if batch.remaining_qty < loan_qty:
                messages.error(request, "借样数量不能超过批次剩余数量")
                return redirect("sales:sample_loan_detail", pk=pk)
            location_id = batch.location_id

        existing_line_no = loan.items.order_by("-line_no").values_list("line_no", flat=True).first() or 0
        SampleLoanItem.objects.create(
            sample_loan=loan,
            line_no=existing_line_no + 1,
            material_id=material_id,
            loan_qty=loan_qty,
            expected_return_date=expected_return_date,
            batch_id=batch_id,
            location_id=location_id,
            line_status=SampleLoanItem.LineStatus.OUT,
        )
        messages.success(request, "借样明细已新增")
        return redirect("sales:sample_loan_detail", pk=pk)


class SampleLoanConfirmOutView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sample_loan_detail", pk)
        if verification_response:
            return verification_response
        if not _filter_sample_loan_queryset_for_user(SampleLoan.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "借样单不存在或无权限操作")
            return redirect("sales:sample_loan_list")
        result = confirm_sample_loan_out(pk, request.user.id, f"sample-out:{pk}")
        if result.success:
            record_audit_log_from_request(request, "sample_loan_confirm_out", "sample_loan", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "借样出库确认失败")
        return redirect("sales:sample_loan_detail", pk=pk)


class SampleLoanConvertToSalesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        require_erp_permission(request.user, PermissionCode.FINANCE_VIEW_AMOUNT, "缺少金额查看权限，不能执行借样转销售")
        from decimal import Decimal, InvalidOperation

        try:
            if not _filter_sample_loan_queryset_for_user(SampleLoan.objects.all(), request.user).filter(pk=pk).exists():
                messages.error(request, "借样单不存在或无权限操作")
                return redirect("sales:sample_loan_list")
            sample_loan_item_id = int(request.POST.get("sample_loan_item", ""))
            convert_qty = Decimal(request.POST.get("convert_qty", ""))
            unit_price = Decimal(request.POST.get("unit_price", ""))
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "借样明细、转销售数量和单价必须正确填写")
            return redirect("sales:sample_loan_detail", pk=pk)

        result = convert_sample_loan_item_to_sales_order(
            sample_loan_item_id,
            convert_qty,
            unit_price,
            request.user.id,
            f"sample-to-sales:{sample_loan_item_id}:{convert_qty}:{unit_price}",
        )
        if result.success:
            messages.success(request, result.message)
            return redirect("sales:sales_order_detail", pk=result.data["sales_order_id"])
        messages.error(request, result.message or result.error_code or "借样转销售失败")
        return redirect("sales:sample_loan_detail", pk=pk)


class SampleLoanReturnCreateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        loan = self._get_loan(request, pk)
        if not loan:
            return redirect("sales:sample_loan_list")
        if loan.status not in [SampleLoan.Status.OUT, SampleLoan.Status.PART_RETURNED, SampleLoan.Status.PART_SOLD]:
            messages.error(request, "只有已出库、部分归还或部分转销售借样单可以登记归还")
            return redirect("sales:sample_loan_detail", pk=pk)
        form = SampleLoanReturnForm(sample_loan=loan)
        item_formset = SampleLoanReturnItemFormSet(sample_loan=loan)
        return self._render(request, loan, None, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        loan = self._get_loan(request, pk)
        if not loan:
            return redirect("sales:sample_loan_list")
        if loan.status not in [SampleLoan.Status.OUT, SampleLoan.Status.PART_RETURNED, SampleLoan.Status.PART_SOLD]:
            messages.error(request, "只有已出库、部分归还或部分转销售借样单可以登记归还")
            return redirect("sales:sample_loan_detail", pk=pk)
        submit_for_confirm = request.POST.get("action") == "submit"
        form = SampleLoanReturnForm(request.POST, sample_loan=loan)
        item_formset = SampleLoanReturnItemFormSet(
            request.POST,
            sample_loan=loan,
            require_ready=submit_for_confirm,
        )
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, loan, None, form, item_formset)

        with transaction.atomic():
            sample_return = form.save(commit=False)
            sample_return.status = SampleLoanReturn.Status.PENDING_CONFIRM if submit_for_confirm else SampleLoanReturn.Status.DRAFT
            sample_return.save()
            item_formset.instance = sample_return
            item_formset.save()

        messages.success(request, "借样归还单已保存")
        return redirect("sales:sample_loan_return_detail", pk=sample_return.pk)

    def _get_loan(self, request, pk):
        try:
            return _filter_sample_loan_queryset_for_user(SampleLoan.objects.prefetch_related("items"), request.user).get(pk=pk)
        except SampleLoan.DoesNotExist:
            messages.error(request, "借样单不存在或无权限操作")
            return None

    def _render(self, request, loan, sample_return, form, item_formset):
        return render(
            request,
            "sales/sample_loan_return_form.html",
            {
                "page_title": f"登记借样归还 {loan.sample_loan_no}",
                "loan": loan,
                "sample_return": sample_return,
                "form": form,
                "item_formset": item_formset,
                "can_submit_for_approval": _can_process_sales(request.user),
            },
        )


class SampleLoanReturnDetailView(LoginRequiredMixin, DetailView):
    model = SampleLoanReturn
    template_name = "sales/sample_loan_return_detail.html"
    context_object_name = "sample_return"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SampleLoanReturnListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sample_return_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("sample_loan", "customer")
            .prefetch_related("items__sample_loan_item", "items__material", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_process_sales = _can_process_sales(self.request.user)
        context["page_title"] = f"借样归还 {self.object.sample_return_no}"
        context["can_edit"] = can_process_sales and self.object.status == SampleLoanReturn.Status.DRAFT
        context["can_void"] = can_process_sales and self.object.status in [SampleLoanReturn.Status.DRAFT, SampleLoanReturn.Status.PENDING_CONFIRM]
        context["can_confirm"] = can_process_sales and self.object.status == SampleLoanReturn.Status.PENDING_CONFIRM
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "sample_loan_return",
            self.object.id,
            self.object.sample_return_no,
        )
        return context


class SampleLoanReturnPrintView(LoginRequiredMixin, DetailView):
    model = SampleLoanReturn
    template_name = "sales/sample_loan_return_print.html"
    context_object_name = "sample_return"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            require_any_erp_permission(request.user, SampleLoanReturnListView.view_permission_required, "缺少销售数据查看权限")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _filter_sample_return_queryset_for_user(super().get_queryset(), self.request.user)
            .select_related("sample_loan", "customer")
            .prefetch_related("items__sample_loan_item", "items__material", "items__location")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印借样归还 {self.object.sample_return_no}"
        record_print_log(
            template_type="sample_loan_return",
            source_doc_type="sample_loan_return",
            source_doc_id=self.object.id,
            source_doc_no=self.object.sample_return_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SampleLoanReturnUpdateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        sample_return = self._get_sample_return(request, pk)
        if not sample_return:
            return redirect("sales:sample_loan_list")
        if sample_return.status != SampleLoanReturn.Status.DRAFT:
            messages.error(request, "只有草稿借样归还单可以编辑")
            return redirect("sales:sample_loan_return_detail", pk=pk)
        form = SampleLoanReturnForm(instance=sample_return, sample_loan=sample_return.sample_loan)
        item_formset = SampleLoanReturnItemFormSet(instance=sample_return, sample_loan=sample_return.sample_loan)
        return self._render(request, sample_return, form, item_formset)

    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        sample_return = self._get_sample_return(request, pk, for_update=True)
        if not sample_return:
            return redirect("sales:sample_loan_list")
        if sample_return.status != SampleLoanReturn.Status.DRAFT:
            messages.error(request, "只有草稿借样归还单可以编辑")
            return redirect("sales:sample_loan_return_detail", pk=pk)

        before_snapshot = _sample_return_snapshot(sample_return)
        submit_for_confirm = request.POST.get("action") == "submit"
        form = SampleLoanReturnForm(request.POST, instance=sample_return, sample_loan=sample_return.sample_loan)
        item_formset = SampleLoanReturnItemFormSet(
            request.POST,
            instance=sample_return,
            sample_loan=sample_return.sample_loan,
            require_ready=submit_for_confirm,
        )
        if not form.is_valid() or not item_formset.is_valid():
            return self._render(request, sample_return, form, item_formset)

        with transaction.atomic():
            sample_return = form.save(commit=False)
            sample_return.status = SampleLoanReturn.Status.PENDING_CONFIRM if submit_for_confirm else SampleLoanReturn.Status.DRAFT
            sample_return.save()
            item_formset.instance = sample_return
            item_formset.save()
            after_snapshot = {
                **_sample_return_snapshot(sample_return),
                "operation_reason": optional_post_reason(request, default="页面编辑借样归还单"),
            }

        record_audit_log_from_request(
            request,
            "sample_return_update",
            "sample_loan_return",
            sample_return.id,
            sample_return.sample_return_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "借样归还单已更新")
        return redirect("sales:sample_loan_return_detail", pk=sample_return.pk)

    def _get_sample_return(self, request, pk, for_update=False):
        queryset = (
            _filter_sample_return_queryset_for_user(SampleLoanReturn.objects.all(), request.user)
            .select_related("sample_loan", "customer")
            .prefetch_related("items__sample_loan_item", "items__material", "items__location")
        )
        if for_update:
            queryset = queryset.select_for_update()
        try:
            return queryset.get(pk=pk)
        except SampleLoanReturn.DoesNotExist:
            messages.error(request, "借样归还单不存在或无权限操作")
            return None

    def _render(self, request, sample_return, form, item_formset):
        return render(
            request,
            "sales/sample_loan_return_form.html",
            {
                "page_title": f"编辑借样归还 {sample_return.sample_return_no}",
                "loan": sample_return.sample_loan,
                "sample_return": sample_return,
                "form": form,
                "item_formset": item_formset,
                "can_submit_for_approval": _can_process_sales(request.user),
                "is_edit": True,
            },
        )


class SampleLoanReturnVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sample_loan_return_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "sales:sample_loan_return_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写借样归还单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                sample_return = (
                    _filter_sample_return_queryset_for_user(SampleLoanReturn.objects.select_for_update(), request.user)
                    .prefetch_related("items__sample_loan_item", "items__material")
                    .get(pk=pk)
                )
                if sample_return.status not in [SampleLoanReturn.Status.DRAFT, SampleLoanReturn.Status.PENDING_CONFIRM]:
                    messages.error(request, "只有草稿或待确认借样归还单可以作废")
                    return redirect("sales:sample_loan_return_detail", pk=pk)
                before_snapshot = _sample_return_snapshot(sample_return)
                sample_return.status = SampleLoanReturn.Status.VOIDED
                sample_return.save(update_fields=["status"])
                after_snapshot = _sample_return_snapshot(sample_return)
        except SampleLoanReturn.DoesNotExist:
            messages.error(request, "借样归还单不存在或无权限操作")
            return redirect("sales:sample_loan_list")

        record_audit_log_from_request(
            request,
            "sample_return_void",
            "sample_loan_return",
            sample_return.id,
            sample_return.sample_return_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "借样归还单已作废")
        return redirect("sales:sample_loan_return_detail", pk=pk)


class SampleLoanReturnConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        require_erp_permission(request.user, PermissionCode.SALES_PROCESS, "缺少销售单据处理权限")
        verification_response = require_second_verify(request, "sales:sample_loan_return_detail", pk)
        if verification_response:
            return verification_response
        if not _filter_sample_return_queryset_for_user(SampleLoanReturn.objects.all(), request.user).filter(pk=pk).exists():
            messages.error(request, "借样归还单不存在或无权限操作")
            return redirect("sales:sample_loan_list")
        result = confirm_sample_return(pk, request.user.id, f"sample-return:{pk}")
        if result.success:
            record_audit_log_from_request(request, "sample_return_confirm", "sample_loan_return", pk, after_snapshot=result.data)
            messages.success(request, result.message)
        else:
            messages.error(request, result.message or result.error_code or "借样归还确认失败")
        return redirect("sales:sample_loan_return_detail", pk=pk)


def _can_view_all_sales(user) -> bool:
    return user_has_permission(user, PermissionCode.SALES_VIEW_ALL)


def _can_process_sales(user) -> bool:
    return user_has_permission(user, PermissionCode.SALES_PROCESS)


def _sales_order_has_shippable_items(order: SalesOrder) -> bool:
    return any(
        item.line_status == SalesOrderItem.LineStatus.CONFIRMED
        and item.inventory_check_status == SalesOrderItem.InventoryCheckStatus.SUFFICIENT
        and item.order_qty > item.shipped_qty
        for item in order.items.all()
    )


def _sales_order_snapshot(order: SalesOrder) -> dict:
    order.refresh_from_db()
    items = order.items.select_related("customer_product", "finished_material").order_by("line_no", "id")
    return {
        "sales_order_no": order.sales_order_no,
        "customer_id": order.customer_id,
        "customer_address_id": order.customer_address_id,
        "order_date": order.order_date.isoformat() if order.order_date else "",
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else "",
        "status": order.status,
        "total_amount": str(order.total_amount),
        "version": order.version,
        "remark": order.remark,
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "customer_product_id": item.customer_product_id,
                "finished_material_id": item.finished_material_id,
                "order_qty": str(item.order_qty),
                "unit_price": str(item.unit_price),
                "line_amount": str(item.line_amount),
                "line_status": item.line_status,
            }
            for item in items
        ],
    }


def _customer_return_snapshot(customer_return: CustomerReturn) -> dict:
    customer_return.refresh_from_db()
    items = customer_return.items.select_related("sales_order_item", "material", "location").order_by("id")
    return {
        "return_no": customer_return.return_no,
        "customer_id": customer_return.customer_id,
        "sales_order_id": customer_return.sales_order_id,
        "return_date": customer_return.return_date.isoformat() if customer_return.return_date else "",
        "status": customer_return.status,
        "return_amount": str(customer_return.return_amount),
        "remark": customer_return.remark,
        "items": [
            {
                "id": item.id,
                "sales_order_item_id": item.sales_order_item_id,
                "material_id": item.material_id,
                "return_qty": str(item.return_qty),
                "unit_price": str(item.unit_price),
                "return_amount": str(item.return_amount),
                "location_id": item.location_id,
                "inventory_type": item.inventory_type,
            }
            for item in items
        ],
    }


def _sales_shipment_snapshot(shipment: SalesShipment) -> dict:
    shipment.refresh_from_db()
    items = shipment.items.select_related("sales_order_item", "material", "batch", "location").order_by("id")
    return {
        "shipment_no": shipment.shipment_no,
        "sales_order_id": shipment.sales_order_id,
        "customer_id": shipment.customer_id,
        "shipment_date": shipment.shipment_date.isoformat() if shipment.shipment_date else "",
        "status": shipment.status,
        "remark": shipment.remark,
        "items": [
            {
                "id": item.id,
                "sales_order_item_id": item.sales_order_item_id,
                "material_id": item.material_id,
                "shipment_qty": str(item.shipment_qty),
                "batch_id": item.batch_id,
                "location_id": item.location_id,
                "cost_price": str(item.cost_price) if item.cost_price is not None else "",
            }
            for item in items
        ],
    }


def _sample_return_snapshot(sample_return: SampleLoanReturn) -> dict:
    sample_return.refresh_from_db()
    items = sample_return.items.select_related("sample_loan_item", "material", "location").order_by("id")
    return {
        "sample_return_no": sample_return.sample_return_no,
        "sample_loan_id": sample_return.sample_loan_id,
        "customer_id": sample_return.customer_id,
        "return_date": sample_return.return_date.isoformat() if sample_return.return_date else "",
        "status": sample_return.status,
        "remark": sample_return.remark,
        "items": [
            {
                "id": item.id,
                "sample_loan_item_id": item.sample_loan_item_id,
                "material_id": item.material_id,
                "return_qty": str(item.return_qty),
                "location_id": item.location_id,
                "inventory_type": item.inventory_type,
                "sample_condition": item.sample_condition,
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


def _filter_customer_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(sales_owner=user)


def _filter_sales_order_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(created_by=user)).distinct()


def _filter_shortage_queryset_for_user(queryset, user):
    if _can_view_all_sales(user) or user_has_permission(user, PermissionCode.PURCHASE_VIEW) or user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset
    return queryset.filter(Q(sales_order__customer__sales_owner=user) | Q(sales_order__created_by=user)).distinct()


def _filter_sales_shipment_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(sales_order__created_by=user) | Q(created_by=user)).distinct()


def _filter_customer_return_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(sales_order__created_by=user)).distinct()


def _filter_sample_loan_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(created_by=user)).distinct()


def _filter_sample_return_queryset_for_user(queryset, user):
    if _can_view_all_sales(user):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(sample_loan__created_by=user)).distinct()
