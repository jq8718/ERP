from django.contrib import admin

from .models import (
    Customer,
    CustomerAddress,
    CustomerProduct,
    Material,
    MaterialSupplierPrice,
    MaterialUnitConversion,
    Supplier,
)


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("material_code", "material_name", "material_type", "base_unit", "qty_precision", "status")
    list_filter = ("material_type", "status")
    search_fields = ("material_code", "material_name", "spec")


@admin.register(MaterialUnitConversion)
class MaterialUnitConversionAdmin(admin.ModelAdmin):
    list_display = ("material", "source_unit", "target_unit", "ratio", "status")
    list_filter = ("status",)
    search_fields = ("material__material_code", "material__material_name", "source_unit", "target_unit")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("customer_no", "customer_name", "short_name", "sales_owner", "status")
    list_filter = ("status", "sales_owner")
    search_fields = ("customer_no", "customer_name", "short_name")


@admin.register(CustomerProduct)
class CustomerProductAdmin(admin.ModelAdmin):
    list_display = ("customer", "customer_product_no", "customer_product_name", "finished_material", "status")
    list_filter = ("status",)
    search_fields = ("customer__customer_name", "customer_product_no", "customer_product_name", "finished_material__material_code")


@admin.register(CustomerAddress)
class CustomerAddressAdmin(admin.ModelAdmin):
    list_display = ("customer", "receiver_name", "is_default", "status")
    list_filter = ("status", "is_default")
    search_fields = ("customer__customer_name", "receiver_name")


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("supplier_no", "supplier_name", "supplier_type", "status")
    list_filter = ("status", "supplier_type")
    search_fields = ("supplier_no", "supplier_name", "contact_name")


@admin.register(MaterialSupplierPrice)
class MaterialSupplierPriceAdmin(admin.ModelAdmin):
    list_display = ("material", "supplier", "purchase_price", "currency", "is_default", "status")
    list_filter = ("status", "is_default", "currency")
    search_fields = ("material__material_code", "material__material_name", "supplier__supplier_name")
