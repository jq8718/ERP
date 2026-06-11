from django import forms
from django.utils import timezone

from bom.models import Bom
from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material
from system.services import next_document_no

from .models import (
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    ProductionOrder,
    ProductionReceipt,
    ProductionReceiptItem,
)


class ProductionOrderForm(forms.ModelForm):
    class Meta:
        model = ProductionOrder
        fields = ["finished_material", "production_qty", "locked_bom", "planned_start_date", "planned_finish_date", "remark"]
        widgets = {
            "planned_start_date": forms.DateInput(attrs={"type": "date"}),
            "planned_finish_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["finished_material"].queryset = Material.objects.filter(
            status=Material.MaterialStatus.ACTIVE,
            material_type=Material.MaterialType.FINISHED,
        ).order_by("material_code")
        self.fields["locked_bom"].queryset = Bom.objects.select_related("finished_material").filter(
            status=Bom.BomStatus.ENABLED
        ).order_by("finished_material__material_code", "-is_default", "-enabled_at", "-id")

    def clean(self):
        cleaned = super().clean()
        finished_material = cleaned.get("finished_material")
        production_qty = cleaned.get("production_qty")
        locked_bom = cleaned.get("locked_bom")
        planned_start_date = cleaned.get("planned_start_date")
        planned_finish_date = cleaned.get("planned_finish_date")
        if production_qty is not None and production_qty <= 0:
            self.add_error("production_qty", "生产数量必须大于 0")
        if finished_material and locked_bom and locked_bom.finished_material_id != finished_material.id:
            self.add_error("locked_bom", "BOM 对应成品必须与生产成品一致")
        if planned_start_date and planned_finish_date and planned_finish_date < planned_start_date:
            self.add_error("planned_finish_date", "计划完成日期不能早于计划开始日期")
        return cleaned

    def save(self, commit=True, user=None):
        order = super().save(commit=False)
        if not order.production_order_no:
            order.production_order_no = next_document_no("MO")
        if order.locked_bom_id:
            order.locked_bom_version = order.locked_bom.bom_version
        if not order.status:
            order.status = ProductionOrder.Status.PENDING
        if user and user.is_authenticated:
            if not order.created_by_id:
                order.created_by = user
            order.updated_by = user
        if not order.planned_start_date:
            order.planned_start_date = timezone.localdate()
        if commit:
            order.save()
            self.save_m2m()
        return order


class ProductionMaterialRequisitionForm(forms.ModelForm):
    class Meta:
        model = ProductionMaterialRequisition
        fields = ["requisition_date", "remark"]
        widgets = {
            "requisition_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }


class ProductionMaterialRequisitionItemForm(forms.ModelForm):
    class Meta:
        model = ProductionMaterialRequisitionItem
        fields = ["issued_qty", "batch", "location", "adjust_reason"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = (
            InventoryBatch.objects.select_related("material", "location")
            .filter(
                inventory_type=InventoryBatch.InventoryType.AVAILABLE,
                batch_status=InventoryBatch.BatchStatus.IN_STOCK,
                remaining_qty__gt=0,
            )
            .order_by("material__material_code", "received_at", "batch_no")
        )
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")

    def clean(self):
        cleaned = super().clean()
        issued_qty = cleaned.get("issued_qty")
        batch = cleaned.get("batch")
        location = cleaned.get("location")
        if issued_qty is not None and issued_qty < 0:
            self.add_error("issued_qty", "实领数量不能小于 0")
        if issued_qty is not None and issued_qty > self.instance.required_qty:
            self.add_error("issued_qty", "实领数量不能超过需求数量")
        if batch:
            if batch.material_id != self.instance.material_id:
                self.add_error("batch", "批次物料必须与领料物料一致")
            if location and batch.location_id != location.id:
                self.add_error("location", "库位必须与批次库位一致")
            if issued_qty is not None and issued_qty > batch.remaining_qty:
                self.add_error("issued_qty", "实领数量不能超过批次剩余数量")
            cleaned["location"] = batch.location
        return cleaned


ProductionMaterialRequisitionItemFormSet = forms.inlineformset_factory(
    ProductionMaterialRequisition,
    ProductionMaterialRequisitionItem,
    form=ProductionMaterialRequisitionItemForm,
    fields=["issued_qty", "batch", "location", "adjust_reason"],
    extra=0,
    can_delete=False,
)


class ProductionReceiptForm(forms.ModelForm):
    class Meta:
        model = ProductionReceipt
        fields = ["receipt_date", "remark"]
        widgets = {
            "receipt_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }


class ProductionReceiptItemForm(forms.ModelForm):
    class Meta:
        model = ProductionReceiptItem
        fields = ["receipt_qty", "location", "batch_no", "quality_status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
        self.fields["batch_no"].required = False

    def clean(self):
        cleaned = super().clean()
        receipt_qty = cleaned.get("receipt_qty")
        if receipt_qty is not None and receipt_qty <= 0:
            self.add_error("receipt_qty", "入库数量必须大于 0")
        production_order = self.instance.production_order if self.instance and self.instance.pk else None
        if production_order and receipt_qty is not None:
            remaining_qty = production_order.production_qty - production_order.received_qty
            if receipt_qty > remaining_qty:
                self.add_error("receipt_qty", "入库数量不能超过生产指令剩余未入库数量")
        return cleaned


ProductionReceiptItemFormSet = forms.inlineformset_factory(
    ProductionReceipt,
    ProductionReceiptItem,
    form=ProductionReceiptItemForm,
    fields=["receipt_qty", "location", "batch_no", "quality_status"],
    extra=0,
    can_delete=False,
)
