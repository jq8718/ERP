from django.contrib import admin

from .models import (
    CustomerReturn,
    CustomerReturnItem,
    SalesOrder,
    SalesOrderChangeLog,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
    SampleLoan,
    SampleLoanItem,
    SampleLoanReturn,
    SampleLoanReturnItem,
    ShortageAlert,
)


class SalesOrderItemInline(admin.TabularInline):
    model = SalesOrderItem
    extra = 0


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ("sales_order_no", "customer", "order_date", "delivery_date", "status", "total_amount")
    list_filter = ("status", "order_date", "delivery_date")
    search_fields = ("sales_order_no", "customer__customer_name")
    inlines = [SalesOrderItemInline]


@admin.register(SalesOrderItem)
class SalesOrderItemAdmin(admin.ModelAdmin):
    list_display = ("sales_order", "line_no", "customer_product", "finished_material", "order_qty", "line_status", "inventory_check_status")
    list_filter = ("line_status", "inventory_check_status")
    search_fields = ("sales_order__sales_order_no", "finished_material__material_code", "customer_product__customer_product_name")


@admin.register(SalesOrderChangeLog)
class SalesOrderChangeLogAdmin(admin.ModelAdmin):
    list_display = ("sales_order", "changed_by", "changed_at")
    search_fields = ("sales_order__sales_order_no", "change_reason")


@admin.register(CustomerReturn)
class CustomerReturnAdmin(admin.ModelAdmin):
    list_display = ("return_no", "customer", "sales_order", "return_date", "status", "return_amount")
    list_filter = ("status", "return_date")
    search_fields = ("return_no", "customer__customer_name", "sales_order__sales_order_no")


@admin.register(CustomerReturnItem)
class CustomerReturnItemAdmin(admin.ModelAdmin):
    list_display = ("customer_return", "sales_order_item", "material", "return_qty", "return_amount")
    search_fields = ("customer_return__return_no", "material__material_code")


@admin.register(SampleLoan)
class SampleLoanAdmin(admin.ModelAdmin):
    list_display = ("sample_loan_no", "customer", "loan_date", "expected_return_date", "status", "overdue_status")
    list_filter = ("status", "overdue_status", "loan_date")
    search_fields = ("sample_loan_no", "customer__customer_name")


@admin.register(SampleLoanItem)
class SampleLoanItemAdmin(admin.ModelAdmin):
    list_display = ("sample_loan", "line_no", "material", "loan_qty", "returned_qty", "sold_qty", "line_status")
    list_filter = ("line_status",)
    search_fields = ("sample_loan__sample_loan_no", "material__material_code")


@admin.register(SampleLoanReturn)
class SampleLoanReturnAdmin(admin.ModelAdmin):
    list_display = ("sample_return_no", "sample_loan", "customer", "return_date", "status")
    list_filter = ("status", "return_date")
    search_fields = ("sample_return_no", "sample_loan__sample_loan_no", "customer__customer_name")


@admin.register(SampleLoanReturnItem)
class SampleLoanReturnItemAdmin(admin.ModelAdmin):
    list_display = ("sample_return", "sample_loan_item", "material", "return_qty", "location", "sample_condition")
    list_filter = ("sample_condition", "location")
    search_fields = ("sample_return__sample_return_no", "material__material_code")


@admin.register(SalesShipment)
class SalesShipmentAdmin(admin.ModelAdmin):
    list_display = ("shipment_no", "sales_order", "customer", "shipment_date", "status")
    list_filter = ("status", "shipment_date")
    search_fields = ("shipment_no", "sales_order__sales_order_no", "customer__customer_name")


@admin.register(SalesShipmentItem)
class SalesShipmentItemAdmin(admin.ModelAdmin):
    list_display = ("shipment", "sales_order_item", "material", "shipment_qty", "batch", "location")
    search_fields = ("shipment__shipment_no", "material__material_code", "batch__batch_no")


@admin.register(ShortageAlert)
class ShortageAlertAdmin(admin.ModelAdmin):
    list_display = ("shortage_no", "sales_order", "sales_order_item", "material", "shortage_qty", "is_required", "status")
    list_filter = ("status", "is_required")
    search_fields = ("shortage_no", "sales_order__sales_order_no", "material__material_code")
