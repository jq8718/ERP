import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO, TextIOWrapper

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import DetailView, TemplateView
from django.utils import timezone

from accounts.permissions import PermissionCode, user_has_permission
from files.services import csv_upload_validation_error, export_queryset_to_csv, record_print_log
from files.view_helpers import build_attachment_panel, export_file_response
from masterdata.models import Customer, Supplier
from purchase.models import PurchaseReceipt, SupplierReturn
from sales.models import CustomerReturn, SalesOrder
from system.services import next_document_no, record_audit_log_from_request
from system.view_helpers import ErpListView, optional_post_reason, require_post_reason, require_second_verify

from .import_services import (
    CUSTOMER_RECEIPT_IMPORT_TEMPLATE_ROWS,
    SUPPLIER_PAYMENT_IMPORT_TEMPLATE_ROWS,
    import_customer_receipts_from_csv,
    import_supplier_payments_from_csv,
)
from .models import (
    CustomerCreditBalance,
    CustomerCreditBalanceTransaction,
    CustomerReceipt,
    CustomerReceiptAllocation,
    Reconciliation,
    ReconciliationItem,
    SupplierCreditBalance,
    SupplierCreditBalanceTransaction,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from .services import (
    apply_customer_credit_balance,
    apply_supplier_credit_balance,
    confirm_customer_receipt,
    confirm_supplier_payment,
    customer_order_available_allocation_amount,
    customer_reconciliation_available_allocation_amount,
    reverse_customer_receipt,
    reverse_supplier_payment,
    supplier_receipt_available_allocation_amount,
    supplier_reconciliation_available_allocation_amount,
)


ZERO_AMOUNT = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


class CustomerReceiptListView(ErpListView):
    model = CustomerReceipt
    page_title = "客户收款"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    create_url_name = "finance:customer_receipt_create"
    create_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS)
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
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class FinanceCsvExportView(LoginRequiredMixin, View):
    module = ""
    list_view_class = None
    ordering = ()
    select_related = ()

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
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
        text_file = TextIOWrapper(upload.file, encoding="utf-8-sig", newline="")
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
            _require_finance_payment_process(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建客户收款"
        context["customers"] = Customer.objects.order_by("customer_no")
        context["receipt_methods"] = CustomerReceipt.ReceiptMethod.choices
        context["today"] = timezone.localdate()
        return context

    def post(self, request):
        amount = _decimal_from_post(request, "receipt_amount")
        receipt_date = _date_from_post(request, "receipt_date")
        customer_id = request.POST.get("customer")
        if amount is None or amount <= 0 or not receipt_date or not customer_id:
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
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("customer", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__sales_order", "allocations__reconciliation", "reversals")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"客户收款 {self.object.receipt_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["can_reverse"] = context["can_view_amount"] and self.object.status in [
            CustomerReceipt.Status.CONFIRMED,
            CustomerReceipt.Status.PART_REVERSED,
        ] and context["can_process_payment"]
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
        allocation_targets, reconciliation_targets = _customer_allocation_target_groups(self.object)
        context["allocation_targets"] = allocation_targets
        context["reconciliation_allocation_targets"] = reconciliation_targets
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
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("customer", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__sales_order", "allocations__reconciliation")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印客户收款 {self.object.receipt_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
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
        _require_finance_payment_process(request.user)
        receipt = CustomerReceipt.objects.select_related("customer").filter(pk=pk).first()
        if receipt is None:
            messages.error(request, "客户收款单不存在")
            return redirect("finance:customer_receipt_list")
        if receipt.status not in self.editable_statuses:
            messages.error(request, "只有草稿或待审核客户收款单可以编辑")
            return redirect("finance:customer_receipt_detail", pk=pk)
        return self._render(request, receipt)

    def post(self, request, pk):
        _require_finance_payment_process(request.user)
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
            or not Customer.objects.filter(pk=customer_id).exists()
        ):
            messages.error(request, "客户、收款日期、收款金额和收款方式必须正确填写")
            return redirect("finance:customer_receipt_edit", pk=pk)

        try:
            with transaction.atomic():
                receipt = CustomerReceipt.objects.select_for_update().get(pk=pk)
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
                "customers": Customer.objects.order_by("customer_no"),
                "receipt_methods": CustomerReceipt.ReceiptMethod.choices,
                "today": timezone.localdate(),
                "receipt": receipt,
                "is_edit": True,
            },
        )


class CustomerReceiptVoidView(LoginRequiredMixin, View):
    voidable_statuses = [CustomerReceipt.Status.DRAFT, CustomerReceipt.Status.PENDING_APPROVAL]

    def post(self, request, pk):
        _require_finance_payment_process(request.user)
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
                receipt = CustomerReceipt.objects.select_for_update().get(pk=pk)
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
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:customer_receipt_detail", pk)
        if verification_response:
            return verification_response
        allocations = _allocation_rows_from_post(request, "sales_order_id", "sales_order_allocated_amount")
        allocations += _allocation_rows_from_post(request, "reconciliation_id", "reconciliation_allocated_amount")
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
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    create_url_name = "finance:supplier_payment_create"
    create_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS)
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
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class SupplierPaymentExportView(FinanceCsvExportView):
    module = "supplier_payments"
    list_view_class = SupplierPaymentListView
    ordering = ("-payment_date", "-id")
    select_related = ("supplier",)


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
        text_file = TextIOWrapper(upload.file, encoding="utf-8-sig", newline="")
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
            _require_finance_payment_process(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "新建供应商付款"
        context["suppliers"] = Supplier.objects.order_by("supplier_no")
        context["payment_methods"] = SupplierPayment.PaymentMethod.choices
        context["today"] = timezone.localdate()
        return context

    def post(self, request):
        amount = _decimal_from_post(request, "payment_amount")
        payment_date = _date_from_post(request, "payment_date")
        supplier_id = request.POST.get("supplier")
        if amount is None or amount <= 0 or not payment_date or not supplier_id:
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
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("supplier", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__purchase_receipt", "allocations__reconciliation", "reversals")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"供应商付款 {self.object.payment_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
        context["can_reverse"] = context["can_view_amount"] and self.object.status in [
            SupplierPayment.Status.CONFIRMED,
            SupplierPayment.Status.PART_REVERSED,
        ] and context["can_process_payment"]
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
        allocation_targets, reconciliation_targets = _supplier_allocation_target_groups(self.object)
        context["allocation_targets"] = allocation_targets
        context["reconciliation_allocation_targets"] = reconciliation_targets
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
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("supplier", "handled_by", "created_by", "confirmed_by")
            .prefetch_related("allocations__purchase_receipt", "allocations__reconciliation")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"打印供应商付款 {self.object.payment_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
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
        _require_finance_payment_process(request.user)
        payment = SupplierPayment.objects.select_related("supplier").filter(pk=pk).first()
        if payment is None:
            messages.error(request, "供应商付款单不存在")
            return redirect("finance:supplier_payment_list")
        if payment.status not in self.editable_statuses:
            messages.error(request, "只有草稿或待审核供应商付款单可以编辑")
            return redirect("finance:supplier_payment_detail", pk=pk)
        return self._render(request, payment)

    def post(self, request, pk):
        _require_finance_payment_process(request.user)
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
            or not Supplier.objects.filter(pk=supplier_id).exists()
        ):
            messages.error(request, "供应商、付款日期、付款金额和付款方式必须正确填写")
            return redirect("finance:supplier_payment_edit", pk=pk)

        try:
            with transaction.atomic():
                payment = SupplierPayment.objects.select_for_update().get(pk=pk)
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
                "suppliers": Supplier.objects.order_by("supplier_no"),
                "payment_methods": SupplierPayment.PaymentMethod.choices,
                "today": timezone.localdate(),
                "payment": payment,
                "is_edit": True,
            },
        )


