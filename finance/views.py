import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.utils import timezone

from accounts.permissions import PermissionCode, user_has_any_permission, user_has_permission
from files.models import Attachment
from files.services import csv_upload_validation_error, export_queryset_to_csv, record_print_log, uploaded_csv_text_file
from files.view_helpers import build_attachment_panel, export_file_response
from masterdata.models import Customer, Supplier
from purchase.models import PurchaseReceipt, SupplierReturn
from sales.models import CustomerReturn, SalesOrder
from system.date_utils import parse_user_date
from system.services import next_document_no, record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason, require_post_reason, require_second_verify

from .forms import ExpenseRecordForm, OpeningPayableForm, OpeningReceivableForm
from .import_services import (
    CUSTOMER_RECEIPT_IMPORT_TEMPLATE_ROWS,
    SUPPLIER_PAYMENT_IMPORT_TEMPLATE_ROWS,
    import_customer_receipts_from_csv,
    import_supplier_payments_from_csv,
)
from .models import (
    CustomerCreditBalance,
    CustomerCreditBalanceTransaction,
    CustomerInvoice,
    CustomerInvoiceItem,
    CustomerReceipt,
    CustomerReceiptAllocation,
    CustomerReceiptReversal,
    ExpenseRecord,
    OpeningPayable,
    OpeningReceivable,
    Reconciliation,
    ReconciliationItem,
    SupplierCreditBalance,
    SupplierCreditBalanceTransaction,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentReversal,
)
from .services import (
    apply_customer_credit_balance,
    apply_supplier_credit_balance,
    confirm_customer_receipt,
    confirm_supplier_payment,
    customer_opening_receivable_available_allocation_amount,
    customer_order_available_allocation_amount,
    customer_reconciliation_available_allocation_amount,
    reverse_customer_receipt,
    reverse_supplier_payment,
    supplier_opening_payable_available_allocation_amount,
    supplier_receipt_available_allocation_amount,
    supplier_reconciliation_available_allocation_amount,
)


ZERO_AMOUNT = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


class OperationsDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "finance/operations_dashboard.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        month_start = today.replace(day=1)
        year_start = today.replace(month=1, day=1)
        context["page_title"] = "经营看板"
        context["today"] = today
        context["month_start"] = month_start
        context["year_start"] = year_start
        context["month"] = _operations_period_summary(month_start, today)
        context["year"] = _operations_period_summary(year_start, today)
        context["balances"] = _operations_balance_summary()
        context["recent_expenses"] = ExpenseRecord.objects.filter(status=ExpenseRecord.Status.CONFIRMED).order_by("-expense_date", "-id")[:8]
        return context


