from decimal import Decimal

from django import forms
from django.db.models import Q, Sum
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Material, MaterialSupplierPrice, Supplier
from system.display import set_form_labels
from system.services import next_document_no

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


class PurchaseRequestForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequest
        fields = ["needed_date", "remark"]
        widgets = {
            "needed_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["needed_date"].initial = self.fields["needed_date"].initial or timezone.localdate()

    def save(self, commit=True, user=None):
        request = super().save(commit=False)
        if not request.purchase_request_no:
            request.purchase_request_no = next_document_no("PR")
        if not request.source_type:
            request.source_type = PurchaseRequest.SourceType.MANUAL
        if not request.status:
            request.status = PurchaseRequest.Status.DRAFT
        if user and user.is_authenticated and not request.requested_by_id:
            request.requested_by = user
        if commit:
            request.save()
            self.save_m2m()
        return request


class PurchaseRequestItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequestItem
        fields = ["material", "request_qty", "suggested_supplier", "needed_date"]
        widgets = {"needed_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["material"].queryset = Material.objects.filter(status=Material.MaterialStatus.ACTIVE).order_by("material_code")
        self.fields["suggested_supplier"].queryset = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by("supplier_no")
        self.fields["suggested_supplier"].required = False

    def clean(self):
        cleaned = super().clean()
        request_qty = cleaned.get("request_qty")
        if request_qty is not None and request_qty <= 0:
            self.add_error("request_qty", "需求数量必须大于 0")
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if not item.line_status:
            item.line_status = PurchaseRequestItem.LineStatus.OPEN
        if commit:
            item.save()
        return item


class BasePurchaseRequestItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("material")
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条采购需求明细")

        seen_materials = set()
        for form in active_forms:
            material = form.cleaned_data["material"]
            if material.id in seen_materials:
                form.add_error("material", "同一采购需求中同一物料不能重复")
            seen_materials.add(material.id)

    def save(self, commit=True):
        super().save(commit=False)
        for obj in self.deleted_objects:
            if obj.pk:
                obj.delete()

        saved = []
        line_no = 1
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE") or not form.cleaned_data.get("material"):
                continue
            item = form.save(commit=False)
            item.purchase_request = self.instance
            item.line_no = line_no + 10000
            if not item.needed_date:
                item.needed_date = self.instance.needed_date
            if commit:
                item.save()
            saved.append(item)
            line_no += 1
        if commit:
            for line_no, item in enumerate(saved, start=1):
                item.line_no = line_no
                item.save(update_fields=["line_no"])
        return saved


PurchaseRequestItemFormSet = inlineformset_factory(
    PurchaseRequest,
    PurchaseRequestItem,
    form=PurchaseRequestItemForm,
    formset=BasePurchaseRequestItemFormSet,
    fields=["material", "request_qty", "suggested_supplier", "needed_date"],
    extra=3,
    can_delete=True,
)


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "order_date", "remark"]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["supplier"].queryset = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by("supplier_no")
        self.fields["order_date"].initial = self.fields["order_date"].initial or timezone.localdate()

    def save(self, commit=True, user=None):
        order = super().save(commit=False)
        if not order.purchase_order_no:
            order.purchase_order_no = next_document_no("PO")
        if not order.status:
            order.status = PurchaseOrder.Status.DRAFT
        if user and user.is_authenticated and not order.created_by_id:
            order.created_by = user
        if commit:
            order.save()
            self.save_m2m()
        return order


class PurchaseOrderItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderItem
        fields = ["material", "order_qty", "unit_price", "needed_date"]
        widgets = {"needed_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        self.supplier = kwargs.pop("supplier", None)
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["material"].queryset = Material.objects.filter(status=Material.MaterialStatus.ACTIVE).order_by("material_code")
        self.fields["unit_price"].required = False

    def clean(self):
        cleaned = super().clean()
        material = cleaned.get("material")
        order_qty = cleaned.get("order_qty")
        unit_price = cleaned.get("unit_price")
        if order_qty is not None and order_qty <= 0:
            self.add_error("order_qty", "采购数量必须大于 0")
        if material and not self.can_edit_amount:
            if self.instance and self.instance.pk and self.instance.material_id == material.id:
                cleaned["unit_price"] = self.instance.unit_price
            else:
                cleaned["unit_price"] = _default_purchase_price(material, self.supplier)
        else:
            if unit_price is not None and unit_price < 0:
                self.add_error("unit_price", "采购单价不能小于 0")
            if material and unit_price in [None, ""]:
                cleaned["unit_price"] = _default_purchase_price(material, self.supplier)
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        item.line_amount = _money(item.order_qty * item.unit_price)
        if not item.line_status:
            item.line_status = PurchaseOrderItem.LineStatus.OPEN
        if commit:
            item.save()
        return item


class BasePurchaseOrderItemFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        self.supplier = kwargs.pop("supplier", None)
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["supplier"] = self.supplier
        kwargs["can_edit_amount"] = self.can_edit_amount
        return super()._construct_form(i, **kwargs)

    def clean(self):
        super().clean()
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("material")
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条采购明细")

        seen_materials = set()
        for form in active_forms:
            material = form.cleaned_data["material"]
            if material.id in seen_materials:
                form.add_error("material", "同一采购单中同一物料不能重复")
            seen_materials.add(material.id)

    def save(self, commit=True):
        super().save(commit=False)
        for obj in self.deleted_objects:
            if obj.pk:
                obj.delete()

        saved = []
        line_no = 1
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE") or not form.cleaned_data.get("material"):
                continue
            item = form.save(commit=False)
            item.purchase_order = self.instance
            item.line_no = line_no + 10000
            if commit:
                item.save()
            saved.append(item)
            line_no += 1
        if commit:
            for line_no, item in enumerate(saved, start=1):
                item.line_no = line_no
                item.save(update_fields=["line_no"])
        return saved


PurchaseOrderItemFormSet = inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderItem,
    form=PurchaseOrderItemForm,
    formset=BasePurchaseOrderItemFormSet,
    fields=["material", "order_qty", "unit_price", "needed_date"],
    extra=3,
    can_delete=True,
)


class PurchaseReceiptForm(forms.ModelForm):
    class Meta:
        model = PurchaseReceipt
        fields = ["receipt_date", "remark"]
        widgets = {
            "receipt_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)


class PurchaseReceiptItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseReceiptItem
        fields = ["received_qty", "accepted_qty", "rejected_qty", "location"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")

    def clean(self):
        cleaned = super().clean()
        received_qty = cleaned.get("received_qty")
        accepted_qty = cleaned.get("accepted_qty")
        rejected_qty = cleaned.get("rejected_qty") or Decimal("0")
        if received_qty is not None and received_qty <= 0:
            self.add_error("received_qty", "到货数量必须大于 0")
        if accepted_qty is not None and accepted_qty < 0:
            self.add_error("accepted_qty", "合格数量不能小于 0")
        if rejected_qty < 0:
            self.add_error("rejected_qty", "不合格数量不能小于 0")
        if received_qty is not None and accepted_qty is not None and accepted_qty + rejected_qty > received_qty:
            raise forms.ValidationError("合格数量与不合格数量之和不能超过到货数量")

        order_item = self.instance.purchase_order_item if self.instance and self.instance.pk else None
        if order_item and accepted_qty is not None:
            remaining_qty = order_item.order_qty - order_item.received_qty
            if accepted_qty > remaining_qty:
                self.add_error("accepted_qty", "合格数量不能超过采购行剩余未到货数量")
        return cleaned


class BasePurchaseReceiptItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        active_forms = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")]
        if not active_forms:
            raise forms.ValidationError("进货单至少需要一条入库明细")


PurchaseReceiptItemFormSet = inlineformset_factory(
    PurchaseReceipt,
    PurchaseReceiptItem,
    form=PurchaseReceiptItemForm,
    formset=BasePurchaseReceiptItemFormSet,
    fields=["received_qty", "accepted_qty", "rejected_qty", "location"],
    extra=0,
    can_delete=False,
)


def recalculate_purchase_order_total(order: PurchaseOrder) -> None:
    total = sum((item.line_amount for item in order.items.all()), Decimal("0.00"))
    order.total_amount = _money(total)
    order.save(update_fields=["total_amount"])


class SupplierReturnForm(forms.ModelForm):
    class Meta:
        model = SupplierReturn
        fields = ["supplier", "purchase_receipt", "return_date", "remark"]
        widgets = {
            "return_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["supplier"].queryset = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by("supplier_no")
        receipt_filter = Q(status__in=[PurchaseReceipt.Status.PARTIAL_RECEIVED, PurchaseReceipt.Status.RECEIVED])
        if self.instance and self.instance.pk and self.instance.purchase_receipt_id:
            receipt_filter |= Q(id=self.instance.purchase_receipt_id)
        self.fields["purchase_receipt"].queryset = (
            PurchaseReceipt.objects.select_related("supplier", "purchase_order")
            .filter(receipt_filter)
            .order_by("-receipt_date", "-id")
        )
        self.fields["purchase_receipt"].required = False
        self.fields["return_date"].initial = self.fields["return_date"].initial or timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        supplier = cleaned.get("supplier")
        receipt = cleaned.get("purchase_receipt")
        if supplier and receipt and receipt.supplier_id != supplier.id:
            self.add_error("purchase_receipt", "来源进货单必须属于所选供应商")
        return cleaned

    def save(self, commit=True, user=None):
        supplier_return = super().save(commit=False)
        if not supplier_return.supplier_return_no:
            supplier_return.supplier_return_no = next_document_no("SR")
        if not supplier_return.status:
            supplier_return.status = SupplierReturn.Status.DRAFT
        if user and user.is_authenticated and not supplier_return.created_by_id:
            supplier_return.created_by = user
        if commit:
            supplier_return.save()
            self.save_m2m()
        return supplier_return


class SupplierReturnItemForm(forms.ModelForm):
    class Meta:
        model = SupplierReturnItem
        fields = ["purchase_receipt_item", "material", "return_qty", "unit_price", "batch", "location", "return_reason"]
        widgets = {"return_reason": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        self.supplier = kwargs.pop("supplier", None)
        self.purchase_receipt = kwargs.pop("purchase_receipt", None)
        self.require_ready = kwargs.pop("require_ready", False)
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        receipt_item_filter = Q(
            accepted_qty__gt=0,
            purchase_receipt__status__in=[PurchaseReceipt.Status.PARTIAL_RECEIVED, PurchaseReceipt.Status.RECEIVED],
        )
        if self.instance and self.instance.pk and self.instance.purchase_receipt_item_id:
            receipt_item_filter |= Q(id=self.instance.purchase_receipt_item_id)
        receipt_item_queryset = (
            PurchaseReceiptItem.objects.select_related("purchase_receipt", "material", "batch", "location")
            .filter(receipt_item_filter)
            .order_by("-purchase_receipt__receipt_date", "-purchase_receipt_id", "id")
        )
        if self.purchase_receipt:
            receipt_item_queryset = receipt_item_queryset.filter(purchase_receipt=self.purchase_receipt)
        elif self.supplier:
            receipt_item_queryset = receipt_item_queryset.filter(purchase_receipt__supplier=self.supplier)
        else:
            receipt_item_queryset = receipt_item_queryset.none()
        self.fields["purchase_receipt_item"].queryset = receipt_item_queryset
        self.fields["purchase_receipt_item"].required = False
        self.fields["material"].queryset = Material.objects.filter(status=Material.MaterialStatus.ACTIVE).order_by("material_code")
        self.fields["material"].required = False
        self.fields["unit_price"].required = False
        batch_filter = Q(
            inventory_type=InventoryBatch.InventoryType.AVAILABLE,
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
            remaining_qty__gt=0,
        )
        if self.instance and self.instance.pk and self.instance.batch_id:
            batch_filter |= Q(id=self.instance.batch_id)
        self.fields["batch"].queryset = (
            InventoryBatch.objects.select_related("material", "location")
            .filter(batch_filter)
            .order_by("material__material_code", "location__location_code", "received_at", "batch_no")
        )
        self.fields["batch"].required = False
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
        self.fields["location"].required = False

    def clean(self):
        cleaned = super().clean()
        receipt_item = cleaned.get("purchase_receipt_item")
        material = cleaned.get("material")
        return_qty = cleaned.get("return_qty")
        unit_price = cleaned.get("unit_price")
        batch = cleaned.get("batch")
        location = cleaned.get("location")

        if receipt_item:
            if self.supplier and receipt_item.purchase_receipt.supplier_id != self.supplier.id:
                self.add_error("purchase_receipt_item", "退货来源行必须属于所选供应商")
            if self.purchase_receipt and receipt_item.purchase_receipt_id != self.purchase_receipt.id:
                self.add_error("purchase_receipt_item", "退货来源行必须属于所选进货单")
            if material and material.id != receipt_item.material_id:
                self.add_error("material", "退货物料必须与来源进货行物料一致")
            cleaned["material"] = receipt_item.material
            if not self.can_edit_amount:
                if self.instance and self.instance.pk and self.instance.purchase_receipt_item_id == receipt_item.id:
                    cleaned["unit_price"] = self.instance.unit_price
                else:
                    cleaned["unit_price"] = receipt_item.unit_price
            elif unit_price in [None, ""]:
                cleaned["unit_price"] = receipt_item.unit_price
            if not batch and receipt_item.batch_id:
                cleaned["batch"] = receipt_item.batch
                batch = receipt_item.batch
            if not location and receipt_item.location_id:
                cleaned["location"] = receipt_item.location
                location = receipt_item.location
        elif not material:
            self.add_error("material", "未选择来源进货行时必须填写退货物料")
        elif not self.can_edit_amount:
            if self.instance and self.instance.pk and self.instance.material_id == material.id:
                cleaned["unit_price"] = self.instance.unit_price
            else:
                cleaned["unit_price"] = Decimal("0")

        if return_qty is not None and return_qty <= 0:
            self.add_error("return_qty", "退货数量必须大于 0")

        if self.can_edit_amount:
            if unit_price is not None and unit_price < 0:
                self.add_error("unit_price", "退货单价不能小于 0")
            elif cleaned.get("unit_price") in [None, ""]:
                cleaned["unit_price"] = Decimal("0")

        if self.require_ready and not batch:
            self.add_error("batch", "提交审核前必须选择退货批次")
        if self.require_ready and not location:
            self.add_error("location", "提交审核前必须选择退货库位")

        if batch:
            effective_material = cleaned.get("material") or material
            if effective_material and batch.material_id != effective_material.id:
                self.add_error("batch", "批次物料必须与退货物料一致")
            if location and batch.location_id != location.id:
                self.add_error("location", "库位必须与批次库位一致")
            if return_qty is not None and return_qty > batch.remaining_qty:
                self.add_error("return_qty", "退货数量不能超过批次剩余数量")
            cleaned["location"] = batch.location

        if receipt_item and return_qty is not None and return_qty > 0:
            returned_qty = (
                SupplierReturnItem.objects.filter(purchase_receipt_item=receipt_item)
                .exclude(supplier_return__status=SupplierReturn.Status.VOIDED)
                .exclude(pk=self.instance.pk)
                .aggregate(total=Sum("return_qty"))
                .get("total")
                or Decimal("0")
            )
            max_return_qty = receipt_item.accepted_qty - returned_qty
            if return_qty > max_return_qty:
                self.add_error("return_qty", f"退货数量不能超过可退数量 {max_return_qty}")

        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if item.purchase_receipt_item_id:
            item.material = item.purchase_receipt_item.material
            if item.unit_price in [None, ""]:
                item.unit_price = item.purchase_receipt_item.unit_price
            if not item.batch_id and item.purchase_receipt_item.batch_id:
                item.batch = item.purchase_receipt_item.batch
            if not item.location_id:
                item.location = item.batch.location if item.batch_id else item.purchase_receipt_item.location
        item.return_amount = _money(item.return_qty * item.unit_price)
        if commit:
            item.save()
        return item


class BaseSupplierReturnItemFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        self.supplier = kwargs.pop("supplier", None)
        self.purchase_receipt = kwargs.pop("purchase_receipt", None)
        self.require_ready = kwargs.pop("require_ready", False)
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["supplier"] = self.supplier
        kwargs["purchase_receipt"] = self.purchase_receipt
        kwargs["require_ready"] = self.require_ready
        kwargs["can_edit_amount"] = self.can_edit_amount
        return super()._construct_form(i, **kwargs)

    def clean(self):
        super().clean()
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and (form.cleaned_data.get("purchase_receipt_item") or form.cleaned_data.get("material"))
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条供应商退货明细")

        seen_items = set()
        for form in active_forms:
            receipt_item = form.cleaned_data.get("purchase_receipt_item")
            material = form.cleaned_data.get("material")
            key = (receipt_item.id if receipt_item else None, material.id if material else None)
            if key in seen_items:
                form.add_error("material", "同一退货单中相同来源行和物料不能重复")
            seen_items.add(key)

    def save(self, commit=True):
        super().save(commit=False)
        for obj in self.deleted_objects:
            if obj.pk:
                obj.delete()

        saved = []
        for form in self.forms:
            if (
                not form.cleaned_data
                or form.cleaned_data.get("DELETE")
                or not (form.cleaned_data.get("purchase_receipt_item") or form.cleaned_data.get("material"))
            ):
                continue
            item = form.save(commit=False)
            item.supplier_return = self.instance
            if commit:
                item.save()
            saved.append(item)
        return saved


SupplierReturnItemFormSet = inlineformset_factory(
    SupplierReturn,
    SupplierReturnItem,
    form=SupplierReturnItemForm,
    formset=BaseSupplierReturnItemFormSet,
    fields=["purchase_receipt_item", "material", "return_qty", "unit_price", "batch", "location", "return_reason"],
    extra=3,
    can_delete=True,
)


def recalculate_supplier_return_total(supplier_return: SupplierReturn) -> None:
    total = sum((item.return_amount for item in supplier_return.items.all()), Decimal("0.00"))
    supplier_return.return_amount = _money(total)
    supplier_return.save(update_fields=["return_amount"])


def _default_purchase_price(material: Material, supplier: Supplier | None) -> Decimal:
    if supplier:
        price = (
            MaterialSupplierPrice.objects.filter(
                material=material,
                supplier=supplier,
                status=MaterialSupplierPrice.PriceStatus.ACTIVE,
            )
            .order_by("-is_default", "-effective_from", "-id")
            .first()
        )
        if price:
            return price.purchase_price
    return material.latest_purchase_price or Decimal("0")


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))
