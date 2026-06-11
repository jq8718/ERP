from django.contrib import admin

from .models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)


@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = ("production_order_no", "sales_order_item", "finished_material", "production_qty", "received_qty", "status")
    list_filter = ("status", "finished_material")
    search_fields = ("production_order_no", "finished_material__material_code", "sales_order_item__sales_order__sales_order_no")


@admin.register(ProductionMaterialRequisition)
class ProductionMaterialRequisitionAdmin(admin.ModelAdmin):
    list_display = ("requisition_no", "production_order", "requisition_date", "status")
    list_filter = ("status", "requisition_date")
    search_fields = ("requisition_no", "production_order__production_order_no")


@admin.register(ProductionMaterialRequisitionItem)
class ProductionMaterialRequisitionItemAdmin(admin.ModelAdmin):
    list_display = ("requisition", "line_no", "material", "required_qty", "issued_qty", "batch", "location")
    list_filter = ("location",)
    search_fields = ("requisition__requisition_no", "material__material_code", "batch__batch_no")


@admin.register(ProductionReceipt)
class ProductionReceiptAdmin(admin.ModelAdmin):
    list_display = ("production_receipt_no", "production_order", "receipt_date", "status")
    list_filter = ("status", "receipt_date")
    search_fields = ("production_receipt_no", "production_order__production_order_no")


@admin.register(ProductionReceiptItem)
class ProductionReceiptItemAdmin(admin.ModelAdmin):
    list_display = ("production_receipt", "line_no", "finished_material", "receipt_qty", "location", "quality_status")
    list_filter = ("quality_status", "location")
    search_fields = ("production_receipt__production_receipt_no", "finished_material__material_code", "batch_no")