class CustomerReceiptListView(ErpListView):
    model = CustomerReceipt
    page_title = "客户收款"
    view_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.SALES_PROCESS)
    permission_denied_message = "缺少客户收款查看权限"
    create_url_name = "finance:customer_receipt_create"
    detail_url_name = "finance:customer_receipt_detail"
    columns = (
        ("收款单号", "receipt_no"),
        ("客户", "customer.customer_name"),
        ("收款日期", "receipt_date"),
        ("金额", "receipt_amount"),
        ("未分配", "unallocated_amount"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("receipt_amount", "unallocated_amount")
    ordering = ["-receipt_date", "-id"]
    page_actions = (
        ("导出CSV", "finance:customer_receipt_export", ""),
        ("下载导入模板", "finance:customer_receipt_import_template", ""),
        ("导入CSV", "finance:customer_receipt_import", "primary"),
    )
    page_action_permissions = {
        "finance:customer_receipt_import_template": (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS),
        "finance:customer_receipt_import": (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS),
    }
    search_fields = ("receipt_no", "customer__customer_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "收款单号", "param": "receipt_no", "field": "receipt_no", "placeholder": "收款单号"},
        {"label": "客户", "param": "customer_name", "field": "customer__customer_name", "placeholder": "客户名称"},
        {
            "label": "收款方式",
            "param": "receipt_method",
            "field": "receipt_method",
            "lookup": "exact",
            "type": "select",
            "choices": CustomerReceipt.ReceiptMethod.choices,
        },
        {"label": "经办人", "param": "handled_by", "field": "handled_by__username", "placeholder": "经办人账号"},
    )
    sortable_fields = {
        "receipt_no": "receipt_no",
        "customer.customer_name": "customer__customer_name",
        "receipt_date": "receipt_date",
        "receipt_amount": "receipt_amount",
        "unallocated_amount": "unallocated_amount",
        "get_status_display": "status",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_customer_receipt_amount(self.request.user)
        return context

    def get_queryset(self):
        return _filter_customer_receipt_queryset_for_user(super().get_queryset(), self.request.user)

    def get_create_url_name(self) -> str:
        return self.create_url_name if _can_process_customer_receipt(self.request.user) else ""

    def get_scope_filter_options(self):
        if _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.SALES_VIEW_ALL):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            return queryset.filter(
                Q(customer__sales_owner=self.request.user)
                | Q(customer__created_by=self.request.user)
                | Q(customer__sales_orders__created_by=self.request.user)
                | Q(created_by=self.request.user)
                | Q(handled_by=self.request.user)
            ).distinct()
        if scope_value == "unassigned" and (
            _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.SALES_VIEW_ALL)
        ):
            return queryset.filter(customer__sales_owner__isnull=True)
        return queryset


class FinanceCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            required_permissions = getattr(self.list_view_class, "view_permission_required", PermissionCode.FINANCE_VIEW_AMOUNT)
            if not user_has_any_permission(request.user, required_permissions):
                raise PermissionDenied("缺少导出权限")
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
        if _can_view_finance_amount(self.request.user):
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


class CustomerReceiptExportView(FinanceCsvExportView):
    module = "customer_receipts"
    list_view_class = CustomerReceiptListView
    ordering = ("-receipt_date", "-id")
    select_related = ("customer",)

    def get_mask_fields(self):
        return () if _can_view_customer_receipt_amount(self.request.user) else self.list_view_class.sensitive_columns


class CustomerReceiptImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        _require_finance_payment_process(request.user)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(CUSTOMER_RECEIPT_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="customer_receipt_import_template.csv"'
        return response


class CustomerReceiptImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_payment_process(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入客户收款"
        context["list_url_name"] = "finance:customer_receipt_list"
        context["template_url_name"] = "finance:customer_receipt_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("finance:customer_receipt_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_customer_receipts_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("finance:customer_receipt_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class CustomerReceiptCreateView(LoginRequiredMixin, TemplateView):
    template_name = "finance/customer_receipt_form.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_customer_receipt_process(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建客户收款"
        context["customers"] = _customer_receipt_customer_queryset(self.request.user).order_by("customer_no")
        context["receipt_methods"] = CustomerReceipt.ReceiptMethod.choices
        context["today"] = timezone.localdate()
        return context

    def post(self, request):
        amount = _decimal_from_post(request, "receipt_amount")
        receipt_date = _date_from_post(request, "receipt_date")
        customer_id = request.POST.get("customer")
        if (
            amount is None
            or amount <= 0
            or not receipt_date
            or not customer_id
            or not _customer_receipt_customer_queryset(request.user).filter(pk=customer_id).exists()
        ):
            messages.error(request, "客户、收款日期和收款金额必须正确填写")
            return redirect("finance:customer_receipt_create")

        receipt = CustomerReceipt.objects.create(
            receipt_no=next_document_no("RC"),
            customer_id=customer_id,
            receipt_date=receipt_date,
            receipt_amount=amount,
            unallocated_amount=amount,
            receipt_method=request.POST.get("receipt_method") or CustomerReceipt.ReceiptMethod.TRANSFER,
            status=CustomerReceipt.Status.PENDING_APPROVAL,
            handled_by=request.user,
            created_by=request.user,
            remark=request.POST.get("remark", "").strip(),
        )
        messages.success(request, "客户收款已创建")
        return redirect("finance:customer_receipt_detail", pk=receipt.pk)


class CustomerReceiptDetailView(LoginRequiredMixin, DetailView):
    model = CustomerReceipt
    template_name = "finance/customer_receipt_detail.html"
    context_object_name = "receipt"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_customer_receipt_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("customer", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__sales_order", "allocations__reconciliation", "reversals")
        )
        return _filter_customer_receipt_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"客户收款 {self.object.receipt_no}"
        context["can_view_amount"] = _can_view_customer_receipt_amount(self.request.user)
        context["can_process_payment"] = _can_process_customer_receipt(self.request.user)
        context["can_reverse"] = _can_process_full_finance_payment(self.request.user) and self.object.status in [
            CustomerReceipt.Status.CONFIRMED,
            CustomerReceipt.Status.PART_REVERSED,
        ]
        context["can_confirm"] = (
            context["can_view_amount"]
            and context["can_process_payment"]
            and self.object.status == CustomerReceipt.Status.PENDING_APPROVAL
        )
        context["can_edit"] = (
            context["can_view_amount"]
            and context["can_process_payment"]
            and self.object.status in [CustomerReceipt.Status.DRAFT, CustomerReceipt.Status.PENDING_APPROVAL]
        )
        context["can_void"] = context["can_edit"]
        allocation_targets, reconciliation_targets, opening_targets = _customer_allocation_target_groups(self.object, self.request.user)
        context["allocation_targets"] = allocation_targets
        context["reconciliation_allocation_targets"] = reconciliation_targets
        context["opening_allocation_targets"] = opening_targets
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "customer_receipt",
            self.object.id,
            self.object.receipt_no,
        )
        return context


class CustomerReceiptPrintView(LoginRequiredMixin, DetailView):
    model = CustomerReceipt
    template_name = "finance/customer_receipt_print.html"
    context_object_name = "receipt"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_customer_receipt_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("customer", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__sales_order", "allocations__reconciliation")
        )
        return _filter_customer_receipt_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印客户收款 {self.object.receipt_no}"
        context["can_view_amount"] = _can_view_customer_receipt_amount(self.request.user)
        record_print_log(
            template_type="customer_receipt",
            source_doc_type="customer_receipt",
            source_doc_id=self.object.id,
            source_doc_no=self.object.receipt_no,
            printed_by_id=self.request.user.id,
        )
        return context


class CustomerReceiptUpdateView(LoginRequiredMixin, View):
    editable_statuses = [CustomerReceipt.Status.DRAFT, CustomerReceipt.Status.PENDING_APPROVAL]

    def get(self, request, pk):
        _require_customer_receipt_process(request.user)
        receipt = _filter_customer_receipt_queryset_for_user(
            CustomerReceipt.objects.select_related("customer"), request.user
        ).filter(pk=pk).first()
        if receipt is None:
            messages.error(request, "客户收款单不存在")
            return redirect("finance:customer_receipt_list")
        if receipt.status not in self.editable_statuses:
            messages.error(request, "只有草稿或待审核客户收款单可以编辑")
            return redirect("finance:customer_receipt_detail", pk=pk)
        return self._render(request, receipt)

    def post(self, request, pk):
        _require_customer_receipt_process(request.user)
        amount = _decimal_from_post(request, "receipt_amount")
        receipt_date = _date_from_post(request, "receipt_date")
        customer_id = request.POST.get("customer")
        receipt_method = request.POST.get("receipt_method") or CustomerReceipt.ReceiptMethod.TRANSFER
        if (
            amount is None
            or amount <= 0
            or not receipt_date
            or not customer_id
            or receipt_method not in CustomerReceipt.ReceiptMethod.values
            or not _customer_receipt_customer_queryset(request.user).filter(pk=customer_id).exists()
        ):
            messages.error(request, "客户、收款日期、收款金额和收款方式必须正确填写")
            return redirect("finance:customer_receipt_edit", pk=pk)

        try:
            with transaction.atomic():
                receipt = _filter_customer_receipt_queryset_for_user(
                    CustomerReceipt.objects.select_for_update(), request.user
                ).get(pk=pk)
                if receipt.status not in self.editable_statuses:
                    messages.error(request, "只有草稿或待审核客户收款单可以编辑")
                    return redirect("finance:customer_receipt_detail", pk=pk)
                if receipt.allocations.select_for_update().exists():
                    messages.error(request, "已有核销明细的客户收款单不能编辑")
                    return redirect("finance:customer_receipt_detail", pk=pk)
                before_snapshot = _customer_receipt_snapshot(receipt)
                receipt.customer_id = customer_id
                receipt.receipt_date = receipt_date
                receipt.receipt_amount = amount
                receipt.unallocated_amount = amount
                receipt.receipt_method = receipt_method
                receipt.handled_by = request.user
                receipt.remark = request.POST.get("remark", "").strip()
                receipt.save(
                    update_fields=[
                        "customer",
                        "receipt_date",
                        "receipt_amount",
                        "unallocated_amount",
                        "receipt_method",
                        "handled_by",
                        "remark",
                    ]
                )
                after_snapshot = {
                    **_customer_receipt_snapshot(receipt),
                    "operation_reason": optional_post_reason(request, default="页面编辑客户收款单"),
                }
        except CustomerReceipt.DoesNotExist:
            messages.error(request, "客户收款单不存在")
            return redirect("finance:customer_receipt_list")

        record_audit_log_from_request(
            request,
            "customer_receipt_update",
            "customer_receipt",
            receipt.id,
            receipt.receipt_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "客户收款单已更新")
        return redirect("finance:customer_receipt_detail", pk=pk)

    def _render(self, request, receipt):
        return render(
            request,
            "finance/customer_receipt_form.html",
            {
                "page_title": f"编辑客户收款 {receipt.receipt_no}",
                "customers": _customer_receipt_customer_queryset(request.user).order_by("customer_no"),
                "receipt_methods": CustomerReceipt.ReceiptMethod.choices,
                "today": timezone.localdate(),
                "receipt": receipt,
                "is_edit": True,
            },
        )


class CustomerReceiptVoidView(LoginRequiredMixin, View):
    voidable_statuses = [CustomerReceipt.Status.DRAFT, CustomerReceipt.Status.PENDING_APPROVAL]

    def post(self, request, pk):
        _require_customer_receipt_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_receipt_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:customer_receipt_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写客户收款单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                receipt = _filter_customer_receipt_queryset_for_user(
                    CustomerReceipt.objects.select_for_update(), request.user
                ).get(pk=pk)
                if receipt.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿或待审核客户收款单可以作废")
                    return redirect("finance:customer_receipt_detail", pk=pk)
                if receipt.allocations.select_for_update().exists():
                    messages.error(request, "已有核销明细的客户收款单不能作废")
                    return redirect("finance:customer_receipt_detail", pk=pk)
                before_snapshot = _customer_receipt_snapshot(receipt)
                receipt.status = CustomerReceipt.Status.VOIDED
                receipt.save(update_fields=["status"])
                after_snapshot = _customer_receipt_snapshot(receipt)
        except CustomerReceipt.DoesNotExist:
            messages.error(request, "客户收款单不存在")
            return redirect("finance:customer_receipt_list")

        record_audit_log_from_request(
            request,
            "customer_receipt_void",
            "customer_receipt",
            receipt.id,
            receipt.receipt_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "客户收款单已作废")
        return redirect("finance:customer_receipt_detail", pk=pk)


class CustomerReceiptConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_customer_receipt_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_receipt_detail", pk)
        if verification_response:
            return verification_response
        allocations = _allocation_rows_from_post(request, "sales_order_id", "sales_order_allocated_amount")
        allocations += _allocation_rows_from_post(request, "reconciliation_id", "reconciliation_allocated_amount")
        allocations += _allocation_rows_from_post(request, "opening_receivable_id", "opening_receivable_allocated_amount")
        receipt = _filter_customer_receipt_queryset_for_user(CustomerReceipt.objects.all(), request.user).filter(pk=pk).first()
        if receipt is None:
            raise Http404("客户收款单不存在")
        _validate_customer_receipt_allocations_for_user(receipt, allocations, request.user)
        result = confirm_customer_receipt(
            pk,
            allocations,
            request.user.id,
            f"customer-receipt-confirm:{pk}:{_allocation_signature(allocations)}",
        )
        if result.success:
            record_audit_log_from_request(request, "customer_receipt_confirm", "customer_receipt", pk, after_snapshot=result.data)
        _flash_result(request, result, "客户收款确认失败")
        return redirect("finance:customer_receipt_detail", pk=pk)


class CustomerReceiptReverseView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_receipt_detail", pk)
        if verification_response:
            return verification_response
        amount = _decimal_from_post(request, "reversal_amount")
        reason = request.POST.get("reason", "").strip()
        if amount is None:
            messages.error(request, "红冲金额格式不正确")
            return redirect("finance:customer_receipt_detail", pk=pk)
        result = reverse_customer_receipt(pk, amount, reason, request.user.id, f"customer-receipt-reverse:{pk}:{amount}:{reason}")
        if result.success:
            record_audit_log_from_request(
                request,
                "customer_receipt_reverse",
                "customer_receipt",
                pk,
                after_snapshot={"amount": str(amount), "reason": reason, **result.data},
            )
        _flash_result(request, result, "客户收款红冲失败")
        return redirect("finance:customer_receipt_detail", pk=pk)


class SupplierPaymentListView(ErpListView):
    model = SupplierPayment
    page_title = "供应商付款"
    view_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.PURCHASE_PROCESS)
    permission_denied_message = "缺少供应商付款查看权限"
    create_url_name = "finance:supplier_payment_create"
    detail_url_name = "finance:supplier_payment_detail"
    columns = (
        ("付款单号", "payment_no"),
        ("供应商", "supplier.supplier_name"),
        ("付款日期", "payment_date"),
        ("金额", "payment_amount"),
        ("未分配", "unallocated_amount"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("payment_amount", "unallocated_amount")
    ordering = ["-payment_date", "-id"]
    page_actions = (
        ("导出CSV", "finance:supplier_payment_export", ""),
        ("下载导入模板", "finance:supplier_payment_import_template", ""),
        ("导入CSV", "finance:supplier_payment_import", "primary"),
    )
    page_action_permissions = {
        "finance:supplier_payment_import_template": (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS),
        "finance:supplier_payment_import": (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS),
    }
    search_fields = ("payment_no", "supplier__supplier_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "付款单号", "param": "payment_no", "field": "payment_no", "placeholder": "付款单号"},
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
        {
            "label": "付款方式",
            "param": "payment_method",
            "field": "payment_method",
            "lookup": "exact",
            "type": "select",
            "choices": SupplierPayment.PaymentMethod.choices,
        },
        {"label": "经办人", "param": "handled_by", "field": "handled_by__username", "placeholder": "经办人账号"},
    )
    sortable_fields = {
        "payment_no": "payment_no",
        "supplier.supplier_name": "supplier__supplier_name",
        "payment_date": "payment_date",
        "payment_amount": "payment_amount",
        "unallocated_amount": "unallocated_amount",
        "get_status_display": "status",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_supplier_payment_amount(self.request.user)
        return context

    def get_queryset(self):
        return _filter_supplier_payment_queryset_for_user(super().get_queryset(), self.request.user)

    def get_create_url_name(self) -> str:
        return self.create_url_name if _can_process_supplier_payment(self.request.user) else ""

    def get_scope_filter_options(self):
        if _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.PURCHASE_VIEW):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            return queryset.filter(
                Q(created_by=self.request.user)
                | Q(handled_by=self.request.user)
                | Q(allocations__purchase_receipt__purchase_order__purchase_owner=self.request.user)
                | Q(allocations__purchase_receipt__purchase_order__created_by=self.request.user)
                | Q(allocations__purchase_receipt__created_by=self.request.user)
            ).distinct()
        if scope_value == "unassigned" and (
            _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.PURCHASE_VIEW)
        ):
            return queryset.filter(handled_by__isnull=True)
        return queryset


class SupplierPaymentExportView(FinanceCsvExportView):
    module = "supplier_payments"
    list_view_class = SupplierPaymentListView
    ordering = ("-payment_date", "-id")
    select_related = ("supplier",)

    def get_mask_fields(self):
        return () if _can_view_supplier_payment_amount(self.request.user) else self.list_view_class.sensitive_columns


class SupplierPaymentImportTemplateView(LoginRequiredMixin, View):
    def get(self, request):
        _require_finance_payment_process(request.user)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(SUPPLIER_PAYMENT_IMPORT_TEMPLATE_ROWS)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="supplier_payment_import_template.csv"'
        return response


class SupplierPaymentImportView(LoginRequiredMixin, TemplateView):
    template_name = "masterdata/csv_import.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_payment_process(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "导入供应商付款"
        context["list_url_name"] = "finance:supplier_payment_list"
        context["template_url_name"] = "finance:supplier_payment_import_template"
        return context

    def post(self, request):
        upload = request.FILES.get("import_file")
        validation_error = csv_upload_validation_error(upload)
        if validation_error:
            messages.error(request, validation_error)
            return redirect("finance:supplier_payment_import")
        text_file = uploaded_csv_text_file(upload)
        result = import_supplier_payments_from_csv(text_file, request.user.id)
        if result.success:
            messages.success(request, f"{result.message}，成功 {result.data['success_count']} 张")
            return redirect("finance:supplier_payment_list")
        return self.render_to_response(
            self.get_context_data(
                errors=result.data.get("errors", []),
                import_job_id=result.data.get("import_job_id"),
            )
        )


class SupplierPaymentCreateView(LoginRequiredMixin, TemplateView):
    template_name = "finance/supplier_payment_form.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_supplier_payment_process(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建供应商付款"
        context["suppliers"] = _supplier_payment_supplier_queryset(self.request.user).order_by("supplier_no")
        context["payment_methods"] = SupplierPayment.PaymentMethod.choices
        context["today"] = timezone.localdate()
        return context

    def post(self, request):
        amount = _decimal_from_post(request, "payment_amount")
        payment_date = _date_from_post(request, "payment_date")
        supplier_id = request.POST.get("supplier")
        if (
            amount is None
            or amount <= 0
            or not payment_date
            or not supplier_id
            or not _supplier_payment_supplier_queryset(request.user).filter(pk=supplier_id).exists()
        ):
            messages.error(request, "供应商、付款日期和付款金额必须正确填写")
            return redirect("finance:supplier_payment_create")

        payment = SupplierPayment.objects.create(
            payment_no=next_document_no("PY"),
            supplier_id=supplier_id,
            payment_date=payment_date,
            payment_amount=amount,
            unallocated_amount=amount,
            payment_method=request.POST.get("payment_method") or SupplierPayment.PaymentMethod.TRANSFER,
            status=SupplierPayment.Status.PENDING_APPROVAL,
            handled_by=request.user,
            created_by=request.user,
            remark=request.POST.get("remark", "").strip(),
        )
        messages.success(request, "供应商付款已创建")
        return redirect("finance:supplier_payment_detail", pk=payment.pk)


class SupplierPaymentDetailView(LoginRequiredMixin, DetailView):
    model = SupplierPayment
    template_name = "finance/supplier_payment_detail.html"
    context_object_name = "payment"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_supplier_payment_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("supplier", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__purchase_receipt", "allocations__reconciliation", "reversals")
        )
        return _filter_supplier_payment_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"供应商付款 {self.object.payment_no}"
        context["can_view_amount"] = _can_view_supplier_payment_amount(self.request.user)
        context["can_process_payment"] = _can_process_supplier_payment(self.request.user)
        context["can_reverse"] = _can_process_full_finance_payment(self.request.user) and self.object.status in [
            SupplierPayment.Status.CONFIRMED,
            SupplierPayment.Status.PART_REVERSED,
        ]
        context["can_confirm"] = (
            context["can_view_amount"]
            and context["can_process_payment"]
            and self.object.status == SupplierPayment.Status.PENDING_APPROVAL
        )
        context["can_edit"] = (
            context["can_view_amount"]
            and context["can_process_payment"]
            and self.object.status in [SupplierPayment.Status.DRAFT, SupplierPayment.Status.PENDING_APPROVAL]
        )
        context["can_void"] = context["can_edit"]
        allocation_targets, reconciliation_targets, opening_targets = _supplier_allocation_target_groups(self.object, self.request.user)
        context["allocation_targets"] = allocation_targets
        context["reconciliation_allocation_targets"] = reconciliation_targets
        context["opening_allocation_targets"] = opening_targets
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "supplier_payment",
            self.object.id,
            self.object.payment_no,
        )
        return context


class SupplierPaymentPrintView(LoginRequiredMixin, DetailView):
    model = SupplierPayment
    template_name = "finance/supplier_payment_print.html"
    context_object_name = "payment"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_supplier_payment_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("supplier", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__purchase_receipt", "allocations__reconciliation")
        )
        return _filter_supplier_payment_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印供应商付款 {self.object.payment_no}"
        context["can_view_amount"] = _can_view_supplier_payment_amount(self.request.user)
        record_print_log(
            template_type="supplier_payment",
            source_doc_type="supplier_payment",
            source_doc_id=self.object.id,
            source_doc_no=self.object.payment_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SupplierPaymentUpdateView(LoginRequiredMixin, View):
    editable_statuses = [SupplierPayment.Status.DRAFT, SupplierPayment.Status.PENDING_APPROVAL]

    def get(self, request, pk):
        _require_supplier_payment_process(request.user)
        payment = _filter_supplier_payment_queryset_for_user(
            SupplierPayment.objects.select_related("supplier"), request.user
        ).filter(pk=pk).first()
        if payment is None:
            messages.error(request, "供应商付款单不存在")
            return redirect("finance:supplier_payment_list")
        if payment.status not in self.editable_statuses:
            messages.error(request, "只有草稿或待审核供应商付款单可以编辑")
            return redirect("finance:supplier_payment_detail", pk=pk)
        return self._render(request, payment)

    def post(self, request, pk):
        _require_supplier_payment_process(request.user)
        amount = _decimal_from_post(request, "payment_amount")
        payment_date = _date_from_post(request, "payment_date")
        supplier_id = request.POST.get("supplier")
        payment_method = request.POST.get("payment_method") or SupplierPayment.PaymentMethod.TRANSFER
        if (
            amount is None
            or amount <= 0
            or not payment_date
            or not supplier_id
            or payment_method not in SupplierPayment.PaymentMethod.values
            or not _supplier_payment_supplier_queryset(request.user).filter(pk=supplier_id).exists()
        ):
            messages.error(request, "供应商、付款日期、付款金额和付款方式必须正确填写")
            return redirect("finance:supplier_payment_edit", pk=pk)

        try:
            with transaction.atomic():
                payment = _filter_supplier_payment_queryset_for_user(
                    SupplierPayment.objects.select_for_update(), request.user
                ).get(pk=pk)
                if payment.status not in self.editable_statuses:
                    messages.error(request, "只有草稿或待审核供应商付款单可以编辑")
                    return redirect("finance:supplier_payment_detail", pk=pk)
                if payment.allocations.select_for_update().exists():
                    messages.error(request, "已有核销明细的供应商付款单不能编辑")
                    return redirect("finance:supplier_payment_detail", pk=pk)
                before_snapshot = _supplier_payment_snapshot(payment)
                payment.supplier_id = supplier_id
                payment.payment_date = payment_date
                payment.payment_amount = amount
                payment.unallocated_amount = amount
                payment.payment_method = payment_method
                payment.handled_by = request.user
                payment.remark = request.POST.get("remark", "").strip()
                payment.save(
                    update_fields=[
                        "supplier",
                        "payment_date",
                        "payment_amount",
                        "unallocated_amount",
                        "payment_method",
                        "handled_by",
                        "remark",
                    ]
                )
                after_snapshot = {
                    **_supplier_payment_snapshot(payment),
                    "operation_reason": optional_post_reason(request, default="页面编辑供应商付款单"),
                }
        except SupplierPayment.DoesNotExist:
            messages.error(request, "供应商付款单不存在")
            return redirect("finance:supplier_payment_list")

        record_audit_log_from_request(
            request,
            "supplier_payment_update",
            "supplier_payment",
            payment.id,
            payment.payment_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "供应商付款单已更新")
        return redirect("finance:supplier_payment_detail", pk=pk)

    def _render(self, request, payment):
        return render(
            request,
            "finance/supplier_payment_form.html",
            {
                "page_title": f"编辑供应商付款 {payment.payment_no}",
                "suppliers": _supplier_payment_supplier_queryset(request.user).order_by("supplier_no"),
                "payment_methods": SupplierPayment.PaymentMethod.choices,
                "today": timezone.localdate(),
                "payment": payment,
                "is_edit": True,
            },
        )


class SupplierPaymentVoidView(LoginRequiredMixin, View):
    voidable_statuses = [SupplierPayment.Status.DRAFT, SupplierPayment.Status.PENDING_APPROVAL]

    def post(self, request, pk):
        _require_supplier_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:supplier_payment_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:supplier_payment_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写供应商付款单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                payment = _filter_supplier_payment_queryset_for_user(
                    SupplierPayment.objects.select_for_update(), request.user
                ).get(pk=pk)
                if payment.status not in self.voidable_statuses:
                    messages.error(request, "只有草稿或待审核供应商付款单可以作废")
                    return redirect("finance:supplier_payment_detail", pk=pk)
                if payment.allocations.select_for_update().exists():
                    messages.error(request, "已有核销明细的供应商付款单不能作废")
                    return redirect("finance:supplier_payment_detail", pk=pk)
                before_snapshot = _supplier_payment_snapshot(payment)
                payment.status = SupplierPayment.Status.VOIDED
                payment.save(update_fields=["status"])
                after_snapshot = _supplier_payment_snapshot(payment)
        except SupplierPayment.DoesNotExist:
            messages.error(request, "供应商付款单不存在")
            return redirect("finance:supplier_payment_list")

        record_audit_log_from_request(
            request,
            "supplier_payment_void",
            "supplier_payment",
            payment.id,
            payment.payment_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "供应商付款单已作废")
        return redirect("finance:supplier_payment_detail", pk=pk)


class SupplierPaymentConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_supplier_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:supplier_payment_detail", pk)
        if verification_response:
            return verification_response
        allocations = _allocation_rows_from_post(request, "purchase_receipt_id", "purchase_receipt_allocated_amount")
        allocations += _allocation_rows_from_post(request, "reconciliation_id", "reconciliation_allocated_amount")
        allocations += _allocation_rows_from_post(request, "opening_payable_id", "opening_payable_allocated_amount")
        payment = _filter_supplier_payment_queryset_for_user(SupplierPayment.objects.all(), request.user).filter(pk=pk).first()
        if payment is None:
            raise Http404("供应商付款单不存在")
        _validate_supplier_payment_allocations_for_user(payment, allocations, request.user)
        result = confirm_supplier_payment(
            pk,
            allocations,
            request.user.id,
            f"supplier-payment-confirm:{pk}:{_allocation_signature(allocations)}",
        )
        if result.success:
            record_audit_log_from_request(request, "supplier_payment_confirm", "supplier_payment", pk, after_snapshot=result.data)
        _flash_result(request, result, "供应商付款确认失败")
        return redirect("finance:supplier_payment_detail", pk=pk)


class SupplierPaymentReverseView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:supplier_payment_detail", pk)
        if verification_response:
            return verification_response
        amount = _decimal_from_post(request, "reversal_amount")
        reason = request.POST.get("reason", "").strip()
        if amount is None:
            messages.error(request, "红冲金额格式不正确")
            return redirect("finance:supplier_payment_detail", pk=pk)
        result = reverse_supplier_payment(pk, amount, reason, request.user.id, f"supplier-payment-reverse:{pk}:{amount}:{reason}")
        if result.success:
            record_audit_log_from_request(
                request,
                "supplier_payment_reverse",
                "supplier_payment",
                pk,
                after_snapshot={"amount": str(amount), "reason": reason, **result.data},
            )
        _flash_result(request, result, "供应商付款红冲失败")
        return redirect("finance:supplier_payment_detail", pk=pk)


class OpeningReceivableListView(ErpListView):
    model = OpeningReceivable
    page_title = "期初应收"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    create_url_name = "finance:opening_receivable_create"
    create_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS)
    detail_url_name = "finance:opening_receivable_detail"
    columns = (
        ("期初单号", "opening_no"),
        ("客户", "customer.customer_name"),
        ("来源单号", "source_doc_no"),
        ("建账日期", "opening_date"),
        ("期初金额", "opening_amount"),
        ("未收金额", "remaining_amount"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("opening_amount", "remaining_amount")
    ordering = ["-opening_date", "-id"]
    search_fields = ("opening_no", "source_doc_no", "customer__customer_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "期初单号", "param": "opening_no", "field": "opening_no", "placeholder": "期初单号"},
        {"label": "客户", "param": "customer_name", "field": "customer__customer_name", "placeholder": "客户名称"},
        {"label": "来源单号", "param": "source_doc_no", "field": "source_doc_no", "placeholder": "来源单号"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("customer")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class OpeningReceivableCreateView(LoginRequiredMixin, View):
    template_name = "finance/opening_receivable_form.html"

    def get(self, request):
        _require_finance_payment_process(request.user)
        return render(request, self.template_name, {"page_title": "新建期初应收", "form": OpeningReceivableForm()})

    def post(self, request):
        _require_finance_payment_process(request.user)
        form = OpeningReceivableForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"page_title": "新建期初应收", "form": form})
        opening = form.save(commit=False)
        opening.opening_no = next_document_no("OR")
        opening.remaining_amount = opening.opening_amount
        opening.status = OpeningReceivable.Status.OPEN
        opening.created_by = request.user
        opening.save()
        record_audit_log_from_request(
            request,
            "opening_receivable_create",
            "opening_receivable",
            opening.id,
            opening.opening_no,
            after_snapshot=_opening_receivable_snapshot(opening),
        )
        messages.success(request, "期初应收已创建")
        return redirect("finance:opening_receivable_detail", pk=opening.pk)


class OpeningReceivableDetailView(LoginRequiredMixin, DetailView):
    model = OpeningReceivable
    template_name = "finance/opening_receivable_detail.html"
    context_object_name = "opening"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "created_by").prefetch_related("customerreceiptallocation_set__customer_receipt")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"期初应收 {self.object.opening_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["can_void"] = context["can_process_payment"] and self.object.status in [
            OpeningReceivable.Status.OPEN,
            OpeningReceivable.Status.PART_SETTLED,
        ]
        return context


class OpeningReceivableVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:opening_receivable_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:opening_receivable_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写期初应收作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                opening = OpeningReceivable.objects.select_for_update().get(pk=pk)
                if CustomerReceiptAllocation.objects.select_for_update().filter(opening_receivable=opening, allocated_amount__gt=0).exists():
                    messages.error(request, "已有收款核销的期初应收不能作废，请先红冲对应收款")
                    return redirect("finance:opening_receivable_detail", pk=pk)
                before_snapshot = _opening_receivable_snapshot(opening)
                opening.status = OpeningReceivable.Status.VOIDED
                opening.remaining_amount = ZERO_AMOUNT
                opening.save(update_fields=["status", "remaining_amount"])
                after_snapshot = {**_opening_receivable_snapshot(opening), "operation_reason": reason}
        except OpeningReceivable.DoesNotExist:
            messages.error(request, "期初应收不存在")
            return redirect("finance:opening_receivable_list")
        record_audit_log_from_request(
            request,
            "opening_receivable_void",
            "opening_receivable",
            opening.id,
            opening.opening_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "期初应收已作废")
        return redirect("finance:opening_receivable_detail", pk=pk)


class OpeningPayableListView(ErpListView):
    model = OpeningPayable
    page_title = "期初应付"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    create_url_name = "finance:opening_payable_create"
    create_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS)
    detail_url_name = "finance:opening_payable_detail"
    columns = (
        ("期初单号", "opening_no"),
        ("供应商", "supplier.supplier_name"),
        ("来源单号", "source_doc_no"),
        ("建账日期", "opening_date"),
        ("期初金额", "opening_amount"),
        ("未付金额", "remaining_amount"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("opening_amount", "remaining_amount")
    ordering = ["-opening_date", "-id"]
    search_fields = ("opening_no", "source_doc_no", "supplier__supplier_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "期初单号", "param": "opening_no", "field": "opening_no", "placeholder": "期初单号"},
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
        {"label": "来源单号", "param": "source_doc_no", "field": "source_doc_no", "placeholder": "来源单号"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class OpeningPayableCreateView(LoginRequiredMixin, View):
    template_name = "finance/opening_payable_form.html"

    def get(self, request):
        _require_finance_payment_process(request.user)
        return render(request, self.template_name, {"page_title": "新建期初应付", "form": OpeningPayableForm()})

    def post(self, request):
        _require_finance_payment_process(request.user)
        form = OpeningPayableForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"page_title": "新建期初应付", "form": form})
        opening = form.save(commit=False)
        opening.opening_no = next_document_no("OP")
        opening.remaining_amount = opening.opening_amount
        opening.status = OpeningPayable.Status.OPEN
        opening.created_by = request.user
        opening.save()
        record_audit_log_from_request(
            request,
            "opening_payable_create",
            "opening_payable",
            opening.id,
            opening.opening_no,
            after_snapshot=_opening_payable_snapshot(opening),
        )
        messages.success(request, "期初应付已创建")
        return redirect("finance:opening_payable_detail", pk=opening.pk)


class OpeningPayableDetailView(LoginRequiredMixin, DetailView):
    model = OpeningPayable
    template_name = "finance/opening_payable_detail.html"
    context_object_name = "opening"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "created_by").prefetch_related("supplierpaymentallocation_set__supplier_payment")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"期初应付 {self.object.opening_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["can_void"] = context["can_process_payment"] and self.object.status in [
            OpeningPayable.Status.OPEN,
            OpeningPayable.Status.PART_SETTLED,
        ]
        return context


class OpeningPayableVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:opening_payable_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:opening_payable_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写期初应付作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                opening = OpeningPayable.objects.select_for_update().get(pk=pk)
                if SupplierPaymentAllocation.objects.select_for_update().filter(opening_payable=opening, allocated_amount__gt=0).exists():
                    messages.error(request, "已有付款核销的期初应付不能作废，请先红冲对应付款")
                    return redirect("finance:opening_payable_detail", pk=pk)
                before_snapshot = _opening_payable_snapshot(opening)
                opening.status = OpeningPayable.Status.VOIDED
                opening.remaining_amount = ZERO_AMOUNT
                opening.save(update_fields=["status", "remaining_amount"])
                after_snapshot = {**_opening_payable_snapshot(opening), "operation_reason": reason}
        except OpeningPayable.DoesNotExist:
            messages.error(request, "期初应付不存在")
            return redirect("finance:opening_payable_list")
        record_audit_log_from_request(
            request,
            "opening_payable_void",
            "opening_payable",
            opening.id,
            opening.opening_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "期初应付已作废")
        return redirect("finance:opening_payable_detail", pk=pk)


class ExpenseRecordListView(ErpListView):
    model = ExpenseRecord
    page_title = "管理费用"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    create_url_name = "finance:expense_record_create"
    create_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS)
    detail_url_name = "finance:expense_record_detail"
    columns = (
        ("费用单号", "expense_no"),
        ("日期", "expense_date"),
        ("类别", "get_category_display"),
        ("金额", "amount"),
        ("收款方", "payee"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("amount",)
    ordering = ["-expense_date", "-id"]
    search_fields = ("expense_no", "payee", "invoice_no", "remark")
    status_filter_field = "status"
    filter_fields = (("类别", "category", ExpenseRecord.ExpenseCategory.choices),)
    field_filters = (
        {"label": "费用单号", "param": "expense_no", "field": "expense_no", "placeholder": "费用单号"},
        {"label": "收款方", "param": "payee", "field": "payee", "placeholder": "收款方"},
        {"label": "发票号", "param": "invoice_no", "field": "invoice_no", "placeholder": "发票号"},
        {
            "label": "付款方式",
            "param": "payment_method",
            "field": "payment_method",
            "lookup": "exact",
            "type": "select",
            "choices": ExpenseRecord.PaymentMethod.choices,
        },
        {"label": "经办人", "param": "handled_by", "field": "handled_by__username", "placeholder": "经办人账号"},
    )

    def get_queryset(self):
        return super().get_queryset().select_related("handled_by", "created_by", "confirmed_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class ExpenseRecordCreateView(LoginRequiredMixin, View):
    template_name = "finance/expense_record_form.html"

    def get(self, request):
        _require_finance_payment_process(request.user)
        return render(request, self.template_name, {"page_title": "新建管理费用", "form": ExpenseRecordForm()})

    def post(self, request):
        _require_finance_payment_process(request.user)
        form = ExpenseRecordForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"page_title": "新建管理费用", "form": form})
        expense = form.save(commit=False)
        expense.expense_no = next_document_no("EX")
        expense.status = ExpenseRecord.Status.DRAFT
        expense.handled_by = request.user
        expense.created_by = request.user
        expense.save()
        record_audit_log_from_request(
            request,
            "expense_record_create",
            "expense_record",
            expense.id,
            expense.expense_no,
            after_snapshot=_expense_record_snapshot(expense),
        )
        messages.success(request, "管理费用已创建")
        return redirect("finance:expense_record_detail", pk=expense.pk)


class ExpenseRecordDetailView(LoginRequiredMixin, DetailView):
    model = ExpenseRecord
    template_name = "finance/expense_record_detail.html"
    context_object_name = "expense"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("handled_by", "created_by", "confirmed_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"管理费用 {self.object.expense_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["can_confirm"] = context["can_process_payment"] and self.object.status == ExpenseRecord.Status.DRAFT
        context["can_void"] = context["can_process_payment"] and self.object.status in [
            ExpenseRecord.Status.DRAFT,
            ExpenseRecord.Status.CONFIRMED,
        ]
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "expense_record",
            self.object.id,
            self.object.expense_no,
        )
        return context


class ExpenseRecordConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:expense_record_detail", pk)
        if verification_response:
            return verification_response
        try:
            with transaction.atomic():
                expense = ExpenseRecord.objects.select_for_update().get(pk=pk)
                if expense.status != ExpenseRecord.Status.DRAFT:
                    messages.error(request, "只有草稿费用单可以确认")
                    return redirect("finance:expense_record_detail", pk=pk)
                before_snapshot = _expense_record_snapshot(expense)
                expense.status = ExpenseRecord.Status.CONFIRMED
                expense.confirmed_at = timezone.now()
                expense.confirmed_by = request.user
                expense.save(update_fields=["status", "confirmed_at", "confirmed_by"])
                after_snapshot = _expense_record_snapshot(expense)
        except ExpenseRecord.DoesNotExist:
            messages.error(request, "管理费用不存在")
            return redirect("finance:expense_record_list")
        record_audit_log_from_request(
            request,
            "expense_record_confirm",
            "expense_record",
            expense.id,
            expense.expense_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "管理费用已确认")
        return redirect("finance:expense_record_detail", pk=pk)


class ExpenseRecordVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:expense_record_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:expense_record_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写管理费用作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                expense = ExpenseRecord.objects.select_for_update().get(pk=pk)
                if expense.status == ExpenseRecord.Status.VOIDED:
                    messages.error(request, "费用单已作废")
                    return redirect("finance:expense_record_detail", pk=pk)
                before_snapshot = _expense_record_snapshot(expense)
                expense.status = ExpenseRecord.Status.VOIDED
                expense.save(update_fields=["status"])
                after_snapshot = {**_expense_record_snapshot(expense), "operation_reason": reason}
        except ExpenseRecord.DoesNotExist:
            messages.error(request, "管理费用不存在")
            return redirect("finance:expense_record_list")
        record_audit_log_from_request(
            request,
            "expense_record_void",
            "expense_record",
            expense.id,
            expense.expense_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "管理费用已作废")
        return redirect("finance:expense_record_detail", pk=pk)


class CustomerCreditBalanceListView(ErpListView):
    model = CustomerCreditBalance
    page_title = "客户待处理余额"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    detail_url_name = "finance:customer_credit_balance_detail"
    columns = (
        ("客户", "customer.customer_name"),
        ("来源单号", "source_doc_no"),
        ("余额", "remaining_amount"),
        ("状态", "get_status_display"),
        ("创建时间", "created_at"),
    )
    sensitive_columns = ("remaining_amount",)
    ordering = ["-created_at"]
    page_actions = (("导出CSV", "finance:customer_credit_balance_export", ""),)
    search_fields = ("source_doc_no", "customer__customer_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "客户", "param": "customer_name", "field": "customer__customer_name", "placeholder": "客户名称"},
        {"label": "来源单号", "param": "source_doc_no", "field": "source_doc_no", "placeholder": "来源单号"},
        {"label": "来源类型", "param": "source_doc_type", "field": "source_doc_type", "placeholder": "来源类型"},
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class CustomerCreditBalanceExportView(FinanceCsvExportView):
    module = "customer_credit_balances"
    list_view_class = CustomerCreditBalanceListView
    ordering = ("-created_at",)
    select_related = ("customer",)


class CustomerCreditBalanceDetailView(LoginRequiredMixin, DetailView):
    model = CustomerCreditBalance
    template_name = "finance/customer_credit_balance_detail.html"
    context_object_name = "balance"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "created_by").prefetch_related("transactions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"客户余额 {self.object.source_doc_no or self.object.pk}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["action_choices"] = CustomerCreditBalanceTransaction.ActionType.choices
        context["can_apply"] = context["can_view_amount"] and context["can_process_payment"] and self.object.status not in [
            CustomerCreditBalance.Status.USED_UP,
            CustomerCreditBalance.Status.CLOSED,
        ]
        context["target_sales_orders"] = _customer_credit_balance_target_orders(self.object)
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "customer_credit_balance",
            self.object.id,
            self.object.source_doc_no,
        )
        return context


class CustomerCreditBalancePrintView(LoginRequiredMixin, DetailView):
    model = CustomerCreditBalance
    template_name = "finance/customer_credit_balance_print.html"
    context_object_name = "balance"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "created_by").prefetch_related("transactions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印客户余额 {self.object.source_doc_no or self.object.pk}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        record_print_log(
            template_type="customer_credit_balance",
            source_doc_type="customer_credit_balance",
            source_doc_id=self.object.id,
            source_doc_no=self.object.source_doc_no,
            printed_by_id=self.request.user.id,
        )
        return context


class CustomerCreditBalanceApplyView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_credit_balance_detail", pk)
        if verification_response:
            return verification_response
        amount = _decimal_from_post(request, "amount")
        if amount is None:
            messages.error(request, "处理金额格式不正确")
            return redirect("finance:customer_credit_balance_detail", pk=pk)
        action_type = request.POST.get("action_type", "")
        reason = request.POST.get("reason", "").strip()
        target_sales_order_id = request.POST.get("target_sales_order_id") or None
        result = apply_customer_credit_balance(
            pk,
            action_type,
            amount,
            request.user.id,
            target_sales_order_id=int(target_sales_order_id) if target_sales_order_id else None,
            reason=reason,
            idempotency_key=f"customer-balance:{pk}:{action_type}:{amount}:{target_sales_order_id or ''}:{reason}",
        )
        if result.success:
            record_audit_log_from_request(
                request,
                "customer_credit_balance_apply",
                "customer_credit_balance",
                pk,
                after_snapshot={"action_type": action_type, "amount": str(amount), "reason": reason, **result.data},
            )
        _flash_result(request, result, "客户余额处理失败")
        return redirect("finance:customer_credit_balance_detail", pk=pk)


class SupplierCreditBalanceListView(ErpListView):
    model = SupplierCreditBalance
    page_title = "供应商待处理余额"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    detail_url_name = "finance:supplier_credit_balance_detail"
    columns = (
        ("供应商", "supplier.supplier_name"),
        ("来源单号", "source_doc_no"),
        ("余额", "remaining_amount"),
        ("状态", "get_status_display"),
        ("创建时间", "created_at"),
    )
    sensitive_columns = ("remaining_amount",)
    ordering = ["-created_at"]
    page_actions = (("导出CSV", "finance:supplier_credit_balance_export", ""),)
    search_fields = ("source_doc_no", "supplier__supplier_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
        {"label": "来源单号", "param": "source_doc_no", "field": "source_doc_no", "placeholder": "来源单号"},
        {"label": "来源类型", "param": "source_doc_type", "field": "source_doc_type", "placeholder": "来源类型"},
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class SupplierCreditBalanceExportView(FinanceCsvExportView):
    module = "supplier_credit_balances"
    list_view_class = SupplierCreditBalanceListView
    ordering = ("-created_at",)
    select_related = ("supplier",)


class SupplierCreditBalanceDetailView(LoginRequiredMixin, DetailView):
    model = SupplierCreditBalance
    template_name = "finance/supplier_credit_balance_detail.html"
    context_object_name = "balance"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "created_by").prefetch_related("transactions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"供应商余额 {self.object.source_doc_no or self.object.pk}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["action_choices"] = SupplierCreditBalanceTransaction.ActionType.choices
        context["can_apply"] = context["can_view_amount"] and context["can_process_payment"] and self.object.status not in [
            SupplierCreditBalance.Status.USED_UP,
            SupplierCreditBalance.Status.CLOSED,
        ]
        context["target_purchase_receipts"] = _supplier_credit_balance_target_receipts(self.object)
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "supplier_credit_balance",
            self.object.id,
            self.object.source_doc_no,
        )
        return context


class SupplierCreditBalancePrintView(LoginRequiredMixin, DetailView):
    model = SupplierCreditBalance
    template_name = "finance/supplier_credit_balance_print.html"
    context_object_name = "balance"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "created_by").prefetch_related("transactions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印供应商余额 {self.object.source_doc_no or self.object.pk}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        record_print_log(
            template_type="supplier_credit_balance",
            source_doc_type="supplier_credit_balance",
            source_doc_id=self.object.id,
            source_doc_no=self.object.source_doc_no,
            printed_by_id=self.request.user.id,
        )
        return context


