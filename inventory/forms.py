from django import forms
from django.utils import timezone

from masterdata.models import Material
from system.display import set_form_labels
from system.services import next_document_no

from .models import InventoryBatch, LocationTransfer, StockCount, WarehouseLocation


class InitialInventoryManualForm(forms.Form):
    material = forms.ModelChoiceField(queryset=Material.objects.none(), label="物料")
    location = forms.ModelChoiceField(queryset=WarehouseLocation.objects.none(), label="库位")
    batch_no = forms.CharField(label="批次号", required=False, max_length=100)
    inventory_type = forms.ChoiceField(label="库存类型", choices=InventoryBatch.InventoryType.choices)
    initial_qty = forms.DecimalField(label="期初数量", max_digits=14, decimal_places=4, min_value=0)
    cost_price = forms.DecimalField(label="成本单价", required=False, max_digits=14, decimal_places=6, min_value=0)
    received_at = forms.DateField(label="入库日期", required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["material"].queryset = Material.objects.filter(status=Material.MaterialStatus.ACTIVE).order_by("material_code")
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
        self.fields["inventory_type"].initial = InventoryBatch.InventoryType.AVAILABLE
        self.fields["received_at"].initial = timezone.localdate()
        set_form_labels(self)

    def clean_initial_qty(self):
        value = self.cleaned_data["initial_qty"]
        if value <= 0:
            raise forms.ValidationError("期初数量必须大于 0")
        return value

    def to_import_row(self) -> dict[str, str]:
        cleaned = self.cleaned_data
        return {
            "material_code": cleaned["material"].material_code,
            "location_code": cleaned["location"].location_code,
            "batch_no": cleaned.get("batch_no") or "",
            "inventory_type": cleaned.get("inventory_type") or InventoryBatch.InventoryType.AVAILABLE,
            "initial_qty": str(cleaned["initial_qty"]),
            "cost_price": str(cleaned["cost_price"]) if cleaned.get("cost_price") is not None else "",
            "received_at": cleaned["received_at"].isoformat() if cleaned.get("received_at") else "",
        }


class StockCountForm(forms.ModelForm):
    location = forms.ModelChoiceField(
        queryset=WarehouseLocation.objects.none(),
        required=False,
        label="限定库位",
        help_text="不选则盘点全部库位的在库批次",
    )

    class Meta:
        model = StockCount
        fields = ["scope_type", "scope_value"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["scope_type"].initial = "batch"
        self.fields["scope_value"].required = False
        self.fields["scope_type"].help_text = "建议使用“按批次”，系统会按库存批次生成快照"
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")


class LocationTransferForm(forms.ModelForm):
    class Meta:
        model = LocationTransfer
        fields = ["batch", "to_location", "transfer_qty"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["batch"].queryset = (
            InventoryBatch.objects.filter(
                batch_status=InventoryBatch.BatchStatus.IN_STOCK,
                remaining_qty__gt=0,
            )
            .select_related("material", "location")
            .order_by("material__material_code", "location__location_code", "batch_no")
        )
        self.fields["to_location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")

    def clean(self):
        cleaned = super().clean()
        batch = cleaned.get("batch")
        to_location = cleaned.get("to_location")
        transfer_qty = cleaned.get("transfer_qty")
        if batch and to_location and batch.location_id == to_location.id:
            self.add_error("to_location", "目标库位不能与原库位相同")
        if transfer_qty is not None and transfer_qty <= 0:
            self.add_error("transfer_qty", "移库数量必须大于 0")
        if batch and transfer_qty and batch.remaining_qty < transfer_qty:
            self.add_error("transfer_qty", "移库数量不能超过批次剩余数量")
        return cleaned

    def save(self, commit=True):
        transfer = super().save(commit=False)
        if not transfer.transfer_no:
            transfer.transfer_no = next_document_no("LT")
        if transfer.batch_id:
            transfer.material = transfer.batch.material
            transfer.from_location = transfer.batch.location
        if commit:
            transfer.save()
        return transfer
