from django.contrib import admin

from .models import Bom, BomItem


class BomItemInline(admin.TabularInline):
    model = BomItem
    extra = 0


@admin.register(Bom)
class BomAdmin(admin.ModelAdmin):
    list_display = ("bom_no", "finished_material", "bom_version", "status", "enabled_at", "created_at")
    list_filter = ("status",)
    search_fields = ("bom_no", "finished_material__material_code", "finished_material__material_name", "bom_version")
    inlines = [BomItemInline]


@admin.register(BomItem)
class BomItemAdmin(admin.ModelAdmin):
    list_display = ("bom", "line_no", "component_material", "usage_qty", "usage_unit", "loss_rate", "is_required")
    list_filter = ("is_required",)
    search_fields = ("bom__bom_no", "component_material__material_code", "component_material__material_name")