class SupplierCreditBalanceApplyView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:supplier_credit_balance_detail", pk)
        if verification_response:
            return verification_response
        amount = _decimal_from_post(request, "amount")
        if amount is None:
            messages.error(request, "处理金额格式不正确")
            return redirect("finance:supplier_credit_balance_detail", pk=pk)
        action_type = request.POST.get("action_type", "")
        reason = request.POST.get("reason", "").strip()
        target_purchase_receipt_id = request.POST.get("target_purchase_receipt_id") or None
        result = apply_supplier_credit_balance(
            pk,
            action_type,
            amount,
            request.user.id,
            target_purchase_receipt_id=int(target_purchase_receipt_id) if target_purchase_receipt_id else None,
            reason=reason,
            idempotency_key=f"supplier-balance:{pk}:{action_type}:{amount}:{target_purchase_receipt_id or ''}:{reason}",
        )
        if result.success:
            record_audit_log_from_request(
                request,
                "supplier_credit_balance_apply",
                "supplier_credit_balance",
                pk,
                after_snapshot={"action_type": action_type, "amount": str(amount), "reason": reason, **result.data},
            )
        _flash_result(request, result, "供应商余额处理失败")
        return redirect("finance:supplier_credit_balance_detail", pk=pk)


class CustomerInvoiceListView(ErpListView):
    model = CustomerInvoice
    page_title = "客户开票"
    view_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.SALES_PROCESS)
    permission_denied_message = "缺少客户开票查看权限"
    create_url_name = "finance:customer_invoice_create"
    detail_url_name = "finance:customer_invoice_detail"
    columns = (
        ("开票单号", "invoice_no"),
        ("发票号码", "external_invoice_no"),
        ("客户", "customer.customer_name"),
        ("关联对账单", "reconciliation.reconciliation_no"),
        ("开票日期", "invoice_date"),
        ("开票金额", "invoice_amount"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("invoice_amount",)
    ordering = ["-invoice_date", "-id"]
    search_fields = ("invoice_no", "external_invoice_no", "customer__customer_name", "reconciliation__reconciliation_no")
    status_filter_field = "status"
    field_filters = (
        {"label": "开票单号", "param": "invoice_no", "field": "invoice_no", "placeholder": "开票单号"},
        {"label": "发票号码", "param": "external_invoice_no", "field": "external_invoice_no", "placeholder": "发票号码"},
        {"label": "客户", "param": "customer_name", "field": "customer__customer_name", "placeholder": "客户名称"},
        {"label": "对账单", "param": "reconciliation_no", "field": "reconciliation__reconciliation_no", "placeholder": "对账单号"},
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_customer_invoice_amount(self.request.user)
        return context

    def get_queryset(self):
        return _filter_customer_invoice_queryset_for_user(
            super().get_queryset().select_related("customer", "reconciliation"),
            self.request.user,
        )

    def get_create_url_name(self) -> str:
        return self.create_url_name if _can_process_customer_invoice(self.request.user) else ""

    def get_scope_filter_options(self):
        if _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.SALES_VIEW_ALL):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            return queryset.filter(
                Q(customer__sales_owner=self.request.user)
                | Q(customer__created_by=self.request.user)
                | Q(customer__sales_orders__created_by=self.request.user)
                | Q(created_by=self.request.user)
            ).distinct()
        if scope_value == "unassigned" and (
            _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.SALES_VIEW_ALL)
        ):
            return queryset.filter(customer__sales_owner__isnull=True).distinct()
        return queryset


class CustomerInvoiceCreateView(LoginRequiredMixin, View):
    template_name = "finance/customer_invoice_form.html"

    def get(self, request):
        _require_customer_invoice_process(request.user)
        return self._render(request)

    def post(self, request):
        _require_customer_invoice_process(request.user)
        customer_id = _int_or_none(request.POST.get("customer"))
        reconciliation_id = _int_or_none(request.POST.get("reconciliation"))
        invoice_date = _date_from_post(request, "invoice_date")
        external_invoice_no = request.POST.get("external_invoice_no", "").strip()
        remark = request.POST.get("remark", "").strip()
        rows = _customer_invoice_item_rows_from_post(request)

        error_message = _validate_customer_invoice_input(
            request.user,
            customer_id,
            reconciliation_id,
            invoice_date,
            rows,
        )
        if error_message:
            messages.error(request, error_message)
            return self._render(request)

        invoice_amount = _money(sum((row["invoice_amount"] for row in rows), ZERO_AMOUNT))
        with transaction.atomic():
            invoice = CustomerInvoice.objects.create(
                invoice_no=next_document_no("INV"),
                external_invoice_no=external_invoice_no,
                customer_id=customer_id,
                reconciliation_id=reconciliation_id,
                invoice_date=invoice_date,
                invoice_amount=invoice_amount,
                status=CustomerInvoice.Status.DRAFT,
                created_by=request.user,
                remark=remark,
            )
            CustomerInvoiceItem.objects.bulk_create(
                [
                    CustomerInvoiceItem(
                        customer_invoice=invoice,
                        reconciliation_item_id=row.get("reconciliation_item_id"),
                        sales_order_id=row["sales_order_id"],
                        line_no=index,
                        invoice_amount=row["invoice_amount"],
                    )
                    for index, row in enumerate(rows, start=1)
                ]
            )
        record_audit_log_from_request(
            request,
            "customer_invoice_create",
            "customer_invoice",
            invoice.id,
            invoice.invoice_no,
            after_snapshot=_customer_invoice_snapshot(invoice),
        )
        messages.success(request, "开票单已保存。请在详情页上传发票附件后再确认开票。")
        return redirect("finance:customer_invoice_detail", pk=invoice.pk)

    def _render(self, request):
        customer_id = _int_or_none(request.POST.get("customer") or request.GET.get("customer"))
        reconciliation_id = _int_or_none(request.POST.get("reconciliation") or request.GET.get("reconciliation"))
        if reconciliation_id:
            reconciliation = _customer_invoice_reconciliation_queryset(request.user).filter(pk=reconciliation_id).first()
            if reconciliation:
                customer_id = reconciliation.customer_id
        rows = _customer_invoice_candidate_rows(request.user, customer_id, reconciliation_id)
        return render(
            request,
            self.template_name,
            {
                "page_title": "新建客户开票",
                "customers": _customer_receipt_customer_queryset(request.user).order_by("customer_no"),
                "reconciliations": _customer_invoice_reconciliation_queryset(request.user, customer_id).order_by("-period_end", "-id"),
                "selected_customer_id": customer_id,
                "selected_reconciliation_id": reconciliation_id,
                "today": timezone.localdate(),
                "rows": rows,
            },
        )


class CustomerInvoiceDetailView(LoginRequiredMixin, DetailView):
    model = CustomerInvoice
    template_name = "finance/customer_invoice_detail.html"
    context_object_name = "invoice"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_customer_invoice_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("customer", "reconciliation", "created_by", "confirmed_by")
            .prefetch_related("items__sales_order", "items__reconciliation_item")
        )
        return _filter_customer_invoice_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"客户开票 {self.object.invoice_no}"
        context["can_view_amount"] = _can_view_customer_invoice_amount(self.request.user)
        context["can_process_invoice"] = _can_process_customer_invoice(self.request.user)
        context["has_invoice_attachment"] = _customer_invoice_has_active_attachment(self.object)
        context["can_confirm"] = (
            context["can_process_invoice"]
            and self.object.status == CustomerInvoice.Status.DRAFT
        )
        context["can_void"] = (
            context["can_process_invoice"]
            and self.object.status in [CustomerInvoice.Status.DRAFT, CustomerInvoice.Status.CONFIRMED]
        )
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "customer_invoice",
            self.object.id,
            self.object.invoice_no,
        )
        return context


class CustomerInvoiceConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_customer_invoice_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_invoice_detail", pk)
        if verification_response:
            return verification_response
        try:
            with transaction.atomic():
                invoice = _filter_customer_invoice_queryset_for_user(
                    CustomerInvoice.objects.select_for_update().prefetch_related("items"),
                    request.user,
                ).get(pk=pk)
                if invoice.status != CustomerInvoice.Status.DRAFT:
                    messages.error(request, "只有草稿开票单可以确认")
                    return redirect("finance:customer_invoice_detail", pk=pk)
                before_snapshot = _customer_invoice_snapshot(invoice)
                error_message = _validate_customer_invoice_confirm(invoice)
                if error_message:
                    messages.error(request, error_message)
                    return redirect("finance:customer_invoice_detail", pk=pk)
                invoice.status = CustomerInvoice.Status.CONFIRMED
                invoice.confirmed_at = timezone.now()
                invoice.confirmed_by = request.user
                invoice.save(update_fields=["status", "confirmed_at", "confirmed_by"])
                after_snapshot = _customer_invoice_snapshot(invoice)
        except CustomerInvoice.DoesNotExist:
            messages.error(request, "开票单不存在")
            return redirect("finance:customer_invoice_list")

        record_audit_log_from_request(
            request,
            "customer_invoice_confirm",
            "customer_invoice",
            invoice.id,
            invoice.invoice_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "开票单已确认")
        return redirect("finance:customer_invoice_detail", pk=pk)


class CustomerInvoiceVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_customer_invoice_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_invoice_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:customer_invoice_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写开票单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                invoice = _filter_customer_invoice_queryset_for_user(
                    CustomerInvoice.objects.select_for_update(),
                    request.user,
                ).get(pk=pk)
                if invoice.status not in [CustomerInvoice.Status.DRAFT, CustomerInvoice.Status.CONFIRMED]:
                    messages.error(request, "当前开票单状态不能作废")
                    return redirect("finance:customer_invoice_detail", pk=pk)
                before_snapshot = _customer_invoice_snapshot(invoice)
                invoice.status = CustomerInvoice.Status.VOIDED
                invoice.save(update_fields=["status"])
                after_snapshot = _customer_invoice_snapshot(invoice)
        except CustomerInvoice.DoesNotExist:
            messages.error(request, "开票单不存在")
            return redirect("finance:customer_invoice_list")

        record_audit_log_from_request(
            request,
            "customer_invoice_void",
            "customer_invoice",
            invoice.id,
            invoice.invoice_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "开票单已作废")
        return redirect("finance:customer_invoice_detail", pk=pk)


