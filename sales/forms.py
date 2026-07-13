from datetime import timedelta
from decimal import Decimal

from django import forms
from django.db.models import Q, Sum
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Customer, CustomerAddress, CustomerProduct, Material
from system.display import set_form_labels
from system.services import next_document_no

from .models import (
    CustomerReturn,
    CustomerReturnItem,
    SalesOrder,
    SalesOrderItem,
    SalesShipment,
    SalesShipmentItem,
    SampleLoan,
    SampleLoanItem,
    SampleLoanReturn,
    SampleLoanReturnItem,
)


def material_choice_label(material: Material) -> str:
    parts = [material.material_code, material.material_name]
    if material.spec:
        parts.append(f"规格型号：{material.spec}")
    return "｜".join(part for part in parts if part)


def sample_loan_batch_label(batch: InventoryBatch) -> str:
    material = batch.material
    parts = [material.material_code, material.material_name]
    if material.spec:
        parts.append(f"规格型号：{material.spec}")
    parts.append(f"批次：{batch.batch_no}")
    if batch.location_id:
        parts.append(f"库位：{batch.location.location_code}")
        if batch.location.location_name:
            parts.append(batch.location.location_name)
    parts.append(f"可用：{batch.remaining_qty}")
    return "｜".join(part for part in parts if part)


def customer_return_sales_order_label(order: SalesOrder) -> str:
    return f"{order.sales_order_no}｜{order.customer.customer_name}｜{order.order_date}｜{order.get_status_display()}"


def customer_return_sales_item_label(item: SalesOrderItem) -> str:
    material = item.finished_material
    parts = [material.material_code, material.material_name]
    if material.spec:
        parts.append(f"规格型号：{material.spec}")
    if item.customer_model_remark:
        parts.append(f"客户型号/备注：{item.customer_model_remark}")
    parts.append(f"已发：{item.shipped_qty}")
    parts.append(f"可退：{customer_returnable_qty(item)}")
    return "｜".join(part for part in parts if part)


def customer_returnable_qty(item: SalesOrderItem) -> Decimal:
    returned_qty = (
        CustomerReturnItem.objects.filter(sales_order_item=item)
        .exclude(customer_return__status=CustomerReturn.Status.VOIDED)
        .aggregate(total=Sum("return_qty"))
        .get("total")
        or Decimal("0")
    )
    return max(item.shipped_qty - returned_qty, Decimal("0"))


class SalesOrderForm(forms.ModelForm):
    submit_for_approval = forms.BooleanField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = SalesOrder
        fields = [
            "customer",
            "customer_address",
            "customer_contract_no",
            "settlement_method",
            "order_date",
            "delivery_date",
            "remark",
        ]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "delivery_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["customer"].queryset = Customer.objects.filter(status=Customer.CustomerStatus.ACTIVE).order_by("customer_no")
        self.fields["order_date"].initial = self.fields["order_date"].initial or timezone.localdate()
        self.fields["customer_address"].queryset = self._customer_address_queryset()
        self.fields["settlement_method"].required = False

    def _customer_address_queryset(self):
        customer_id = None
        if self.is_bound:
            customer_id = self.data.get(self.add_prefix("customer"))
        elif self.instance and self.instance.pk:
            customer_id = self.instance.customer_id
        if not customer_id:
            return CustomerAddress.objects.none()
        return (
            CustomerAddress.objects.select_related("customer")
            .filter(
                customer_id=customer_id,
                address_type=CustomerAddress.AddressType.SHIPPING,
                status=CustomerAddress.AddressStatus.ACTIVE,
            )
            .order_by("-is_default", "id")
        )

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get("customer")
        customer_address = cleaned.get("customer_address")
        if customer_address and customer and customer_address.customer_id != customer.id:
            self.add_error("customer_address", "收货地址必须属于所选客户")
        if customer and not customer_address and "customer_address" not in self.errors:
            cleaned["customer_address"] = (
                CustomerAddress.objects.filter(
                    customer=customer,
                    address_type=CustomerAddress.AddressType.SHIPPING,
                    status=CustomerAddress.AddressStatus.ACTIVE,
                )
                .order_by("-is_default", "id")
                .first()
            )
        return cleaned

    def save(self, commit=True, user=None):
        order = super().save(commit=False)
        if not order.sales_order_no:
            order.sales_order_no = next_document_no("SO")
        if not order.status:
            order.status = SalesOrder.Status.DRAFT
        if user and user.is_authenticated:
            if not order.created_by_id:
                order.created_by = user
            order.updated_by = user
        if commit:
            order.save()
            self.save_m2m()
        return order


