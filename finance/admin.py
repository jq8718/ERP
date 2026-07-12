from django.contrib import admin

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


@admin.register(Reconciliation)
class ReconciliationAdmin(admin.ModelAdmin):
    list_display = ("reconciliation_no", "party_type", "customer", "supplier", "period_start", "period_end", "total_amount", "status")
    list_filter = ("party_type", "status", "period_start")
    search_fields = ("reconciliation_no", "customer__customer_name", "supplier__supplier_name")


@admin.register(ReconciliationItem)
class ReconciliationItemAdmin(admin.ModelAdmin):
    list_display = ("reconciliation", "line_no", "source_type", "source_no", "source_date", "open_amount")
    list_filter = ("source_type",)
    search_fields = ("reconciliation__reconciliation_no", "source_no")


class CustomerInvoiceItemInline(admin.TabularInline):
    model = CustomerInvoiceItem
    extra = 0


@admin.register(CustomerInvoice)
class CustomerInvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_no", "external_invoice_no", "customer", "invoice_date", "invoice_amount", "status")
    list_filter = ("status", "invoice_date")
    search_fields = ("invoice_no", "external_invoice_no", "customer__customer_name", "reconciliation__reconciliation_no")
    inlines = [CustomerInvoiceItemInline]


@admin.register(CustomerInvoiceItem)
class CustomerInvoiceItemAdmin(admin.ModelAdmin):
    list_display = ("customer_invoice", "line_no", "sales_order", "invoice_amount")
    search_fields = ("customer_invoice__invoice_no", "sales_order__sales_order_no")


@admin.register(CustomerReceipt)
class CustomerReceiptAdmin(admin.ModelAdmin):
    list_display = ("receipt_no", "customer", "receipt_date", "receipt_amount", "unallocated_amount", "status")
    list_filter = ("status", "receipt_method", "receipt_date")
    search_fields = ("receipt_no", "customer__customer_name")


@admin.register(CustomerReceiptAllocation)
class CustomerReceiptAllocationAdmin(admin.ModelAdmin):
    list_display = ("customer_receipt", "sales_order", "reconciliation", "allocated_amount", "allocation_type")
    list_filter = ("allocation_type",)
    search_fields = ("customer_receipt__receipt_no", "sales_order__sales_order_no", "reconciliation__reconciliation_no")


@admin.register(CustomerReceiptReversal)
class CustomerReceiptReversalAdmin(admin.ModelAdmin):
    list_display = ("reversal_no", "source_receipt", "reversal_amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("reversal_no", "source_receipt__receipt_no")


@admin.register(OpeningReceivable)
class OpeningReceivableAdmin(admin.ModelAdmin):
    list_display = ("opening_no", "customer", "source_doc_no", "opening_date", "opening_amount", "remaining_amount", "status")
    list_filter = ("status", "opening_date")
    search_fields = ("opening_no", "source_doc_no", "customer__customer_name")


@admin.register(CustomerCreditBalance)
class CustomerCreditBalanceAdmin(admin.ModelAdmin):
    list_display = ("customer", "source_doc_type", "source_doc_no", "balance_amount", "used_amount", "remaining_amount", "status")
    list_filter = ("status", "source_doc_type")
    search_fields = ("customer__customer_name", "source_doc_no")


@admin.register(CustomerCreditBalanceTransaction)
class CustomerCreditBalanceTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_no", "credit_balance", "action_type", "amount", "target_doc_type", "target_doc_no", "created_at")
    list_filter = ("action_type", "target_doc_type")
    search_fields = ("transaction_no", "target_doc_no")


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = ("payment_no", "supplier", "payment_date", "payment_amount", "unallocated_amount", "status")
    list_filter = ("status", "payment_method", "payment_date")
    search_fields = ("payment_no", "supplier__supplier_name")


@admin.register(SupplierPaymentAllocation)
class SupplierPaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("supplier_payment", "purchase_receipt", "reconciliation", "allocated_amount", "allocation_type")
    list_filter = ("allocation_type",)
    search_fields = ("supplier_payment__payment_no", "purchase_receipt__purchase_receipt_no", "reconciliation__reconciliation_no")


@admin.register(SupplierPaymentReversal)
class SupplierPaymentReversalAdmin(admin.ModelAdmin):
    list_display = ("reversal_no", "source_payment", "reversal_amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("reversal_no", "source_payment__payment_no")


@admin.register(OpeningPayable)
class OpeningPayableAdmin(admin.ModelAdmin):
    list_display = ("opening_no", "supplier", "source_doc_no", "opening_date", "opening_amount", "remaining_amount", "status")
    list_filter = ("status", "opening_date")
    search_fields = ("opening_no", "source_doc_no", "supplier__supplier_name")


@admin.register(ExpenseRecord)
class ExpenseRecordAdmin(admin.ModelAdmin):
    list_display = ("expense_no", "expense_date", "category", "amount", "payee", "status")
    list_filter = ("status", "category", "expense_date")
    search_fields = ("expense_no", "payee", "invoice_no", "remark")


@admin.register(SupplierCreditBalance)
class SupplierCreditBalanceAdmin(admin.ModelAdmin):
    list_display = ("supplier", "source_doc_type", "source_doc_no", "balance_amount", "used_amount", "remaining_amount", "status")
    list_filter = ("status", "source_doc_type")
    search_fields = ("supplier__supplier_name", "source_doc_no")


@admin.register(SupplierCreditBalanceTransaction)
class SupplierCreditBalanceTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_no", "credit_balance", "action_type", "amount", "target_doc_type", "target_doc_no", "created_at")
    list_filter = ("action_type", "target_doc_type")
    search_fields = ("transaction_no", "target_doc_no")