class ReconciliationListView(ErpListView):
    model = Reconciliation
    page_title = "对账单"
    view_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.SALES_PROCESS, PermissionCode.PURCHASE_PROCESS)
    permission_denied_message = "缺少对账单查看权限"
    create_url_name = "finance:reconciliation_create"
    detail_url_name = "finance:reconciliation_detail"
    columns = (
        ("对账单号", "reconciliation_no"),
        ("对象类型", "get_party_type_display"),
        ("客户", "customer.customer_name"),
        ("供应商", "supplier.supplier_name"),
        ("开始日期", "period_start"),
        ("结束日期", "period_end"),
        ("金额", "total_amount"),
        ("状态", "get_status_display"),
    )
    sensitive_columns = ("total_amount",)
    ordering = ["-period_start", "-id"]
    page_actions = (("导出CSV", "finance:reconciliation_export", ""),)
    search_fields = ("reconciliation_no", "customer__customer_name", "supplier__supplier_name")
    status_filter_field = "status"
    field_filters = (
        {"label": "对账单号", "param": "reconciliation_no", "field": "reconciliation_no", "placeholder": "对账单号"},
        {
            "label": "对象类型",
            "param": "party_type",
            "field": "party_type",
            "lookup": "exact",
            "type": "select",
            "choices": Reconciliation.PartyType.choices,
        },
        {"label": "客户", "param": "customer_name", "field": "customer__customer_name", "placeholder": "客户名称"},
        {"label": "供应商", "param": "supplier_name", "field": "supplier__supplier_name", "placeholder": "供应商名称"},
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_reconciliation_amount(self.request.user)
        return context

    def get_queryset(self):
        return _filter_reconciliation_queryset_for_user(super().get_queryset(), self.request.user)

    def get_create_url_name(self) -> str:
        return self.create_url_name if _can_create_reconciliation(self.request.user) else ""

    def get_scope_filter_options(self):
        if _can_view_finance_amount(self.request.user) or user_has_permission(self.request.user, PermissionCode.SALES_VIEW_ALL) or user_has_permission(self.request.user, PermissionCode.PURCHASE_VIEW):
            return (
                {"value": "all", "label": "全部", "default": True},
                {"value": "mine", "label": "我的"},
                {"value": "unassigned", "label": "未分配"},
            )
        return ({"value": "mine", "label": "我的", "default": True},)

    def apply_scope_filter(self, queryset, scope_value: str):
        if scope_value == "mine":
            conditions = (
                Q(customer__sales_owner=self.request.user)
                | Q(customer__created_by=self.request.user)
                | Q(supplier__created_by=self.request.user)
                | Q(created_by=self.request.user)
            )
            if user_has_permission(self.request.user, PermissionCode.PURCHASE_PROCESS):
                conditions |= Q(supplier__in=_supplier_payment_supplier_queryset(self.request.user))
            return queryset.filter(conditions).distinct()
        if scope_value == "unassigned" and (
            _can_view_finance_amount(self.request.user)
            or user_has_permission(self.request.user, PermissionCode.SALES_VIEW_ALL)
            or user_has_permission(self.request.user, PermissionCode.PURCHASE_VIEW)
        ):
            return queryset.filter(
                Q(party_type=Reconciliation.PartyType.CUSTOMER, customer__sales_owner__isnull=True)
                | Q(party_type=Reconciliation.PartyType.SUPPLIER, created_by__isnull=True)
            ).distinct()
        return queryset


class ReconciliationExportView(FinanceCsvExportView):
    module = "reconciliations"
    list_view_class = ReconciliationListView
    ordering = ("-period_start", "-id")
    select_related = ("customer", "supplier")

    def get_mask_fields(self):
        return () if _can_view_reconciliation_amount(self.request.user) else self.list_view_class.sensitive_columns


class ReconciliationCreateView(LoginRequiredMixin, View):
    template_name = "finance/reconciliation_form.html"

    def get(self, request):
        _require_reconciliation_create(request.user)
        return self._render(request)

    def post(self, request):
        _require_reconciliation_create(request.user)
        party_type = request.POST.get("party_type", "")
        period_start = _date_from_post(request, "period_start")
        period_end = _date_from_post(request, "period_end")
        customer_id = request.POST.get("customer") or None
        supplier_id = request.POST.get("supplier") or None

        error_message = _validate_reconciliation_input(party_type, period_start, period_end, customer_id, supplier_id, request.user)
        if error_message:
            messages.error(request, error_message)
            return self._render(request)

        rows = _reconciliation_rows(party_type, customer_id, supplier_id, period_start, period_end, user=request.user)
        total_amount = _rows_total(rows)
        if not rows:
            messages.error(request, "所选对象和日期范围内没有可对账明细")
            return self._render(request)

        reconciliation = Reconciliation.objects.create(
            reconciliation_no=next_document_no("REC"),
            party_type=party_type,
            customer_id=customer_id if party_type == Reconciliation.PartyType.CUSTOMER else None,
            supplier_id=supplier_id if party_type == Reconciliation.PartyType.SUPPLIER else None,
            period_start=period_start,
            period_end=period_end,
            total_amount=total_amount,
            status=Reconciliation.Status.DRAFT,
            created_by=request.user,
            remark=request.POST.get("remark", "").strip(),
        )
        record_audit_log_from_request(
            request,
            "reconciliation_create",
            "reconciliation",
            reconciliation.id,
            reconciliation.reconciliation_no,
            after_snapshot=_reconciliation_snapshot(reconciliation),
        )
        messages.success(request, "对账单已创建")
        return redirect("finance:reconciliation_detail", pk=reconciliation.pk)

    def _render(self, request):
        return render(
            request,
            self.template_name,
            {
                "page_title": "新建对账单",
                "party_type_choices": _reconciliation_party_type_choices(request.user),
                "customers": _customer_receipt_customer_queryset(request.user).order_by("customer_no"),
                "suppliers": (
                    _supplier_payment_supplier_queryset(request.user).order_by("supplier_no")
                    if _can_create_supplier_reconciliation(request.user)
                    else Supplier.objects.none()
                ),
                "today": timezone.localdate(),
            },
        )


class ReconciliationDetailView(LoginRequiredMixin, DetailView):
    model = Reconciliation
    template_name = "finance/reconciliation_detail.html"
    context_object_name = "reconciliation"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_reconciliation_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = super().get_queryset().select_related("customer", "supplier", "created_by")
        return _filter_reconciliation_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"对账单 {self.object.reconciliation_no}"
        context["can_view_amount"] = _can_view_reconciliation_amount(self.request.user)
        context["can_process_payment"] = _can_process_reconciliation(self.request.user, self.object)
        context["can_create_customer_invoice"] = (
            self.object.party_type == Reconciliation.PartyType.CUSTOMER
            and _can_process_customer_invoice(self.request.user)
            and self.object.status != Reconciliation.Status.VOIDED
        )
        context["can_confirm"] = (
            context["can_view_amount"]
            and context["can_process_payment"]
            and self.object.status == Reconciliation.Status.DRAFT
        )
        context["can_void"] = (
            context["can_view_amount"]
            and context["can_process_payment"]
            and self.object.status in [Reconciliation.Status.DRAFT, Reconciliation.Status.CONFIRMED]
        )
        rows = _display_reconciliation_rows(self.object, self.request.user)
        if self.object.party_type == Reconciliation.PartyType.CUSTOMER:
            rows, invoice_summary = _decorate_customer_reconciliation_invoice_rows(rows)
        else:
            invoice_summary = {"total_amount": ZERO_AMOUNT, "invoiced_amount": ZERO_AMOUNT, "uninvoiced_amount": ZERO_AMOUNT}
        context["rows"] = rows
        context["invoice_summary"] = invoice_summary
        context["current_total"] = _rows_total(context["rows"])
        context["attachment_panel"] = build_attachment_panel(
            self.request.user,
            "reconciliation",
            self.object.id,
            self.object.reconciliation_no,
        )
        return context


class ReconciliationPrintView(LoginRequiredMixin, DetailView):
    model = Reconciliation
    template_name = "finance/reconciliation_print.html"
    context_object_name = "reconciliation"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_reconciliation_view(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = super().get_queryset().select_related("customer", "supplier", "created_by")
        return _filter_reconciliation_queryset_for_user(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = _display_reconciliation_rows(self.object, self.request.user)
        if self.object.party_type == Reconciliation.PartyType.CUSTOMER:
            rows, invoice_summary = _decorate_customer_reconciliation_invoice_rows(rows)
        else:
            invoice_summary = {"total_amount": ZERO_AMOUNT, "invoiced_amount": ZERO_AMOUNT, "uninvoiced_amount": ZERO_AMOUNT}
        context["page_title"] = f"打印对账单 {self.object.reconciliation_no}"
        context["rows"] = rows
        context["invoice_summary"] = invoice_summary
        context["current_total"] = _rows_total(rows)
        context["statement_rows"] = _reconciliation_statement_rows(self.object, self.request.user)
        context["statement_total"] = _statement_rows_total(context["statement_rows"])
        context["prior_balance"] = _reconciliation_prior_balance(self.object, self.request.user)
        context["ending_balance"] = _money(context["prior_balance"] + context["current_total"])
        context["party_name"] = _reconciliation_party_name(self.object)
        context["statement_date"] = timezone.localdate()
        record_print_log(
            template_type="reconciliation",
            source_doc_type="reconciliation",
            source_doc_id=self.object.id,
            source_doc_no=self.object.reconciliation_no,
            printed_by_id=self.request.user.id,
        )
        return context


class ReconciliationConfirmView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_reconciliation_view(request.user)
        verification_response = require_second_verify(request, "finance:reconciliation_detail", pk)
        if verification_response:
            return verification_response
        try:
            with transaction.atomic():
                reconciliation = _filter_reconciliation_queryset_for_user(
                    Reconciliation.objects.select_for_update(), request.user
                ).get(pk=pk)
                if not _can_process_reconciliation(request.user, reconciliation):
                    raise PermissionDenied("缺少对账单处理权限")
                if reconciliation.status != Reconciliation.Status.DRAFT:
                    messages.error(request, "只有草稿对账单可以确认")
                    return redirect("finance:reconciliation_detail", pk=pk)
                before_snapshot = _reconciliation_snapshot(reconciliation)
                rows = _reconciliation_rows(
                    reconciliation.party_type,
                    reconciliation.customer_id,
                    reconciliation.supplier_id,
                    reconciliation.period_start,
                    reconciliation.period_end,
                    for_update=True,
                    user=request.user,
                )
                if not rows:
                    messages.error(request, "当前没有可确认的对账明细")
                    return redirect("finance:reconciliation_detail", pk=pk)
                _replace_reconciliation_items(reconciliation, rows)
                reconciliation.total_amount = _rows_total(rows)
                reconciliation.status = Reconciliation.Status.CONFIRMED
                reconciliation.save(update_fields=["total_amount", "status"])
                after_snapshot = _reconciliation_snapshot(reconciliation)
        except Reconciliation.DoesNotExist:
            messages.error(request, "对账单不存在")
            return redirect("finance:reconciliation_list")

        record_audit_log_from_request(
            request,
            "reconciliation_confirm",
            "reconciliation",
            reconciliation.id,
            reconciliation.reconciliation_no,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        messages.success(request, "对账单已确认")
        return redirect("finance:reconciliation_detail", pk=pk)


class ReconciliationVoidView(LoginRequiredMixin, View):
    def post(self, request, pk):
        _require_reconciliation_view(request.user)
        verification_response = require_second_verify(request, "finance:reconciliation_detail", pk)
        if verification_response:
            return verification_response
        reason, reason_response = require_post_reason(
            request,
            "finance:reconciliation_detail",
            pk,
            field_names=("void_reason", "reason"),
            message="请填写对账单作废原因",
        )
        if reason_response:
            return reason_response
        try:
            with transaction.atomic():
                reconciliation = _filter_reconciliation_queryset_for_user(
                    Reconciliation.objects.select_for_update(), request.user
                ).get(pk=pk)
                if not _can_process_reconciliation(request.user, reconciliation):
                    raise PermissionDenied("缺少对账单处理权限")
                if reconciliation.status not in [Reconciliation.Status.DRAFT, Reconciliation.Status.CONFIRMED]:
                    messages.error(request, "当前对账单状态不能作废")
                    return redirect("finance:reconciliation_detail", pk=pk)
                if _reconciliation_has_allocations(reconciliation):
                    messages.error(request, "已有核销明细的对账单不能作废")
                    return redirect("finance:reconciliation_detail", pk=pk)
                before_snapshot = _reconciliation_snapshot(reconciliation)
                reconciliation.status = Reconciliation.Status.VOIDED
                reconciliation.save(update_fields=["status"])
                after_snapshot = _reconciliation_snapshot(reconciliation)
        except Reconciliation.DoesNotExist:
            messages.error(request, "对账单不存在")
            return redirect("finance:reconciliation_list")

        record_audit_log_from_request(
            request,
            "reconciliation_void",
            "reconciliation",
            reconciliation.id,
            reconciliation.reconciliation_no,
            before_snapshot=before_snapshot,
            after_snapshot={**after_snapshot, "operation_reason": reason},
        )
        messages.success(request, "对账单已作废")
        return redirect("finance:reconciliation_detail", pk=pk)


def _decimal_from_post(request, field_name: str):
    try:
        return Decimal(request.POST.get(field_name, "0"))
    except (InvalidOperation, TypeError):
        return None


def _date_from_post(request, field_name: str) -> date | None:
    return parse_user_date(request.POST.get(field_name, ""))


def _allocation_rows_from_post(request, target_key: str, amount_key: str = "allocated_amount") -> list[dict]:
    target_ids = request.POST.getlist(target_key)
    amounts = request.POST.getlist(amount_key)
    if not amounts and amount_key != "allocated_amount":
        amounts = request.POST.getlist("allocated_amount")
    rows = []
    for target_id, amount_value in zip(target_ids, amounts):
        if not target_id or not amount_value:
            continue
        try:
            amount = Decimal(amount_value)
        except InvalidOperation:
            continue
        if amount <= 0:
            continue
        rows.append({target_key: int(target_id), "allocated_amount": str(amount)})
    return rows


def _allocation_signature(allocations: list[dict]) -> str:
    parts = []
    for row in sorted(allocations, key=lambda item: sorted(item.items())):
        if row.get("sales_order_id"):
            target = f"sales_order:{row['sales_order_id']}"
        elif row.get("purchase_receipt_id"):
            target = f"purchase_receipt:{row['purchase_receipt_id']}"
        else:
            target = f"reconciliation:{row.get('reconciliation_id')}"
        parts.append(f"{target}:{row['allocated_amount']}")
    return "|".join(parts) or "empty"


def _int_or_none(value):
    try:
        return int(value) if value not in [None, ""] else None
    except (TypeError, ValueError):
        return None


def _customer_invoice_item_rows_from_post(request) -> list[dict]:
    sales_order_ids = request.POST.getlist("sales_order_id")
    reconciliation_item_ids = request.POST.getlist("reconciliation_item_id")
    amounts = request.POST.getlist("invoice_item_amount")
    rows = []
    for index, sales_order_id in enumerate(sales_order_ids):
        amount_value = amounts[index] if index < len(amounts) else ""
        if not sales_order_id or not amount_value:
            continue
        try:
            sales_order_id_value = int(sales_order_id)
        except (TypeError, ValueError):
            continue
        try:
            amount = _money(Decimal(amount_value))
        except (InvalidOperation, TypeError):
            continue
        if amount <= ZERO_AMOUNT:
            continue
        reconciliation_item_id = reconciliation_item_ids[index] if index < len(reconciliation_item_ids) else ""
        try:
            reconciliation_item_id_value = int(reconciliation_item_id) if reconciliation_item_id else None
        except (TypeError, ValueError):
            reconciliation_item_id_value = None
        rows.append(
            {
                "sales_order_id": sales_order_id_value,
                "reconciliation_item_id": reconciliation_item_id_value,
                "invoice_amount": amount,
            }
        )
    return rows


def _validate_customer_invoice_input(user, customer_id, reconciliation_id, invoice_date, rows: list[dict]) -> str:
    if not customer_id:
        return "请选择客户"
    if not _customer_receipt_customer_queryset(user).filter(pk=customer_id).exists():
        return "客户不存在或不属于当前销售范围"
    if not invoice_date:
        return "请选择开票日期"
    if not rows:
        return "请至少填写一行开票金额"
    reconciliation = None
    if reconciliation_id:
        reconciliation = _customer_invoice_reconciliation_queryset(user, customer_id).filter(pk=reconciliation_id).first()
        if not reconciliation:
            return "对账单不存在或不属于当前客户"
    allowed_orders = _filter_customer_sales_order_queryset_for_user(
        SalesOrder.objects.filter(customer_id=customer_id),
        user,
    )
    candidate_rows = _customer_invoice_candidate_rows(user, customer_id, reconciliation_id)
    candidate_by_order = {row["sales_order_id"]: row for row in candidate_rows}
    requested_by_order = {}
    for row in rows:
        order_id = row["sales_order_id"]
        if not allowed_orders.filter(pk=order_id).exists():
            return "开票明细中存在不属于当前客户或当前用户范围的销售订单"
        candidate = candidate_by_order.get(order_id)
        if not candidate:
            return "开票明细中存在不可开票的销售订单"
        if reconciliation and row.get("reconciliation_item_id") != candidate.get("reconciliation_item_id"):
            return "开票明细与对账单明细不一致"
        if row["invoice_amount"] > candidate["available_amount"]:
            return f"{candidate['source_no']} 的开票金额不能超过未开票金额 {candidate['available_amount']}"
        requested_by_order[order_id] = requested_by_order.get(order_id, ZERO_AMOUNT) + row["invoice_amount"]
    for order_id, requested_amount in requested_by_order.items():
        candidate = candidate_by_order[order_id]
        if requested_amount > candidate["available_amount"]:
            return f"{candidate['source_no']} 的开票金额合计不能超过未开票金额 {candidate['available_amount']}"
    return ""


def _customer_invoice_candidate_rows(user, customer_id, reconciliation_id=None) -> list[dict]:
    if not customer_id:
        return []
    if not _customer_receipt_customer_queryset(user).filter(pk=customer_id).exists():
        return []

    if reconciliation_id:
        reconciliation = _customer_invoice_reconciliation_queryset(user, customer_id).filter(pk=reconciliation_id).first()
        if not reconciliation:
            return []
        rows = []
        for row in _display_reconciliation_rows(reconciliation, user):
            if row["source_type"] != ReconciliationItem.SourceType.SALES_ORDER:
                continue
            available_amount = _money(max(row["open_amount"] - _sales_order_invoiced_amount(row["source_doc_id"]), ZERO_AMOUNT))
            if available_amount <= ZERO_AMOUNT:
                continue
            rows.append(
                {
                    "sales_order_id": row["source_doc_id"],
                    "reconciliation_item_id": row.get("reconciliation_item_id"),
                    "source_no": row["source_no"],
                    "source_date": row["source_date"],
                    "total_amount": row["open_amount"],
                    "invoiced_amount": _money(row["open_amount"] - available_amount),
                    "available_amount": available_amount,
                    "suggested_amount": available_amount,
                }
            )
        return rows

    rows = []
    orders = (
        SalesOrder.objects.filter(customer_id=customer_id)
        .exclude(
            status__in=[
                SalesOrder.Status.DRAFT,
                SalesOrder.Status.PENDING_APPROVAL,
                SalesOrder.Status.REJECTED,
                SalesOrder.Status.VOIDED,
            ]
        )
        .order_by("-order_date", "-id")
    )
    orders = _filter_customer_sales_order_queryset_for_user(orders, user)
    for order in orders:
        summary = _sales_order_invoice_summary(order)
        if summary["uninvoiced_amount"] <= ZERO_AMOUNT:
            continue
        rows.append(
            {
                "sales_order_id": order.id,
                "reconciliation_item_id": None,
                "source_no": order.sales_order_no,
                "source_date": order.order_date,
                "total_amount": summary["total_amount"],
                "invoiced_amount": summary["invoiced_amount"],
                "available_amount": summary["uninvoiced_amount"],
                "suggested_amount": summary["uninvoiced_amount"],
            }
        )
    return rows


def _validate_customer_invoice_confirm(invoice: CustomerInvoice) -> str:
    if not _customer_invoice_has_active_attachment(invoice):
        return "请先上传发票附件，上传后才可以确认已开票"
    items = list(invoice.items.select_related("sales_order").order_by("line_no"))
    if not items:
        return "开票单没有明细，不能确认"
    item_total = _money(sum((item.invoice_amount for item in items), ZERO_AMOUNT))
    if item_total != invoice.invoice_amount:
        return "开票明细金额合计必须等于开票单金额"
    if item_total <= ZERO_AMOUNT:
        return "开票金额必须大于 0"
    order_ids = sorted({item.sales_order_id for item in items})
    locked_orders = {
        order.id: order
        for order in SalesOrder.objects.select_for_update().filter(pk__in=order_ids).order_by("id")
    }
    amount_by_order = {}
    for item in items:
        order = locked_orders.get(item.sales_order_id)
        if not order:
            return "开票明细中的销售订单不存在"
        if order.customer_id != invoice.customer_id:
            return "开票明细客户与开票单客户不一致"
        amount_by_order[item.sales_order_id] = amount_by_order.get(item.sales_order_id, ZERO_AMOUNT) + item.invoice_amount
    for order_id, requested_amount in amount_by_order.items():
        order = locked_orders[order_id]
        available_amount = _sales_order_uninvoiced_amount(order, exclude_invoice_id=invoice.id)
        if requested_amount > available_amount:
            return f"{order.sales_order_no} 的开票金额不能超过未开票金额 {available_amount}"
    return ""


def _active_customer_invoice_ids():
    return Attachment.objects.filter(
        source_doc_type="customer_invoice",
        status=Attachment.AttachmentStatus.ACTIVE,
    ).values_list("source_doc_id", flat=True)


def _confirmed_customer_invoice_items():
    return CustomerInvoiceItem.objects.filter(
        customer_invoice__status=CustomerInvoice.Status.CONFIRMED,
        customer_invoice_id__in=_active_customer_invoice_ids(),
    )


def _customer_invoice_has_active_attachment(invoice: CustomerInvoice) -> bool:
    return Attachment.objects.filter(
        source_doc_type="customer_invoice",
        source_doc_id=invoice.id,
        status=Attachment.AttachmentStatus.ACTIVE,
    ).exists()


def _sales_order_invoiced_amount(sales_order_id: int, exclude_invoice_id: int | None = None) -> Decimal:
    queryset = _confirmed_customer_invoice_items().filter(sales_order_id=sales_order_id)
    if exclude_invoice_id:
        queryset = queryset.exclude(customer_invoice_id=exclude_invoice_id)
    return _money(queryset.aggregate(total=Sum("invoice_amount"))["total"] or ZERO_AMOUNT)


def _sales_order_uninvoiced_amount(order: SalesOrder, exclude_invoice_id: int | None = None) -> Decimal:
    return _money(max((order.total_amount or ZERO_AMOUNT) - _sales_order_invoiced_amount(order.id, exclude_invoice_id), ZERO_AMOUNT))


def _sales_order_invoice_summary(order: SalesOrder) -> dict:
    total_amount = _money(order.total_amount or ZERO_AMOUNT)
    invoiced_amount = min(_sales_order_invoiced_amount(order.id), total_amount)
    uninvoiced_amount = _money(max(total_amount - invoiced_amount, ZERO_AMOUNT))
    if total_amount <= ZERO_AMOUNT or invoiced_amount <= ZERO_AMOUNT:
        status_label = "未开票"
    elif uninvoiced_amount <= ZERO_AMOUNT:
        status_label = "已开票"
    else:
        status_label = "部分开票"
    return {
        "total_amount": total_amount,
        "invoiced_amount": _money(invoiced_amount),
        "uninvoiced_amount": uninvoiced_amount,
        "status_label": status_label,
    }


def _decorate_customer_reconciliation_invoice_rows(rows: list[dict]) -> tuple[list[dict], dict]:
    decorated_rows = []
    total_amount = ZERO_AMOUNT
    invoiced_amount = ZERO_AMOUNT
    for row in rows:
        decorated = {**row}
        if row["source_type"] == ReconciliationItem.SourceType.SALES_ORDER:
            row_invoiced = min(_sales_order_invoiced_amount(row["source_doc_id"]), row["open_amount"])
            row_uninvoiced = _money(max(row["open_amount"] - row_invoiced, ZERO_AMOUNT))
            decorated["invoiced_amount"] = _money(row_invoiced)
            decorated["uninvoiced_amount"] = row_uninvoiced
            total_amount += row["open_amount"]
            invoiced_amount += row_invoiced
        else:
            decorated["invoiced_amount"] = ZERO_AMOUNT
            decorated["uninvoiced_amount"] = row["open_amount"]
        decorated_rows.append(decorated)
    total_amount = _money(total_amount)
    invoiced_amount = _money(invoiced_amount)
    return decorated_rows, {
        "total_amount": total_amount,
        "invoiced_amount": invoiced_amount,
        "uninvoiced_amount": _money(max(total_amount - invoiced_amount, ZERO_AMOUNT)),
    }


def _customer_allocation_target_groups(receipt: CustomerReceipt, user=None):
    if receipt.status != CustomerReceipt.Status.PENDING_APPROVAL:
        return [], [], []
    remaining_receipt_amount = receipt.receipt_amount
    order_targets = []
    orders = (
        SalesOrder.objects.filter(customer=receipt.customer)
        .exclude(status__in=[SalesOrder.Status.DRAFT, SalesOrder.Status.PENDING_APPROVAL, SalesOrder.Status.REJECTED])
        .order_by("-order_date", "-id")
    )
    if user is not None and not _can_process_full_finance_payment(user):
        orders = _filter_customer_sales_order_queryset_for_user(orders, user)
    for order in orders:
        available_amount = customer_order_available_allocation_amount(order)
        if available_amount <= 0:
            continue
        suggested_amount = min(remaining_receipt_amount, available_amount) if remaining_receipt_amount > 0 else Decimal("0.00")
        remaining_receipt_amount -= suggested_amount
        order_targets.append(
            {
                "order": order,
                "available_amount": available_amount,
                "suggested_amount": suggested_amount,
            }
        )

    reconciliation_targets = []
    reconciliations = Reconciliation.objects.filter(
        party_type=Reconciliation.PartyType.CUSTOMER,
        customer=receipt.customer,
        status=Reconciliation.Status.CONFIRMED,
    ).order_by("-period_end", "-id")
    if user is not None and not _can_process_full_finance_payment(user):
        reconciliations = _filter_reconciliation_queryset_for_user(reconciliations, user)
    for reconciliation in reconciliations:
        available_amount = customer_reconciliation_available_allocation_amount(reconciliation)
        if available_amount <= 0:
            continue
        suggested_amount = min(remaining_receipt_amount, available_amount) if remaining_receipt_amount > 0 else Decimal("0.00")
        remaining_receipt_amount -= suggested_amount
        reconciliation_targets.append(
            {
                "reconciliation": reconciliation,
                "available_amount": available_amount,
                "suggested_amount": suggested_amount,
            }
        )

    opening_targets = []
    if user is not None and not _can_process_full_finance_payment(user):
        return order_targets, reconciliation_targets, opening_targets
    openings = OpeningReceivable.objects.filter(
        customer=receipt.customer,
        status__in=[OpeningReceivable.Status.OPEN, OpeningReceivable.Status.PART_SETTLED],
        remaining_amount__gt=0,
    ).order_by("opening_date", "id")
    for opening in openings:
        available_amount = customer_opening_receivable_available_allocation_amount(opening)
        if available_amount <= 0:
            continue
        suggested_amount = min(remaining_receipt_amount, available_amount) if remaining_receipt_amount > 0 else Decimal("0.00")
        remaining_receipt_amount -= suggested_amount
        opening_targets.append(
            {
                "opening": opening,
                "available_amount": available_amount,
                "suggested_amount": suggested_amount,
            }
        )
    return order_targets, reconciliation_targets, opening_targets


def _supplier_allocation_target_groups(payment: SupplierPayment, user=None):
    if payment.status != SupplierPayment.Status.PENDING_APPROVAL:
        return [], [], []
    remaining_payment_amount = payment.payment_amount
    receipt_targets = []
    receipts = (
        PurchaseReceipt.objects.filter(supplier=payment.supplier, status=PurchaseReceipt.Status.RECEIVED)
        .select_related("purchase_order")
        .order_by("-receipt_date", "-id")
    )
    if user is not None and not _can_process_full_finance_payment(user):
        receipts = _filter_supplier_purchase_receipt_queryset_for_user(receipts, user)
    for receipt in receipts:
        available_amount = supplier_receipt_available_allocation_amount(receipt)
        if available_amount <= 0:
            continue
        suggested_amount = min(remaining_payment_amount, available_amount) if remaining_payment_amount > 0 else Decimal("0.00")
        remaining_payment_amount -= suggested_amount
        receipt_targets.append(
            {
                "receipt": receipt,
                "available_amount": available_amount,
                "suggested_amount": suggested_amount,
            }
        )

    reconciliation_targets = []
    reconciliations = Reconciliation.objects.filter(
        party_type=Reconciliation.PartyType.SUPPLIER,
        supplier=payment.supplier,
        status=Reconciliation.Status.CONFIRMED,
    ).order_by("-period_end", "-id")
    if user is not None and not _can_process_full_finance_payment(user):
        reconciliations = _filter_reconciliation_queryset_for_user(reconciliations, user)
    for reconciliation in reconciliations:
        available_amount = supplier_reconciliation_available_allocation_amount(reconciliation)
        if available_amount <= 0:
            continue
        suggested_amount = min(remaining_payment_amount, available_amount) if remaining_payment_amount > 0 else Decimal("0.00")
        remaining_payment_amount -= suggested_amount
        reconciliation_targets.append(
            {
                "reconciliation": reconciliation,
                "available_amount": available_amount,
                "suggested_amount": suggested_amount,
            }
        )

    opening_targets = []
    if user is not None and not _can_process_full_finance_payment(user):
        return receipt_targets, reconciliation_targets, opening_targets
    openings = OpeningPayable.objects.filter(
        supplier=payment.supplier,
        status__in=[OpeningPayable.Status.OPEN, OpeningPayable.Status.PART_SETTLED],
        remaining_amount__gt=0,
    ).order_by("opening_date", "id")
    for opening in openings:
        available_amount = supplier_opening_payable_available_allocation_amount(opening)
        if available_amount <= 0:
            continue
        suggested_amount = min(remaining_payment_amount, available_amount) if remaining_payment_amount > 0 else Decimal("0.00")
        remaining_payment_amount -= suggested_amount
        opening_targets.append(
            {
                "opening": opening,
                "available_amount": available_amount,
                "suggested_amount": suggested_amount,
            }
        )
    return receipt_targets, reconciliation_targets, opening_targets


def _customer_credit_balance_target_orders(balance: CustomerCreditBalance) -> list[dict]:
    targets = []
    orders = (
        SalesOrder.objects.filter(customer=balance.customer)
        .exclude(
            status__in=[
                SalesOrder.Status.DRAFT,
                SalesOrder.Status.PENDING_APPROVAL,
                SalesOrder.Status.REJECTED,
                SalesOrder.Status.VOIDED,
            ]
        )
        .order_by("-order_date", "-id")
    )
    for order in orders:
        available_amount = customer_order_available_allocation_amount(order)
        if available_amount > ZERO_AMOUNT:
            targets.append({"order": order, "available_amount": available_amount})
    return targets


def _supplier_credit_balance_target_receipts(balance: SupplierCreditBalance) -> list[dict]:
    targets = []
    receipts = (
        PurchaseReceipt.objects.filter(supplier=balance.supplier, status=PurchaseReceipt.Status.RECEIVED)
        .select_related("purchase_order")
        .order_by("-receipt_date", "-id")
    )
    for receipt in receipts:
        available_amount = supplier_receipt_available_allocation_amount(receipt)
        if available_amount > ZERO_AMOUNT:
            targets.append({"receipt": receipt, "available_amount": available_amount})
    return targets


def _flash_result(request, result, fallback_message: str) -> None:
    if result.success:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message or result.error_code or fallback_message)


def _can_view_finance_amount(user) -> bool:
    return user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT)


def _operations_period_summary(start_date: date, end_date: date) -> dict:
    sales_amount = _sum_decimal(
        SalesOrder.objects.filter(
            order_date__range=(start_date, end_date),
            status__in=[
                SalesOrder.Status.CONFIRMED,
                SalesOrder.Status.IN_PRODUCTION,
                SalesOrder.Status.SHIPPED,
                SalesOrder.Status.COMPLETED,
            ],
        ),
        "total_amount",
    )
    customer_return_amount = _sum_decimal(
        CustomerReturn.objects.filter(
            return_date__range=(start_date, end_date),
            status__in=[
                CustomerReturn.Status.CONFIRMED,
                CustomerReturn.Status.RECEIVED,
            ],
        ),
        "return_amount",
    )
    purchase_amount = _sum_decimal(
        PurchaseReceipt.objects.filter(
            receipt_date__range=(start_date, end_date),
            status__in=[
                PurchaseReceipt.Status.PARTIAL_RECEIVED,
                PurchaseReceipt.Status.RECEIVED,
            ],
        ),
        "items__accepted_qty",
        multiplier_field="items__unit_price",
    )
    supplier_return_amount = _sum_decimal(
        SupplierReturn.objects.filter(
            return_date__range=(start_date, end_date),
            status__in=[
                SupplierReturn.Status.CONFIRMED,
                SupplierReturn.Status.SHIPPED,
            ],
        ),
        "return_amount",
    )
    received_amount = _net_customer_receipt_amount(start_date, end_date)
    paid_amount = _net_supplier_payment_amount(start_date, end_date)
    expense_amount = _sum_decimal(
        ExpenseRecord.objects.filter(
            expense_date__range=(start_date, end_date),
            status=ExpenseRecord.Status.CONFIRMED,
        ),
        "amount",
    )
    gross_cash_amount = received_amount - paid_amount - expense_amount
    estimated_margin_amount = sales_amount - customer_return_amount - purchase_amount + supplier_return_amount - expense_amount
    return {
        "sales_amount": sales_amount,
        "customer_return_amount": customer_return_amount,
        "purchase_amount": purchase_amount,
        "supplier_return_amount": supplier_return_amount,
        "received_amount": received_amount,
        "paid_amount": paid_amount,
        "expense_amount": expense_amount,
        "cash_net_amount": gross_cash_amount.quantize(MONEY_QUANT),
        "estimated_margin_amount": estimated_margin_amount.quantize(MONEY_QUANT),
    }


def _operations_balance_summary() -> dict:
    opening_receivable_remaining = _sum_decimal(
        OpeningReceivable.objects.exclude(status=OpeningReceivable.Status.VOIDED),
        "remaining_amount",
    )
    opening_payable_remaining = _sum_decimal(
        OpeningPayable.objects.exclude(status=OpeningPayable.Status.VOIDED),
        "remaining_amount",
    )
    customer_credit_remaining = _sum_decimal(
        CustomerCreditBalance.objects.exclude(
            status__in=[
                CustomerCreditBalance.Status.USED_UP,
                CustomerCreditBalance.Status.CLOSED,
            ]
        ),
        "remaining_amount",
    )
    supplier_credit_remaining = _sum_decimal(
        SupplierCreditBalance.objects.exclude(
            status__in=[
                SupplierCreditBalance.Status.USED_UP,
                SupplierCreditBalance.Status.CLOSED,
            ]
        ),
        "remaining_amount",
    )
    current_receivable = _current_sales_receivable_amount()
    current_payable = _current_purchase_payable_amount()
    return {
        "opening_receivable_remaining": opening_receivable_remaining,
        "opening_payable_remaining": opening_payable_remaining,
        "current_receivable": current_receivable,
        "current_payable": current_payable,
        "total_receivable": (opening_receivable_remaining + current_receivable).quantize(MONEY_QUANT),
        "total_payable": (opening_payable_remaining + current_payable).quantize(MONEY_QUANT),
        "customer_credit_remaining": customer_credit_remaining,
        "supplier_credit_remaining": supplier_credit_remaining,
    }


def _current_sales_receivable_amount() -> Decimal:
    total = ZERO_AMOUNT
    for order in (
        SalesOrder.objects.filter(
            status__in=[
                SalesOrder.Status.CONFIRMED,
                SalesOrder.Status.IN_PRODUCTION,
                SalesOrder.Status.SHIPPED,
                SalesOrder.Status.COMPLETED,
            ]
        )
        .prefetch_related("items")
        .order_by("id")
    ):
        receivable = sum((item.line_amount for item in order.items.all()), start=ZERO_AMOUNT)
        allocated = _sum_decimal(CustomerReceiptAllocation.objects.filter(sales_order=order), "allocated_amount")
        remaining = receivable - allocated
        if remaining > ZERO_AMOUNT:
            total += remaining
    return total.quantize(MONEY_QUANT)


def _current_purchase_payable_amount() -> Decimal:
    total = ZERO_AMOUNT
    for receipt in (
        PurchaseReceipt.objects.filter(
            status__in=[
                PurchaseReceipt.Status.PARTIAL_RECEIVED,
                PurchaseReceipt.Status.RECEIVED,
            ]
        )
        .prefetch_related("items")
        .order_by("id")
    ):
        payable = sum((item.accepted_qty * item.unit_price for item in receipt.items.all()), start=ZERO_AMOUNT).quantize(MONEY_QUANT)
        allocated = _sum_decimal(SupplierPaymentAllocation.objects.filter(purchase_receipt=receipt), "allocated_amount")
        remaining = payable - allocated
        if remaining > ZERO_AMOUNT:
            total += remaining
    return total.quantize(MONEY_QUANT)


def _net_customer_receipt_amount(start_date: date, end_date: date) -> Decimal:
    receipt_total = _sum_decimal(
        CustomerReceipt.objects.filter(
            receipt_date__range=(start_date, end_date),
            status__in=[
                CustomerReceipt.Status.CONFIRMED,
                CustomerReceipt.Status.PART_REVERSED,
                CustomerReceipt.Status.REVERSED,
            ],
        ),
        "receipt_amount",
    )
    reversal_total = _sum_decimal(
        CustomerReceiptReversal.objects.filter(
            confirmed_at__date__range=(start_date, end_date),
            status=CustomerReceiptReversal.Status.CONFIRMED,
        ),
        "reversal_amount",
    )
    return (receipt_total - reversal_total).quantize(MONEY_QUANT)


def _net_supplier_payment_amount(start_date: date, end_date: date) -> Decimal:
    payment_total = _sum_decimal(
        SupplierPayment.objects.filter(
            payment_date__range=(start_date, end_date),
            status__in=[
                SupplierPayment.Status.CONFIRMED,
                SupplierPayment.Status.PART_REVERSED,
                SupplierPayment.Status.REVERSED,
            ],
        ),
        "payment_amount",
    )
    reversal_total = _sum_decimal(
        SupplierPaymentReversal.objects.filter(
            confirmed_at__date__range=(start_date, end_date),
            status=SupplierPaymentReversal.Status.CONFIRMED,
        ),
        "reversal_amount",
    )
    return (payment_total - reversal_total).quantize(MONEY_QUANT)


def _sum_decimal(queryset, field_name: str, multiplier_field: str = "") -> Decimal:
    if multiplier_field:
        total = ZERO_AMOUNT
        for value, multiplier in queryset.values_list(field_name, multiplier_field):
            total += (value or ZERO_AMOUNT) * (multiplier or ZERO_AMOUNT)
        return total.quantize(MONEY_QUANT)
    return (queryset.aggregate(total=Sum(field_name))["total"] or ZERO_AMOUNT).quantize(MONEY_QUANT)


def _customer_receipt_customer_queryset(user):
    queryset = Customer.objects.all()
    if _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset.distinct()
    return queryset.filter(Q(sales_owner=user) | Q(created_by=user) | Q(sales_orders__created_by=user)).distinct()


def _filter_customer_sales_order_queryset_for_user(queryset, user):
    if _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset
    return queryset.filter(Q(customer__sales_owner=user) | Q(customer__created_by=user) | Q(created_by=user)).distinct()


def _filter_customer_receipt_queryset_for_user(queryset, user):
    if _can_view_finance_amount(user) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset
    if user_has_permission(user, PermissionCode.SALES_PROCESS):
        return queryset.filter(
            Q(customer__sales_owner=user)
            | Q(customer__created_by=user)
            | Q(customer__sales_orders__created_by=user)
            | Q(created_by=user)
            | Q(handled_by=user)
        ).distinct()
    return queryset.none()


def _filter_customer_invoice_queryset_for_user(queryset, user):
    if _can_view_finance_amount(user) or user_has_permission(user, PermissionCode.SALES_VIEW_ALL):
        return queryset
    if user_has_permission(user, PermissionCode.SALES_PROCESS):
        return queryset.filter(
            Q(customer__sales_owner=user)
            | Q(customer__created_by=user)
            | Q(customer__sales_orders__created_by=user)
            | Q(created_by=user)
        ).distinct()
    return queryset.none()


def _customer_invoice_reconciliation_queryset(user, customer_id=None):
    queryset = Reconciliation.objects.filter(
        party_type=Reconciliation.PartyType.CUSTOMER,
    ).exclude(status=Reconciliation.Status.VOIDED)
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    return _filter_reconciliation_queryset_for_user(queryset, user)


def _supplier_payment_supplier_queryset(user):
    queryset = Supplier.objects.all()
    if _can_process_full_finance_payment(user):
        return queryset.distinct()
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset.filter(
            Q(created_by=user)
            | Q(purchase_orders__purchase_owner=user)
            | Q(purchase_orders__created_by=user)
            | Q(purchase_orders__receipts__purchase_order__purchase_owner=user)
            | Q(purchase_orders__receipts__created_by=user)
        ).distinct()
    return queryset.none()


def _filter_supplier_purchase_receipt_queryset_for_user(queryset, user):
    if _can_process_full_finance_payment(user):
        return queryset
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset.filter(Q(purchase_order__purchase_owner=user) | Q(purchase_order__created_by=user) | Q(created_by=user)).distinct()
    return queryset.none()


def _filter_supplier_payment_queryset_for_user(queryset, user):
    if _can_view_finance_amount(user):
        return queryset
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        return queryset.filter(
            Q(created_by=user)
            | Q(handled_by=user)
            | Q(allocations__purchase_receipt__purchase_order__purchase_owner=user)
            | Q(allocations__purchase_receipt__purchase_order__created_by=user)
            | Q(allocations__purchase_receipt__created_by=user)
        ).distinct()
    return queryset.none()


def _filter_reconciliation_queryset_for_user(queryset, user):
    if _can_view_finance_amount(user):
        return queryset
    conditions = Q()
    if user_has_permission(user, PermissionCode.SALES_PROCESS):
        allowed_customers = _customer_receipt_customer_queryset(user)
        conditions |= Q(party_type=Reconciliation.PartyType.CUSTOMER, customer__in=allowed_customers)
        conditions |= Q(party_type=Reconciliation.PartyType.CUSTOMER, created_by=user)
    if user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        allowed_suppliers = _supplier_payment_supplier_queryset(user)
        conditions |= Q(party_type=Reconciliation.PartyType.SUPPLIER, supplier__in=allowed_suppliers)
        conditions |= Q(party_type=Reconciliation.PartyType.SUPPLIER, created_by=user)
    if not conditions:
        return queryset.none()
    return queryset.filter(conditions).distinct()


def _validate_customer_receipt_allocations_for_user(receipt: CustomerReceipt, allocations: list[dict], user) -> None:
    if _can_process_full_finance_payment(user):
        return
    if not user_has_permission(user, PermissionCode.SALES_PROCESS):
        raise PermissionDenied("缺少客户收款处理权限")
    allowed_orders = _filter_customer_sales_order_queryset_for_user(
        SalesOrder.objects.filter(customer=receipt.customer), user
    )
    allowed_reconciliations = _filter_reconciliation_queryset_for_user(
        Reconciliation.objects.filter(
            party_type=Reconciliation.PartyType.CUSTOMER,
            customer=receipt.customer,
        ),
        user,
    )
    for allocation in allocations:
        sales_order_id = allocation.get("sales_order_id")
        reconciliation_id = allocation.get("reconciliation_id")
        if allocation.get("opening_receivable_id"):
            raise PermissionDenied("销售角色不能核销期初应收")
        if sales_order_id and not allowed_orders.filter(pk=sales_order_id).exists():
            raise PermissionDenied("只能核销自己负责的销售订单")
        if reconciliation_id and not allowed_reconciliations.filter(pk=reconciliation_id).exists():
            raise PermissionDenied("只能核销自己负责的客户对账单")


def _validate_supplier_payment_allocations_for_user(payment: SupplierPayment, allocations: list[dict], user) -> None:
    if _can_process_full_finance_payment(user):
        return
    if not user_has_permission(user, PermissionCode.PURCHASE_PROCESS):
        raise PermissionDenied("缺少供应商付款处理权限")
    allowed_receipts = _filter_supplier_purchase_receipt_queryset_for_user(
        PurchaseReceipt.objects.filter(supplier=payment.supplier), user
    )
    allowed_reconciliations = _filter_reconciliation_queryset_for_user(
        Reconciliation.objects.filter(
            party_type=Reconciliation.PartyType.SUPPLIER,
            supplier=payment.supplier,
        ),
        user,
    )
    for allocation in allocations:
        purchase_receipt_id = allocation.get("purchase_receipt_id")
        reconciliation_id = allocation.get("reconciliation_id")
        if allocation.get("opening_payable_id"):
            raise PermissionDenied("采购角色不能核销期初应付")
        if purchase_receipt_id and not allowed_receipts.filter(pk=purchase_receipt_id).exists():
            raise PermissionDenied("只能核销自己负责的进货单")
        if reconciliation_id and not allowed_reconciliations.filter(pk=reconciliation_id).exists():
            raise PermissionDenied("只能核销自己创建的供应商对账单")


def _reconciliation_party_type_choices(user):
    choices = []
    if _can_create_customer_reconciliation(user):
        choices.append((Reconciliation.PartyType.CUSTOMER, Reconciliation.PartyType.CUSTOMER.label))
    if _can_create_supplier_reconciliation(user):
        choices.append((Reconciliation.PartyType.SUPPLIER, Reconciliation.PartyType.SUPPLIER.label))
    return tuple(choices)


def _can_process_finance_payment(user) -> bool:
    return user_has_permission(user, PermissionCode.FINANCE_PAYMENT_PROCESS)


def _can_process_full_finance_payment(user) -> bool:
    return _can_view_finance_amount(user) and _can_process_finance_payment(user)


def _can_view_customer_receipt(user) -> bool:
    return _can_view_finance_amount(user) or user_has_permission(user, PermissionCode.SALES_PROCESS)


def _can_view_customer_receipt_amount(user) -> bool:
    return _can_view_customer_receipt(user)


def _can_process_customer_receipt(user) -> bool:
    return _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.SALES_PROCESS)


def _can_view_customer_invoice(user) -> bool:
    return _can_view_finance_amount(user) or user_has_permission(user, PermissionCode.SALES_PROCESS)


def _can_view_customer_invoice_amount(user) -> bool:
    return _can_view_customer_invoice(user)


def _can_process_customer_invoice(user) -> bool:
    return _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.SALES_PROCESS)