class SupplierPaymentVoidView(LoginRequiredMixin, View):
    voidable_statuses = [SupplierPayment.Status.DRAFT, SupplierPayment.Status.PENDING_APPROVAL]

    def post(self, request, pk):
        _require_finance_payment_process(request.user)
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
                payment = SupplierPayment.objects.select_for_update().get(pk=pk)
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
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:supplier_payment_detail", pk)
        if verification_response:
            return verification_response
        allocations = _allocation_rows_from_post(request, "purchase_receipt_id", "purchase_receipt_allocated_amount")
        allocations += _allocation_rows_from_post(request, "reconciliation_id", "reconciliation_allocated_amount")
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


class ReconciliationListView(ErpListView):
    model = Reconciliation
    page_title = "对账单"
    view_permission_required = PermissionCode.FINANCE_VIEW_AMOUNT
    permission_denied_message = "缺少财务金额查看权限"
    create_url_name = "finance:reconciliation_create"
    create_permission_required = (PermissionCode.FINANCE_VIEW_AMOUNT, PermissionCode.FINANCE_PAYMENT_PROCESS)
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["mask_sensitive_columns"] = not _can_view_finance_amount(self.request.user)
        return context


class ReconciliationExportView(FinanceCsvExportView):
    module = "reconciliations"
    list_view_class = ReconciliationListView
    ordering = ("-period_start", "-id")
    select_related = ("customer", "supplier")


class ReconciliationCreateView(LoginRequiredMixin, View):
    template_name = "finance/reconciliation_form.html"

    def get(self, request):
        _require_finance_payment_process(request.user)
        return self._render(request)

    def post(self, request):
        _require_finance_payment_process(request.user)
        party_type = request.POST.get("party_type", "")
        period_start = _date_from_post(request, "period_start")
        period_end = _date_from_post(request, "period_end")
        customer_id = request.POST.get("customer") or None
        supplier_id = request.POST.get("supplier") or None

        error_message = _validate_reconciliation_input(party_type, period_start, period_end, customer_id, supplier_id)
        if error_message:
            messages.error(request, error_message)
            return self._render(request)

        rows = _reconciliation_rows(party_type, customer_id, supplier_id, period_start, period_end)
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
                "party_type_choices": Reconciliation.PartyType.choices,
                "customers": Customer.objects.order_by("customer_no"),
                "suppliers": Supplier.objects.order_by("supplier_no"),
                "today": timezone.localdate(),
            },
        )


