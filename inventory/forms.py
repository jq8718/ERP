from django import forms

from system.services import next_document_no

from .models import InventoryBatch, LocationTransfer, StockCount, WarehouseLocation


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
        self.fields["scope_type"].initial = "batch"
        self.fields["scope_value"].required = False
        self.fields["scope_type"].help_text = "MVP 建议使用 batch，按库存批次生成快照"
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")


class LocationTransferForm(forms.ModelForm):
    class Meta:
        model = LocationTransfer
        fields = ["batch", "to_location", "transfer_qty"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