def _can_view_supplier_payment(user) -> bool:
    return _can_view_finance_amount(user) or user_has_permission(user, PermissionCode.PURCHASE_PROCESS)


def _can_view_supplier_payment_amount(user) -> bool:
    return _can_view_supplier_payment(user)


def _can_process_supplier_payment(user) -> bool:
    return _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.PURCHASE_PROCESS)


def _can_view_reconciliation(user) -> bool:
    return (
        _can_view_finance_amount(user)
        or user_has_permission(user, PermissionCode.SALES_PROCESS)
        or user_has_permission(user, PermissionCode.PURCHASE_PROCESS)
    )


def _can_view_reconciliation_amount(user) -> bool:
    return _can_view_reconciliation(user)


def _can_create_customer_reconciliation(user) -> bool:
    return _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.SALES_PROCESS)


def _can_create_supplier_reconciliation(user) -> bool:
    return _can_process_full_finance_payment(user) or user_has_permission(user, PermissionCode.PURCHASE_PROCESS)


def _can_create_reconciliation(user) -> bool:
    return _can_create_customer_reconciliation(user) or _can_create_supplier_reconciliation(user)


def _can_process_reconciliation(user, reconciliation: Reconciliation) -> bool:
    if _can_process_full_finance_payment(user):
        return True
    if reconciliation.party_type == Reconciliation.PartyType.CUSTOMER:
        return user_has_permission(user, PermissionCode.SALES_PROCESS) and _customer_receipt_customer_queryset(user).filter(
            pk=reconciliation.customer_id
        ).exists()
    if reconciliation.party_type == Reconciliation.PartyType.SUPPLIER:
        return user_has_permission(user, PermissionCode.PURCHASE_PROCESS) and (
            reconciliation.created_by_id == user.id
            or _supplier_payment_supplier_queryset(user).filter(pk=reconciliation.supplier_id).exists()
        )
    return False