class ReconciliationDetailView(LoginRequiredMixin, DetailView):
    model = Reconciliation
    template_name = "finance/reconciliation_detail.html"
    context_object_name = "reconciliation"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "supplier", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"对账单 {self.object.reconciliation_no}"
        context["can_view_amount"] = _can_view_finance_amount(self.request.user)
        context["can_process_payment"] = _can_process_finance_payment(self.request.user)
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
        context["rows"] = _display_reconciliation_rows(self.object)
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
            _require_finance_amount(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "supplier", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = _display_reconciliation_rows(self.object)
        context["page_title"] = f"打印对账单 {self.object.reconciliation_no}"
        context["rows"] = rows
        context["current_total"] = _rows_total(rows)
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
        _require_finance_payment_process(request.user)
        verification_response = require_second_verify(request, "finance:reconciliation_detail", pk)
        if verification_response:
            return verification_response
        try:
            with transaction.atomic():
                reconciliation = Reconciliation.objects.select_for_update().get(pk=pk)
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
        _require_finance_payment_process(request.user)
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
                reconciliation = Reconciliation.objects.select_for_update().get(pk=pk)
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
    value = request.POST.get(field_name, "")
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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


def _customer_allocation_target_groups(receipt: CustomerReceipt):
    if receipt.status != CustomerReceipt.Status.PENDING_APPROVAL:
        return [], []
    remaining_receipt_amount = receipt.receipt_amount
    order_targets = []
    orders = (
        SalesOrder.objects.filter(customer=receipt.customer)
        .exclude(status__in=[SalesOrder.Status.DRAFT, SalesOrder.Status.PENDING_APPROVAL, SalesOrder.Status.REJECTED])
        .order_by("-order_date", "-id")
    )
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
    return order_targets, reconciliation_targets


def _supplier_allocation_target_groups(payment: SupplierPayment):
    if payment.status != SupplierPayment.Status.PENDING_APPROVAL:
        return [], []
    remaining_payment_amount = payment.payment_amount
    receipt_targets = []
    receipts = (
        PurchaseReceipt.objects.filter(supplier=payment.supplier, status=PurchaseReceipt.Status.RECEIVED)
        .select_related("purchase_order")
        .order_by("-receipt_date", "-id")
    )
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
    return receipt_targets, reconciliation_targets


def _flash_result(request, result, fallback_message: str) -> None:
    if result.success:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message or result.error_code or fallback_message)


def _can_view_finance_amount(user) -> bool:
    return user_has_permission(user, PermissionCode.FINANCE_VIEW_AMOUNT)


def _can_process_finance_payment(user) -> bool:
    return user_has_permission(user, PermissionCode.FINANCE_PAYMENT_PROCESS)


def _require_finance_amount(user) -> None:
    if not _can_view_finance_amount(user):
        raise PermissionDenied("缺少财务金额查看权限")


def _require_finance_payment_process(user) -> None:
    if not _can_view_finance_amount(user) or not _can_process_finance_payment(user):
        raise PermissionDenied("缺少收付款处理权限")


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


def _validate_reconciliation_input(party_type, period_start, period_end, customer_id, supplier_id) -> str:
    if party_type not in Reconciliation.PartyType.values:
        return "请选择对账对象类型"
    if not period_start or not period_end:
        return "请选择对账开始日期和结束日期"
    if period_start > period_end:
        return "对账开始日期不能晚于结束日期"
    if party_type == Reconciliation.PartyType.CUSTOMER and not customer_id:
        return "客户对账必须选择客户"
    if party_type == Reconciliation.PartyType.SUPPLIER and not supplier_id:
        return "供应商对账必须选择供应商"
    if party_type == Reconciliation.PartyType.CUSTOMER and not Customer.objects.filter(pk=customer_id).exists():
        return "客户不存在"
    if party_type == Reconciliation.PartyType.SUPPLIER and not Supplier.objects.filter(pk=supplier_id).exists():
        return "供应商不存在"
    return ""


def _reconciliation_rows(party_type, customer_id, supplier_id, period_start, period_end, for_update=False) -> list[dict]:
    if party_type == Reconciliation.PartyType.CUSTOMER:
        return _customer_reconciliation_rows(customer_id, period_start, period_end, for_update=for_update)
    if party_type == Reconciliation.PartyType.SUPPLIER:
        return _supplier_reconciliation_rows(supplier_id, period_start, period_end, for_update=for_update)
    return []


def _display_reconciliation_rows(reconciliation: Reconciliation) -> list[dict]:
    snapshot_rows = list(reconciliation.items.order_by("line_no"))
    if snapshot_rows:
        return [
            {
                "source_type": item.source_type,
                "source_type_label": item.get_source_type_display(),
                "source_doc_id": item.source_doc_id,
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
    )


def _customer_reconciliation_rows(customer_id, period_start, period_end, for_update=False) -> list[dict]:
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


def _supplier_reconciliation_rows(supplier_id, period_start, period_end, for_update=False) -> list[dict]:
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