class SalesOrderItemForm(forms.ModelForm):
    customer_product = forms.ModelChoiceField(
        queryset=CustomerProduct.objects.none(),
        required=False,
        widget=forms.HiddenInput,
    )

    class Meta:
        model = SalesOrderItem
        fields = ["finished_material", "customer_model_remark", "order_qty", "unit_price", "customer_product"]
        widgets = {"customer_model_remark": forms.TextInput(attrs={"placeholder": "可填写客户型号、客户图号或备注"})}

    def __init__(self, *args, **kwargs):
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["finished_material"].label = "产品/物料"
        self.fields["finished_material"].required = False
        self.fields["finished_material"].queryset = (
            Material.objects.filter(
                status=Material.MaterialStatus.ACTIVE,
                material_type=Material.MaterialType.FINISHED,
            )
            .order_by("material_code")
        )
        self.fields["finished_material"].label_from_instance = material_choice_label
        self.fields["customer_product"].queryset = (
            CustomerProduct.objects.select_related("customer", "finished_material")
            .filter(status=CustomerProduct.ProductStatus.ACTIVE, finished_material__isnull=False)
            .order_by("customer__customer_name", "customer_product_no")
        )
        if self.instance and self.instance.pk and self.instance.customer_product_id:
            self.fields["customer_product"].initial = self.instance.customer_product_id
        self.fields["unit_price"].required = False

    def clean(self):
        cleaned = super().clean()
        customer_product = cleaned.get("customer_product")
        finished_material = cleaned.get("finished_material")
        order_qty = cleaned.get("order_qty")
        unit_price = cleaned.get("unit_price")
        if not finished_material and customer_product:
            finished_material = customer_product.finished_material
            cleaned["finished_material"] = finished_material
            self._legacy_customer_product = customer_product
        else:
            self._legacy_customer_product = None
        if not finished_material and (order_qty or unit_price):
            self.add_error("finished_material", "产品/物料不能为空")
        if finished_material and finished_material.material_type != Material.MaterialType.FINISHED:
            self.add_error("finished_material", "销售订单只能选择成品物料")
        if order_qty is not None and order_qty <= 0:
            self.add_error("order_qty", "数量必须大于 0")
        if not self.can_edit_amount:
            if self.instance and self.instance.pk and self.instance.finished_material_id == getattr(finished_material, "id", None):
                cleaned["unit_price"] = self.instance.unit_price
            elif customer_product:
                cleaned["unit_price"] = customer_product.default_sale_price or Decimal("0")
            else:
                cleaned["unit_price"] = Decimal("0")
        else:
            if unit_price is not None and unit_price < 0:
                self.add_error("unit_price", "单价不能小于 0")
            if finished_material and unit_price in [None, ""]:
                cleaned["unit_price"] = (customer_product.default_sale_price if customer_product else None) or Decimal("0")
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        customer_product = getattr(self, "_legacy_customer_product", None)
        posted_customer_product = self.cleaned_data.get("customer_product")
        if customer_product:
            item.customer_product = customer_product
            item.finished_material = customer_product.finished_material
            if not item.customer_model_remark:
                item.customer_model_remark = f"{customer_product.customer_product_no} {customer_product.customer_product_name}".strip()
        elif posted_customer_product:
            if not item.customer_model_remark:
                item.customer_model_remark = f"{posted_customer_product.customer_product_no} {posted_customer_product.customer_product_name}".strip()
            if posted_customer_product.finished_material_id != item.finished_material_id:
                item.customer_product = None
        item.line_amount = _money(item.order_qty * item.unit_price)
        if item.sales_order.status == SalesOrder.Status.PENDING_APPROVAL:
            item.line_status = SalesOrderItem.LineStatus.PENDING_APPROVAL
        elif item.sales_order.status == SalesOrder.Status.DRAFT:
            item.line_status = SalesOrderItem.LineStatus.DRAFT
            item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.UNCHECKED
            item.locked_bom = None
            item.locked_bom_version = ""
        elif not item.line_status:
            item.line_status = SalesOrderItem.LineStatus.DRAFT
        if commit:
            item.save()
        return item


class BaseSalesOrderItemFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["can_edit_amount"] = self.can_edit_amount
        return super()._construct_form(i, **kwargs)

    def clean(self):
        super().clean()
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and (form.cleaned_data.get("finished_material") or form.cleaned_data.get("customer_product"))
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条销售订单明细")

        seen_finished_materials = set()
        for form in active_forms:
            finished_material = form.cleaned_data.get("finished_material")
            customer_product = form.cleaned_data.get("customer_product")
            material_id = finished_material.id if finished_material else customer_product.finished_material_id
            if material_id in seen_finished_materials:
                form.add_error("finished_material", "同一销售订单中产品/物料不能重复")
            seen_finished_materials.add(material_id)

    def save(self, commit=True):
        super().save(commit=False)
        for obj in self.deleted_objects:
            if obj.pk:
                obj.delete()

        saved = []
        line_no = 1
        for form in self.forms:
            if (
                not form.cleaned_data
                or form.cleaned_data.get("DELETE")
                or not (form.cleaned_data.get("finished_material") or form.cleaned_data.get("customer_product"))
            ):
                continue
            item = form.save(commit=False)
            item.sales_order = self.instance
            item.line_no = line_no + 10000
            if self.instance.status == SalesOrder.Status.PENDING_APPROVAL:
                item.line_status = SalesOrderItem.LineStatus.PENDING_APPROVAL
            elif self.instance.status == SalesOrder.Status.DRAFT:
                item.line_status = SalesOrderItem.LineStatus.DRAFT
                item.inventory_check_status = SalesOrderItem.InventoryCheckStatus.UNCHECKED
                item.locked_bom = None
                item.locked_bom_version = ""
            elif not item.line_status:
                item.line_status = SalesOrderItem.LineStatus.DRAFT
            if commit:
                item.save()
            saved.append(item)
            line_no += 1
        if commit:
            for line_no, item in enumerate(saved, start=1):
                item.line_no = line_no
                item.save(update_fields=["line_no"])
        return saved


SalesOrderItemFormSet = inlineformset_factory(
    SalesOrder,
    SalesOrderItem,
    form=SalesOrderItemForm,
    formset=BaseSalesOrderItemFormSet,
    fields=["finished_material", "customer_model_remark", "order_qty", "unit_price", "customer_product"],
    extra=1,
    can_delete=True,
)


def recalculate_sales_order_total(order: SalesOrder) -> None:
    total = sum((item.line_amount for item in order.items.all()), Decimal("0.00"))
    order.total_amount = _money(total)
    order.save(update_fields=["total_amount", "updated_at"])


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