def _require_finance_amount(user) -> None:
    if not _can_view_finance_amount(user):
        raise PermissionDenied("缺少财务金额查看权限")


def _require_finance_payment_process(user) -> None:
    if not _can_view_finance_amount(user) or not _can_process_finance_payment(user):
        raise PermissionDenied("缺少收付款处理权限")


def _require_customer_receipt_view(user) -> None:
    if not _can_view_customer_receipt(user):
        raise PermissionDenied("缺少客户收款查看权限")


def _require_customer_receipt_process(user) -> None:
    if not _can_process_customer_receipt(user):
        raise PermissionDenied("缺少客户收款处理权限")


def _require_customer_invoice_view(user) -> None:
    if not _can_view_customer_invoice(user):
        raise PermissionDenied("缺少客户开票查看权限")


def _require_customer_invoice_process(user) -> None:
    if not _can_process_customer_invoice(user):
        raise PermissionDenied("缺少客户开票处理权限")


def _require_supplier_payment_view(user) -> None:
    if not _can_view_supplier_payment(user):
        raise PermissionDenied("缺少供应商付款查看权限")


def _require_supplier_payment_process(user) -> None:
    if not _can_process_supplier_payment(user):
        raise PermissionDenied("缺少供应商付款处理权限")


def _require_reconciliation_view(user) -> None:
    if not _can_view_reconciliation(user):
        raise PermissionDenied("缺少对账单查看权限")


def _require_reconciliation_create(user) -> None:
    if not _can_create_reconciliation(user):
        raise PermissionDenied("缺少对账单处理权限")


def _customer_receipt_snapshot(receipt: CustomerReceipt) -> dict:
    return {
        "receipt_no": receipt.receipt_no,
        "customer_id": receipt.customer_id,
        "receipt_date": receipt.receipt_date.isoformat() if receipt.receipt_date else "",
        "receipt_amount": str(receipt.receipt_amount),
        "unallocated_amount": str(receipt.unallocated_amount),
        "receipt_method": receipt.receipt_method,
        "status": receipt.status,
        "handled_by_id": receipt.handled_by_id,
        "remark": receipt.remark,
    }


def _customer_invoice_snapshot(invoice: CustomerInvoice) -> dict:
    invoice.refresh_from_db()
    return {
        "invoice_no": invoice.invoice_no,
        "external_invoice_no": invoice.external_invoice_no,
        "customer_id": invoice.customer_id,
        "reconciliation_id": invoice.reconciliation_id,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else "",
        "invoice_amount": str(invoice.invoice_amount),
        "status": invoice.status,
        "confirmed_by_id": invoice.confirmed_by_id,
        "confirmed_at": invoice.confirmed_at.isoformat() if invoice.confirmed_at else "",
        "remark": invoice.remark,
        "items": [
            {
                "line_no": item.line_no,
                "sales_order_id": item.sales_order_id,
                "reconciliation_item_id": item.reconciliation_item_id,
                "invoice_amount": str(item.invoice_amount),
            }
            for item in invoice.items.order_by("line_no")
        ],
    }


def _supplier_payment_snapshot(payment: SupplierPayment) -> dict:
    return {
        "payment_no": payment.payment_no,
        "supplier_id": payment.supplier_id,
        "payment_date": payment.payment_date.isoformat() if payment.payment_date else "",
        "payment_amount": str(payment.payment_amount),
        "unallocated_amount": str(payment.unallocated_amount),
        "payment_method": payment.payment_method,
        "status": payment.status,
        "handled_by_id": payment.handled_by_id,
        "remark": payment.remark,
    }


