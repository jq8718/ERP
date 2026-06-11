from django.contrib import admin

from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseRequest,
    PurchaseRequestItem,
    SupplierReturn,
    SupplierReturnItem,
)


class PurchaseRequestItemInline(admin.TabularInline):
    model = PurchaseRequestItem
    extra = 0


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = ("purchase_request_no", "source_type", "status", "requested_by", "needed_date", "created_at")
    list_filter = ("source_type", "status", "needed_date")
    search_fields = ("purchase_request_no", "remark")
    inlines = [PurchaseRequestItemInline]


@admin.register(PurchaseRequestItem)
class PurchaseRequestItemAdmin(admin.ModelAdmin):
    list_display = ("purchase_request", "line_no", "material", "request_qty", "suggested_supplier", "line_status")
    list_filter = ("line_status", "suggested_supplier")
    search_fields = ("purchase_request__purchase_request_no", "material__material_code", "material__material_name")


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("purchase_order_no", "supplier", "order_date", "status", "total_amount")
    list_filter = ("status", "order_date", "supplier")
    search_fields = ("purchase_order_no", "supplier__supplier_name")


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "line_no", "material", "order_qty", "received_qty", "unit_price", "line_status")
    list_filter = ("line_status",)
    search_fields = ("purchase_order__purchase_order_no", "material__material_code")


@admin.register(PurchaseReceipt)
class PurchaseReceiptAdmin(admin.ModelAdmin):
    list_display = ("purchase_receipt_no", "purchase_order", "supplier", "receipt_date", "status")
    list_filter = ("status", "receipt_date", "supplier")
    search_fields = ("purchase_receipt_no", "purchase_order__purchase_order_no", "supplier__supplier_name")


@admin.register(PurchaseReceiptItem)
class PurchaseReceiptItemAdmin(admin.ModelAdmin):
    list_display = ("purchase_receipt", "purchase_order_item", "material", "received_qty", "accepted_qty", "rejected_qty", "location")
    list_filter = ("location",)
    search_fields = ("purchase_receipt__purchase_receipt_no", "material__material_code")


@admin.register(SupplierReturn)
class SupplierReturnAdmin(admin.ModelAdmin):
    list_display = ("supplier_return_no", "supplier", "purchase_receipt", "return_date", "status", "return_amount")
    list_filter = ("status", "return_date", "supplier")
    search_fields = ("supplier_return_no", "supplier__supplier_name", "purchase_receipt__purchase_receipt_no")


@admin.register(SupplierReturnItem)
class SupplierReturnItemAdmin(admin.ModelAdmin):
    list_display = ("supplier_return", "purchase_receipt_item", "material", "return_qty", "return_amount")
    search_fields = ("supplier_return__supplier_return_no", "material__material_code")