class CustomerReturnForm(forms.ModelForm):
    show_all_orders = forms.BooleanField(required=False, label="显示全部销售订单")

    class Meta:
        model = CustomerReturn
        fields = ["customer", "sales_order", "return_date", "remark"]
        widgets = {
            "return_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        sales_order_queryset = kwargs.pop("sales_order_queryset", None)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        show_all_orders = self._show_all_orders()
        selected_order_id = self._selected_sales_order_id()
        self.fields["customer"].queryset = Customer.objects.filter(status=Customer.CustomerStatus.ACTIVE).order_by("customer_no")
        self.fields["customer"].required = False
        if sales_order_queryset is None:
            sales_order_queryset = (
                SalesOrder.objects.select_related("customer")
                .filter(status__in=[SalesOrder.Status.SHIPPED, SalesOrder.Status.COMPLETED], items__shipped_qty__gt=0)
                .distinct()
            )
        if not show_all_orders:
            recent_from = timezone.localdate() - timedelta(days=7)
            date_filter = Q(order_date__gte=recent_from)
            if selected_order_id:
                date_filter |= Q(pk=selected_order_id)
            sales_order_queryset = sales_order_queryset.filter(date_filter)
        self.fields["sales_order"].queryset = sales_order_queryset.select_related("customer").order_by("-order_date", "-id")
        self.fields["sales_order"].label_from_instance = customer_return_sales_order_label
        self.fields["sales_order"].required = True
        self.fields["show_all_orders"].initial = show_all_orders
        self.fields["return_date"].initial = self.fields["return_date"].initial or timezone.localdate()

    def _show_all_orders(self) -> bool:
        if self.is_bound:
            return self.data.get(self.add_prefix("show_all_orders")) in ["1", "true", "on", "yes"]
        return bool(self.initial.get("show_all_orders"))

    def _selected_sales_order_id(self):
        if self.is_bound:
            return self.data.get(self.add_prefix("sales_order")) or None
        if self.instance and self.instance.pk:
            return self.instance.sales_order_id
        return self.initial.get("sales_order")

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get("customer")
        sales_order = cleaned.get("sales_order")
        if not sales_order:
            self.add_error("sales_order", "请选择来源销售订单")
        elif customer and sales_order.customer_id != customer.id:
            self.add_error("sales_order", "来源销售订单必须属于所选客户")
        elif sales_order:
            cleaned["customer"] = sales_order.customer
        return cleaned

    def save(self, commit=True):
        customer_return = super().save(commit=False)
        if not customer_return.return_no:
            customer_return.return_no = next_document_no("RT")
        if not customer_return.status:
            customer_return.status = CustomerReturn.Status.DRAFT
        if commit:
            customer_return.save()
            self.save_m2m()
        return customer_return


class CustomerReturnItemForm(forms.ModelForm):
    class Meta:
        model = CustomerReturnItem
        fields = [
            "sales_order_item",
            "material",
            "return_qty",
            "unit_price",
            "location",
            "inventory_type",
            "return_reason",
        ]
        widgets = {"return_reason": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        self.customer = kwargs.pop("customer", None)
        self.sales_order = kwargs.pop("sales_order", None)
        self.require_ready = kwargs.pop("require_ready", False)
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["sales_order_item"].label = "退货规格/型号"
        sales_item_queryset = (
            SalesOrderItem.objects.select_related("sales_order", "finished_material", "customer_product")
            .filter(
                shipped_qty__gt=0,
                sales_order__status__in=[SalesOrder.Status.SHIPPED, SalesOrder.Status.COMPLETED],
            )
            .order_by("-sales_order__order_date", "-sales_order_id", "line_no")
        )
        if self.sales_order:
            sales_item_queryset = sales_item_queryset.filter(sales_order=self.sales_order)
        elif self.customer:
            sales_item_queryset = sales_item_queryset.filter(sales_order__customer=self.customer)
        else:
            sales_item_queryset = sales_item_queryset.none()
        self.fields["sales_order_item"].queryset = sales_item_queryset
        self.fields["sales_order_item"].label_from_instance = customer_return_sales_item_label
        self.fields["sales_order_item"].required = False
        self.fields["material"].queryset = Material.objects.filter(
            status=Material.MaterialStatus.ACTIVE,
            material_type=Material.MaterialType.FINISHED,
        ).order_by("material_code")
        self.fields["material"].label_from_instance = material_choice_label
        self.fields["material"].widget = forms.HiddenInput()
        self.fields["material"].required = False
        self.fields["unit_price"].required = False
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
        self.fields["location"].required = False
        self.fields["inventory_type"].initial = self.fields["inventory_type"].initial or InventoryBatch.InventoryType.AVAILABLE

    def clean(self):
        cleaned = super().clean()
        sales_order_item = cleaned.get("sales_order_item")
        material = cleaned.get("material")
        return_qty = cleaned.get("return_qty")
        unit_price = cleaned.get("unit_price")
        location = cleaned.get("location")

        if sales_order_item:
            if self.customer and sales_order_item.sales_order.customer_id != self.customer.id:
                self.add_error("sales_order_item", "退货来源行必须属于所选客户")
            if self.sales_order and sales_order_item.sales_order_id != self.sales_order.id:
                self.add_error("sales_order_item", "退货来源行必须属于所选销售订单")
            if material and material.id != sales_order_item.finished_material_id:
                self.add_error("material", "退货物料必须与来源销售行成品一致")
            cleaned["material"] = sales_order_item.finished_material
            if not self.can_edit_amount:
                if self.instance and self.instance.pk and self.instance.sales_order_item_id == sales_order_item.id:
                    cleaned["unit_price"] = self.instance.unit_price
                else:
                    cleaned["unit_price"] = sales_order_item.unit_price
            elif unit_price in [None, ""]:
                cleaned["unit_price"] = sales_order_item.unit_price
        elif not material:
            self.add_error("material", "未选择来源销售行时必须填写退货物料")
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

        if self.require_ready and not location:
            self.add_error("location", "提交审核前必须选择入库库位")

        if sales_order_item and return_qty is not None and return_qty > 0:
            returned_qty = (
                CustomerReturnItem.objects.filter(sales_order_item=sales_order_item)
                .exclude(customer_return__status=CustomerReturn.Status.VOIDED)
                .exclude(pk=self.instance.pk)
                .aggregate(total=Sum("return_qty"))
                .get("total")
                or Decimal("0")
            )
            max_return_qty = sales_order_item.shipped_qty - returned_qty
            if return_qty > max_return_qty:
                self.add_error("return_qty", f"退货数量不能超过可退数量 {max_return_qty}")

        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if item.sales_order_item_id:
            item.material = item.sales_order_item.finished_material
            if item.unit_price in [None, ""]:
                item.unit_price = item.sales_order_item.unit_price
        if not item.inventory_type:
            item.inventory_type = InventoryBatch.InventoryType.AVAILABLE
        item.return_amount = _money(item.return_qty * item.unit_price)
        if commit:
            item.save()
        return item


class BaseCustomerReturnItemFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        self.customer = kwargs.pop("customer", None)
        self.sales_order = kwargs.pop("sales_order", None)
        self.require_ready = kwargs.pop("require_ready", False)
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["customer"] = self.customer
        kwargs["sales_order"] = self.sales_order
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
            and (form.cleaned_data.get("sales_order_item") or form.cleaned_data.get("material"))
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条客户退货明细")

        seen_items = set()
        for form in active_forms:
            sales_order_item = form.cleaned_data.get("sales_order_item")
            material = form.cleaned_data.get("material")
            key = (sales_order_item.id if sales_order_item else None, material.id if material else None)
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
                or not (form.cleaned_data.get("sales_order_item") or form.cleaned_data.get("material"))
            ):
                continue
            item = form.save(commit=False)
            item.customer_return = self.instance
            if commit:
                item.save()
            saved.append(item)
        return saved


CustomerReturnItemFormSet = inlineformset_factory(
    CustomerReturn,
    CustomerReturnItem,
    form=CustomerReturnItemForm,
    formset=BaseCustomerReturnItemFormSet,
    fields=["sales_order_item", "material", "return_qty", "unit_price", "location", "inventory_type", "return_reason"],
    extra=1,
    can_delete=True,
)


def recalculate_customer_return_total(customer_return: CustomerReturn) -> None:
    total = sum((item.return_amount for item in customer_return.items.all()), Decimal("0.00"))
    customer_return.return_amount = _money(total)
    customer_return.save(update_fields=["return_amount"])


class SalesShipmentForm(forms.ModelForm):
    class Meta:
        model = SalesShipment
        fields = [
            "shipment_date",
            "customer_contract_no",
            "customer_address_text",
            "customer_contact_name",
            "customer_contact_phone",
            "settlement_method",
            "remark",
        ]
        widgets = {
            "shipment_date": forms.DateInput(attrs={"type": "date"}),
            "customer_address_text": forms.Textarea(attrs={"rows": 2}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)


class SalesShipmentItemForm(forms.ModelForm):
    class Meta:
        model = SalesShipmentItem
        fields = ["shipment_qty", "batch", "location"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["batch"].queryset = (
            InventoryBatch.objects.select_related("material", "location")
            .filter(
                inventory_type=InventoryBatch.InventoryType.AVAILABLE,
                batch_status=InventoryBatch.BatchStatus.IN_STOCK,
                remaining_qty__gt=0,
            )
            .order_by("material__material_code", "location__location_code", "received_at", "batch_no")
        )
        if self.instance and self.instance.pk and self.instance.batch_id:
            self.fields["batch"].queryset = (
                InventoryBatch.objects.select_related("material", "location")
                .filter(pk=self.instance.batch_id)
                | self.fields["batch"].queryset
            ).distinct()
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")

    def clean(self):
        cleaned = super().clean()
        shipment_qty = cleaned.get("shipment_qty")
        batch = cleaned.get("batch")
        location = cleaned.get("location")
        if shipment_qty is not None and shipment_qty <= 0:
            self.add_error("shipment_qty", "出库数量必须大于 0")

        sales_item = self.instance.sales_order_item if self.instance and self.instance.pk else None
        if sales_item:
            if self.instance.material_id != sales_item.finished_material_id:
                raise forms.ValidationError("出库物料必须与销售订单行成品一致")
            if shipment_qty is not None:
                remaining_qty = sales_item.order_qty - sales_item.shipped_qty
                if shipment_qty > remaining_qty:
                    self.add_error("shipment_qty", "出库数量不能超过销售订单行未发货数量")
        if batch:
            if self.instance.material_id and batch.material_id != self.instance.material_id:
                self.add_error("batch", "批次物料必须与出库物料一致")
            if location and batch.location_id != location.id:
                self.add_error("location", "库位必须与批次库位一致")
            if shipment_qty is not None and shipment_qty > batch.remaining_qty:
                self.add_error("shipment_qty", "出库数量不能超过批次剩余数量")
            cleaned["location"] = batch.location
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if item.batch_id:
            item.location = item.batch.location
            item.cost_price = item.batch.cost_price
        if commit:
            item.save()
        return item


class BaseSalesShipmentItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        active_forms = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")]
        if not active_forms:
            raise forms.ValidationError("销售出库单至少需要一条出库明细")


SalesShipmentItemFormSet = inlineformset_factory(
    SalesShipment,
    SalesShipmentItem,
    form=SalesShipmentItemForm,
    formset=BaseSalesShipmentItemFormSet,
    fields=["shipment_qty", "batch", "location"],
    extra=0,
    can_delete=False,
)


class SampleLoanForm(forms.ModelForm):
    class Meta:
        model = SampleLoan
        fields = ["customer", "loan_date", "expected_return_date", "remark"]
        widgets = {
            "loan_date": forms.DateInput(attrs={"type": "date"}),
            "expected_return_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["customer"].queryset = Customer.objects.filter(status=Customer.CustomerStatus.ACTIVE).order_by("customer_no")
        self.fields["loan_date"].initial = self.fields["loan_date"].initial or timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        loan_date = cleaned.get("loan_date")
        expected_return_date = cleaned.get("expected_return_date")
        if loan_date and expected_return_date and expected_return_date < loan_date:
            self.add_error("expected_return_date", "预计归还日期不能早于借出日期")
        return cleaned

    def save(self, commit=True, user=None):
        loan = super().save(commit=False)
        if not loan.sample_loan_no:
            loan.sample_loan_no = next_document_no("SL")
        loan.status = loan.status or SampleLoan.Status.PENDING_APPROVAL
        if user and user.is_authenticated and not loan.created_by_id:
            loan.created_by = user
        if commit:
            loan.save()
            self.save_m2m()
        return loan


class SampleLoanItemForm(forms.ModelForm):
    class Meta:
        model = SampleLoanItem
        fields = ["material", "loan_qty", "expected_return_date", "batch", "location"]
        widgets = {"expected_return_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["material"].queryset = Material.objects.filter(
            status=Material.MaterialStatus.ACTIVE,
            material_type=Material.MaterialType.FINISHED,
        ).order_by("material_code")
        self.fields["material"].label_from_instance = material_choice_label
        self.fields["material"].widget.attrs["style"] = "display:none;"
        self.fields["batch"].queryset = InventoryBatch.objects.filter(
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
            remaining_qty__gt=0,
            material__status=Material.MaterialStatus.ACTIVE,
            material__material_type=Material.MaterialType.FINISHED,
        ).select_related("material", "location").order_by("material__material_code", "location__location_code", "batch_no")
        self.fields["batch"].label_from_instance = sample_loan_batch_label
        self.fields["batch"].widget.attrs["style"] = "display:none;"
        self.fields["batch"].required = False
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
        self.fields["location"].widget.attrs["style"] = "display:none;"
        self.fields["location"].required = False

    def clean(self):
        cleaned = super().clean()
        material = cleaned.get("material")
        loan_qty = cleaned.get("loan_qty")
        batch = cleaned.get("batch")
        location = cleaned.get("location")
        if loan_qty is not None and loan_qty <= 0:
            self.add_error("loan_qty", "借出数量必须大于 0")
        if batch:
            if material and batch.material_id != material.id:
                self.add_error("batch", "批次物料必须与借样物料一致")
            if location and batch.location_id != location.id:
                self.add_error("location", "库位必须与批次库位一致")
            if loan_qty and batch.remaining_qty < loan_qty:
                self.add_error("loan_qty", "借样数量不能超过批次剩余数量")
            cleaned["location"] = batch.location
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if not item.line_status:
            item.line_status = SampleLoanItem.LineStatus.OUT
        if commit:
            item.save()
        return item


class BaseSampleLoanItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("material")
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条借样明细")

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
            item.sample_loan = self.instance
            item.line_no = line_no
            if not item.expected_return_date:
                item.expected_return_date = self.instance.expected_return_date
            if commit:
                item.save()
            saved.append(item)
            line_no += 1
        return saved


SampleLoanItemFormSet = inlineformset_factory(
    SampleLoan,
    SampleLoanItem,
    form=SampleLoanItemForm,
    formset=BaseSampleLoanItemFormSet,
    fields=["material", "loan_qty", "expected_return_date", "batch", "location"],
    extra=1,
    can_delete=True,
)


class SampleLoanReturnForm(forms.ModelForm):
    class Meta:
        model = SampleLoanReturn
        fields = ["return_date", "remark"]
        widgets = {
            "return_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.sample_loan = kwargs.pop("sample_loan", None)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["return_date"].initial = self.fields["return_date"].initial or timezone.localdate()

    def save(self, commit=True):
        sample_return = super().save(commit=False)
        if self.sample_loan:
            sample_return.sample_loan = self.sample_loan
            sample_return.customer = self.sample_loan.customer
        if not sample_return.sample_return_no:
            sample_return.sample_return_no = next_document_no("SR")
        if not sample_return.status:
            sample_return.status = SampleLoanReturn.Status.DRAFT
        if commit:
            sample_return.save()
            self.save_m2m()
        return sample_return


class SampleLoanReturnItemForm(forms.ModelForm):
    class Meta:
        model = SampleLoanReturnItem
        fields = ["sample_loan_item", "return_qty", "location", "sample_condition", "remark"]
        widgets = {"remark": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        self.sample_loan = kwargs.pop("sample_loan", None)
        self.require_ready = kwargs.pop("require_ready", False)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        item_queryset = SampleLoanItem.objects.select_related("sample_loan", "material").filter(
            loan_qty__gt=0,
            sample_loan__status__in=[
                SampleLoan.Status.OUT,
                SampleLoan.Status.PART_RETURNED,
                SampleLoan.Status.PART_SOLD,
            ],
        )
        if self.sample_loan:
            item_queryset = item_queryset.filter(sample_loan=self.sample_loan)
        else:
            item_queryset = item_queryset.none()
        self.fields["sample_loan_item"].queryset = item_queryset.order_by("line_no", "id")
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
        self.fields["location"].required = False

    def clean(self):
        cleaned = super().clean()
        loan_item = cleaned.get("sample_loan_item")
        return_qty = cleaned.get("return_qty")
        location = cleaned.get("location")
        if loan_item and self.sample_loan and loan_item.sample_loan_id != self.sample_loan.id:
            self.add_error("sample_loan_item", "归还明细必须属于当前借样单")
        if return_qty is not None and return_qty <= 0:
            self.add_error("return_qty", "归还数量必须大于 0")
        if self.require_ready and not location:
            self.add_error("location", "提交确认前必须选择入库库位")
        if loan_item and return_qty is not None and return_qty > 0:
            pending_return_qty = (
                SampleLoanReturnItem.objects.filter(sample_loan_item=loan_item)
                .exclude(sample_return__status__in=[SampleLoanReturn.Status.VOIDED, SampleLoanReturn.Status.RECEIVED])
                .exclude(pk=self.instance.pk)
                .aggregate(total=Sum("return_qty"))
                .get("total")
                or Decimal("0")
            )
            available_qty = loan_item.loan_qty - loan_item.returned_qty - loan_item.sold_qty - pending_return_qty
            if return_qty > available_qty:
                self.add_error("return_qty", f"归还数量不能超过可归还数量 {available_qty}")
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if item.sample_loan_item_id:
            item.sample_loan = item.sample_loan_item.sample_loan
            item.material = item.sample_loan_item.material
        item.inventory_type = _sample_condition_inventory_type(item.sample_condition)
        if commit:
            item.save()
        return item


class BaseSampleLoanReturnItemFormSet(BaseInlineFormSet):
    def __init__(self, *args, **kwargs):
        self.sample_loan = kwargs.pop("sample_loan", None)
        self.require_ready = kwargs.pop("require_ready", False)
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["sample_loan"] = self.sample_loan
        kwargs["require_ready"] = self.require_ready
        return super()._construct_form(i, **kwargs)

    def clean(self):
        super().clean()
        active_forms = [
            form
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("sample_loan_item")
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条借样归还明细")

        seen_keys = set()
        for form in active_forms:
            loan_item = form.cleaned_data.get("sample_loan_item")
            location = form.cleaned_data.get("location")
            key = (loan_item.id if loan_item else None, location.id if location else None)
            if key in seen_keys:
                form.add_error("sample_loan_item", "同一归还单中相同借样行和库位不能重复")
            seen_keys.add(key)

    def save(self, commit=True):
        super().save(commit=False)
        for obj in self.deleted_objects:
            if obj.pk:
                obj.delete()

        saved = []
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE") or not form.cleaned_data.get("sample_loan_item"):
                continue
            item = form.save(commit=False)
            item.sample_return = self.instance
            item.sample_loan = self.sample_loan or item.sample_loan_item.sample_loan
            if commit:
                item.save()
            saved.append(item)
        return saved


SampleLoanReturnItemFormSet = inlineformset_factory(
    SampleLoanReturn,
    SampleLoanReturnItem,
    form=SampleLoanReturnItemForm,
    formset=BaseSampleLoanReturnItemFormSet,
    fields=["sample_loan_item", "return_qty", "location", "sample_condition", "remark"],
    extra=1,
    can_delete=True,
)


def _sample_condition_inventory_type(sample_condition: str) -> str:
    if sample_condition == SampleLoanReturnItem.SampleCondition.GOOD:
        return InventoryBatch.InventoryType.AVAILABLE
    return InventoryBatch.InventoryType.PENDING