def _opening_receivable_snapshot(opening: OpeningReceivable) -> dict:
    opening.refresh_from_db()
    return {
        "opening_no": opening.opening_no,
        "customer_id": opening.customer_id,
        "source_doc_no": opening.source_doc_no,
        "opening_date": opening.opening_date.isoformat() if opening.opening_date else "",
        "due_date": opening.due_date.isoformat() if opening.due_date else "",
        "opening_amount": str(opening.opening_amount),
        "settled_amount": str(opening.settled_amount),
        "remaining_amount": str(opening.remaining_amount),
        "status": opening.status,
        "remark": opening.remark,
    }


def _opening_payable_snapshot(opening: OpeningPayable) -> dict:
    opening.refresh_from_db()
    return {
        "opening_no": opening.opening_no,
        "supplier_id": opening.supplier_id,
        "source_doc_no": opening.source_doc_no,
        "opening_date": opening.opening_date.isoformat() if opening.opening_date else "",
        "due_date": opening.due_date.isoformat() if opening.due_date else "",
        "opening_amount": str(opening.opening_amount),
        "settled_amount": str(opening.settled_amount),
        "remaining_amount": str(opening.remaining_amount),
        "status": opening.status,
        "remark": opening.remark,
    }


def _expense_record_snapshot(expense: ExpenseRecord) -> dict:
    expense.refresh_from_db()
    return {
        "expense_no": expense.expense_no,
        "expense_date": expense.expense_date.isoformat() if expense.expense_date else "",
        "category": expense.category,
        "amount": str(expense.amount),
        "payment_method": expense.payment_method,
        "payee": expense.payee,
        "invoice_no": expense.invoice_no,
        "handled_by_id": expense.handled_by_id,
        "status": expense.status,
        "confirmed_by_id": expense.confirmed_by_id,
        "confirmed_at": expense.confirmed_at.isoformat() if expense.confirmed_at else "",
        "remark": expense.remark,
    }


def _validate_reconciliation_input(party_type, period_start, period_end, customer_id, supplier_id, user=None) -> str:
    if party_type not in Reconciliation.PartyType.values:
        return "请选择对账对象类型"
    if user is not None:
        if party_type == Reconciliation.PartyType.CUSTOMER and not _can_create_customer_reconciliation(user):
            return "缺少客户对账单处理权限"
        if party_type == Reconciliation.PartyType.SUPPLIER and not _can_create_supplier_reconciliation(user):
            return "缺少供应商对账单处理权限"
    if not period_start or not period_end:
        return "请选择对账开始日期和结束日期"
    if period_start > period_end:
        return "对账开始日期不能晚于结束日期"
    if party_type == Reconciliation.PartyType.CUSTOMER and not customer_id:
        return "客户对账必须选择客户"
    if party_type == Reconciliation.PartyType.SUPPLIER and not supplier_id:
        return "供应商对账必须选择供应商"
    if party_type == Reconciliation.PartyType.CUSTOMER and user is not None and not _customer_receipt_customer_queryset(user).filter(pk=customer_id).exists():
        return "客户不存在或不属于当前销售范围"
    if party_type == Reconciliation.PartyType.CUSTOMER and user is None and not Customer.objects.filter(pk=customer_id).exists():
        return "客户不存在"
    if party_type == Reconciliation.PartyType.SUPPLIER and user is not None and not _supplier_payment_supplier_queryset(user).filter(pk=supplier_id).exists():
        return "供应商不存在或不属于当前采购范围"
    if party_type == Reconciliation.PartyType.SUPPLIER and not Supplier.objects.filter(pk=supplier_id).exists():
        return "供应商不存在"
    return ""


def _reconciliation_rows(party_type, customer_id, supplier_id, period_start, period_end, for_update=False, user=None) -> list[dict]:
    if party_type == Reconciliation.PartyType.CUSTOMER:
        return _customer_reconciliation_rows(customer_id, period_start, period_end, for_update=for_update, user=user)
    if party_type == Reconciliation.PartyType.SUPPLIER:
        return _supplier_reconciliation_rows(supplier_id, period_start, period_end, for_update=for_update, user=user)
    return []


def _display_reconciliation_rows(reconciliation: Reconciliation, user=None) -> list[dict]:
    snapshot_rows = list(reconciliation.items.order_by("line_no"))
    if (
        user is not None
        and not _can_process_full_finance_payment(user)
        and user_has_permission(user, PermissionCode.PURCHASE_PROCESS)
        and reconciliation.party_type == Reconciliation.PartyType.SUPPLIER
    ):
        allowed_receipt_ids = set(
            _filter_supplier_purchase_receipt_queryset_for_user(PurchaseReceipt.objects.all(), user).values_list("id", flat=True)
        )
        snapshot_rows = [
            item
            for item in snapshot_rows
            if item.source_type == ReconciliationItem.SourceType.PURCHASE_RECEIPT and item.source_doc_id in allowed_receipt_ids
        ]
    if snapshot_rows:
        return [
            {
                "source_type": item.source_type,
                "source_type_label": item.get_source_type_display(),
                "source_doc_id": item.source_doc_id,
                "reconciliation_item_id": item.id,
                "source_no": item.source_no,
                "source_date": item.source_date,
                "gross_amount": item.gross_amount,
                "adjust_amount": item.adjust_amount,
                "allocated_amount": item.allocated_amount,
                "open_amount": item.open_amount,
            }
            for item in snapshot_rows
        ]
    return _reconciliation_rows(
        reconciliation.party_type,
        reconciliation.customer_id,
        reconciliation.supplier_id,
        reconciliation.period_start,
        reconciliation.period_end,
        user=user,
    )


def _reconciliation_statement_rows(reconciliation: Reconciliation, user=None) -> list[dict]:
    source_rows = _display_reconciliation_rows(reconciliation, user)
    if reconciliation.party_type == Reconciliation.PartyType.CUSTOMER:
        order_ids = [row["source_doc_id"] for row in source_rows if row["source_type"] == ReconciliationItem.SourceType.SALES_ORDER]
        orders = {
            order.id: order
            for order in SalesOrder.objects.filter(pk__in=order_ids)
            .prefetch_related("items__finished_material")
            .order_by("order_date", "id")
        }
        rows = []
        line_no = 1
        for source_row in source_rows:
            order = orders.get(source_row["source_doc_id"])
            if not order:
                continue
            first_line = True
            for item in order.items.all().order_by("line_no", "id"):
                material = item.finished_material
                spec = material.spec or item.customer_model_remark
                rows.append(
                    {
                        "line_no": line_no,
                        "source_date": order.order_date,
                        "source_no": order.sales_order_no if first_line else "",
                        "spec": spec,
                        "item_name": f"{material.material_code} {material.material_name}",
                        "qty": item.order_qty,
                        "unit_price": item.unit_price,
                        "amount": item.line_amount,
                        "note": "",
                    }
                )
                first_line = False
                line_no += 1
        return rows

    if reconciliation.party_type == Reconciliation.PartyType.SUPPLIER:
        receipt_ids = [
            row["source_doc_id"] for row in source_rows if row["source_type"] == ReconciliationItem.SourceType.PURCHASE_RECEIPT
        ]
        receipts = {
            receipt.id: receipt
            for receipt in PurchaseReceipt.objects.filter(pk__in=receipt_ids)
            .select_related("purchase_order")
            .prefetch_related("items__material")
            .order_by("receipt_date", "id")
        }
        rows = []
        line_no = 1
        for source_row in source_rows:
            receipt = receipts.get(source_row["source_doc_id"])
            if not receipt:
                continue
            first_line = True
            for item in receipt.items.all().order_by("id"):
                material = item.material
                rows.append(
                    {
                        "line_no": line_no,
                        "source_date": receipt.receipt_date,
                        "source_no": receipt.purchase_receipt_no if first_line else "",
                        "spec": material.spec,
                        "item_name": f"{material.material_code} {material.material_name}",
                        "qty": item.accepted_qty,
                        "unit_price": item.unit_price,
                        "amount": _money(item.accepted_qty * item.unit_price),
                        "note": receipt.purchase_order.purchase_order_no if first_line and receipt.purchase_order_id else "",
                    }
                )
                first_line = False
                line_no += 1
        return rows
    return []


def _statement_rows_total(rows: list[dict]) -> Decimal:
    return _money(sum((row["amount"] for row in rows), ZERO_AMOUNT))


def _reconciliation_party_name(reconciliation: Reconciliation) -> str:
    if reconciliation.party_type == Reconciliation.PartyType.CUSTOMER and reconciliation.customer_id:
        return reconciliation.customer.customer_name
    if reconciliation.party_type == Reconciliation.PartyType.SUPPLIER and reconciliation.supplier_id:
        return reconciliation.supplier.supplier_name
    return ""


def _reconciliation_prior_balance(reconciliation: Reconciliation, user=None) -> Decimal:
    if reconciliation.party_type == Reconciliation.PartyType.CUSTOMER and reconciliation.customer_id:
        total = ZERO_AMOUNT
        orders = (
            SalesOrder.objects.filter(customer=reconciliation.customer, order_date__lt=reconciliation.period_start)
            .exclude(
                status__in=[
                    SalesOrder.Status.DRAFT,
                    SalesOrder.Status.PENDING_APPROVAL,
                    SalesOrder.Status.REJECTED,
                    SalesOrder.Status.VOIDED,
                ]
            )
            .order_by("id")
        )
        if user is not None and not _can_process_full_finance_payment(user):
            orders = _filter_customer_sales_order_queryset_for_user(orders, user)
        for order in orders:
            amount = customer_order_available_allocation_amount(order)
            if amount > ZERO_AMOUNT:
                total += amount
        return _money(total)
    if reconciliation.party_type == Reconciliation.PartyType.SUPPLIER and reconciliation.supplier_id:
        total = ZERO_AMOUNT
        receipts = PurchaseReceipt.objects.filter(
            supplier=reconciliation.supplier,
            receipt_date__lt=reconciliation.period_start,
            status=PurchaseReceipt.Status.RECEIVED,
        ).order_by("id")
        if user is not None and not _can_process_full_finance_payment(user):
            receipts = _filter_supplier_purchase_receipt_queryset_for_user(receipts, user)
        for receipt in receipts:
            amount = supplier_receipt_available_allocation_amount(receipt)
            if amount > ZERO_AMOUNT:
                total += amount
        return _money(total)
    return ZERO_AMOUNT


def _customer_reconciliation_rows(customer_id, period_start, period_end, for_update=False, user=None) -> list[dict]:
    queryset = (
        SalesOrder.objects.filter(customer_id=customer_id, order_date__range=(period_start, period_end))
        .exclude(
            status__in=[
                SalesOrder.Status.DRAFT,
                SalesOrder.Status.PENDING_APPROVAL,
                SalesOrder.Status.REJECTED,
                SalesOrder.Status.VOIDED,
            ]
        )
        .prefetch_related("items")
        .order_by("id")
    )
    if for_update:
        queryset = queryset.select_for_update()
    if user is not None and not _can_process_full_finance_payment(user):
        queryset = _filter_customer_sales_order_queryset_for_user(queryset, user)

    rows = []
    for order in queryset:
        gross_amount = _money(order.items.aggregate(total=Sum("line_amount"))["total"] or ZERO_AMOUNT)
        return_amount = _money(
            CustomerReturn.objects.filter(
                customer_id=customer_id,
                sales_order=order,
                status__in=[CustomerReturn.Status.CONFIRMED, CustomerReturn.Status.RECEIVED],
            ).aggregate(total=Sum("return_amount"))["total"]
            or ZERO_AMOUNT
        )
        allocated_amount = _money(
            CustomerReceiptAllocation.objects.filter(
                sales_order=order,
                customer_receipt__status__in=[
                    CustomerReceipt.Status.CONFIRMED,
                    CustomerReceipt.Status.PART_REVERSED,
                    CustomerReceipt.Status.REVERSED,
                ],
            ).aggregate(total=Sum("allocated_amount"))["total"]
            or ZERO_AMOUNT
        )
        open_amount = _money(max(gross_amount - return_amount - allocated_amount, ZERO_AMOUNT))
        if open_amount <= ZERO_AMOUNT:
            continue
        rows.append(
            {
                "source_type": ReconciliationItem.SourceType.SALES_ORDER,
                "source_type_label": "销售订单",
                "source_doc_id": order.id,
                "source_no": order.sales_order_no,
                "source_date": order.order_date,
                "gross_amount": gross_amount,
                "adjust_amount": return_amount,
                "allocated_amount": allocated_amount,
                "open_amount": open_amount,
            }
        )
    return rows


def _supplier_reconciliation_rows(supplier_id, period_start, period_end, for_update=False, user=None) -> list[dict]:
    queryset = (
        PurchaseReceipt.objects.filter(
            supplier_id=supplier_id,
            receipt_date__range=(period_start, period_end),
            status=PurchaseReceipt.Status.RECEIVED,
        )
        .prefetch_related("items")
        .order_by("id")
    )
    if for_update:
        queryset = queryset.select_for_update()
    if user is not None and not _can_process_full_finance_payment(user):
        queryset = _filter_supplier_purchase_receipt_queryset_for_user(queryset, user)

    rows = []
    for receipt in queryset:
        gross_amount = _money(sum((item.accepted_qty * item.unit_price for item in receipt.items.all()), ZERO_AMOUNT))
        return_amount = _money(
            SupplierReturn.objects.filter(
                supplier_id=supplier_id,
                purchase_receipt=receipt,
                status__in=[SupplierReturn.Status.CONFIRMED, SupplierReturn.Status.SHIPPED],
            ).aggregate(total=Sum("return_amount"))["total"]
            or ZERO_AMOUNT
        )
        allocated_amount = _money(
            SupplierPaymentAllocation.objects.filter(
                purchase_receipt=receipt,
                supplier_payment__status__in=[
                    SupplierPayment.Status.CONFIRMED,
                    SupplierPayment.Status.PART_REVERSED,
                    SupplierPayment.Status.REVERSED,
                ],
            ).aggregate(total=Sum("allocated_amount"))["total"]
            or ZERO_AMOUNT
        )
        open_amount = _money(max(gross_amount - return_amount - allocated_amount, ZERO_AMOUNT))
        if open_amount <= ZERO_AMOUNT:
            continue
        rows.append(
            {
                "source_type": ReconciliationItem.SourceType.PURCHASE_RECEIPT,
                "source_type_label": "进货单",
                "source_doc_id": receipt.id,
                "source_no": receipt.purchase_receipt_no,
                "source_date": receipt.receipt_date,
                "gross_amount": gross_amount,
                "adjust_amount": return_amount,
                "allocated_amount": allocated_amount,
                "open_amount": open_amount,
            }
        )
    return rows


def _rows_total(rows: list[dict]) -> Decimal:
    return _money(sum((row["open_amount"] for row in rows), ZERO_AMOUNT))


def _replace_reconciliation_items(reconciliation: Reconciliation, rows: list[dict]) -> None:
    reconciliation.items.all().delete()
    ReconciliationItem.objects.bulk_create(
        [
            ReconciliationItem(
                reconciliation=reconciliation,
                line_no=index,
                source_type=row["source_type"],
                source_doc_id=row["source_doc_id"],
                source_no=row["source_no"],
                source_date=row["source_date"],
                gross_amount=row["gross_amount"],
                adjust_amount=row["adjust_amount"],
                allocated_amount=row["allocated_amount"],
                open_amount=row["open_amount"],
            )
            for index, row in enumerate(rows, start=1)
        ]
    )


def _money(value) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANT)


def _reconciliation_has_allocations(reconciliation: Reconciliation) -> bool:
    if reconciliation.party_type == Reconciliation.PartyType.CUSTOMER:
        return CustomerReceiptAllocation.objects.filter(reconciliation=reconciliation).exists()
    if reconciliation.party_type == Reconciliation.PartyType.SUPPLIER:
        return SupplierPaymentAllocation.objects.filter(reconciliation=reconciliation).exists()
    return False


def _reconciliation_snapshot(reconciliation: Reconciliation) -> dict:
    return {
        "reconciliation_no": reconciliation.reconciliation_no,
        "party_type": reconciliation.party_type,
        "customer_id": reconciliation.customer_id,
        "supplier_id": reconciliation.supplier_id,
        "period_start": reconciliation.period_start.isoformat() if reconciliation.period_start else "",
        "period_end": reconciliation.period_end.isoformat() if reconciliation.period_end else "",
        "total_amount": str(reconciliation.total_amount),
        "status": reconciliation.status,
        "remark": reconciliation.remark,
    }
