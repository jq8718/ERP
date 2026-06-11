from django.contrib import admin

from .models import (
    Inventory,
    InventoryBatch,
    InventoryTransaction,
    LocationTransfer,
    StockCount,
    StockCountItem,
    WarehouseLocation,
)


@admin.register(WarehouseLocation)
class WarehouseLocationAdmin(admin.ModelAdmin):
    list_display = ("location_code", "location_name", "status")
    list_filter = ("status",)
    search_fields = ("location_code", "location_name")


@admin.register(InventoryBatch)
class InventoryBatchAdmin(admin.ModelAdmin):
    list_display = ("batch_no", "material", "location", "inventory_type", "remaining_qty", "batch_status", "received_at")
    list_filter = ("inventory_type", "batch_status", "location")
    search_fields = ("batch_no", "material__material_code", "material__material_name")


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("material", "location", "inventory_type", "qty", "updated_at")
    list_filter = ("inventory_type", "location")
    search_fields = ("material__material_code", "material__material_name")


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_no", "transaction_type", "material", "location", "qty_delta", "source_doc_type", "created_at")
    list_filter = ("transaction_type", "location", "created_at")
    search_fields = ("transaction_no", "material__material_code", "source_doc_no")


@admin.register(LocationTransfer)
class LocationTransferAdmin(admin.ModelAdmin):
    list_display = ("transfer_no", "material", "batch", "from_location", "to_location", "transfer_qty", "status")
    list_filter = ("status", "from_location", "to_location")
    search_fields = ("transfer_no", "material__material_code", "batch__batch_no")


@admin.register(StockCount)
class StockCountAdmin(admin.ModelAdmin):
    list_display = ("stock_count_no", "scope_type", "scope_value", "snapshot_at", "status", "created_at")
    list_filter = ("status", "scope_type")
    search_fields = ("stock_count_no", "scope_value")


@admin.register(StockCountItem)
class StockCountItemAdmin(admin.ModelAdmin):
    list_display = ("stock_count", "material", "batch", "location", "book_qty", "counted_qty", "difference_qty")
    list_filter = ("location",)
    search_fields = ("stock_count__stock_count_no", "material__material_code", "batch__batch_no")
