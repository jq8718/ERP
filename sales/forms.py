from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from inventory.models import InventoryBatch, WarehouseLocation
from masterdata.models import Customer, CustomerAddress, CustomerProduct, Material
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


class SalesOrderForm(forms.ModelForm):
    submit_for_approval = forms.BooleanField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = SalesOrder
        fields = ["customer", "customer_address", "order_date", "delivery_date", "remark"]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "delivery_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["order_date"].initial = self.fields["order_date"].initial or timezone.localdate()
        self.fields["customer_address"].queryset = CustomerAddress.objects.select_related("customer").filter(
            status=CustomerAddress.AddressStatus.ACTIVE
        )

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
    class Meta:
        model = SalesOrderItem
        fields = ["customer_product", "order_qty", "unit_price"]

    def __init__(self, *args, **kwargs):
        self.can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)
        self.fields["customer_product"].queryset = (
            CustomerProduct.objects.select_related("customer", "finished_material")
            .filter(status=CustomerProduct.ProductStatus.ACTIVE, finished_material__isnull=False)
            .order_by("customer__customer_name", "customer_product_no")
        )
        self.fields["unit_price"].required = False

    def clean(self):
        cleaned = super().clean()
        customer_product = cleaned.get("customer_product")
        order_qty = cleaned.get("order_qty")
        unit_price = cleaned.get("unit_price")
        if customer_product and customer_product.finished_material_id is None:
            raise forms.ValidationError("客户产品必须关联成品编码")
        if order_qty is not None and order_qty <= 0:
            self.add_error("order_qty", "数量必须大于 0")
        if customer_product and not self.can_edit_amount:
            if self.instance and self.instance.pk and self.instance.customer_product_id == customer_product.id:
                cleaned["unit_price"] = self.instance.unit_price
            else:
                cleaned["unit_price"] = customer_product.default_sale_price or Decimal("0")
        else:
            if unit_price is not None and unit_price < 0:
                self.add_error("unit_price", "单价不能小于 0")
            if customer_product and unit_price in [None, ""]:
                cleaned["unit_price"] = customer_product.default_sale_price or Decimal("0")
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if item.customer_product_id:
            item.finished_material = item.customer_product.finished_material
        item.line_amount = _money(item.order_qty * item.unit_price)
        if item.sales_order.status == SalesOrder.Status.PENDING_APPROVAL:
            item.line_status = SalesOrderItem.LineStatus.PENDING_APPROVAL
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
            if form.cleaned_data and not form.cleaned_data.get("DELETE") and form.cleaned_data.get("customer_product")
        ]
        if not active_forms:
            raise forms.ValidationError("至少需要录入一条销售订单明细")

        seen_customer_products = set()
        for form in active_forms:
            customer_product = form.cleaned_data["customer_product"]
            if customer_product.id in seen_customer_products:
                form.add_error("customer_product", "同一销售订单中客户产品不能重复")
            seen_customer_products.add(customer_product.id)

    def save(self, commit=True):
        super().save(commit=False)
        for obj in self.deleted_objects:
            if obj.pk:
                obj.delete()

        saved = []
        line_no = 1
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE") or not form.cleaned_data.get("customer_product"):
                continue
            item = form.save(commit=False)
            item.sales_order = self.instance
            item.line_no = line_no + 10000
            if self.instance.status == SalesOrder.Status.PENDING_APPROVAL:
                item.line_status = SalesOrderItem.LineStatus.PENDING_APPROVAL
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
    fields=["customer_product", "order_qty", "unit_price"],
    extra=3,
    can_delete=True,
)


def recalculate_sales_order_total(order: SalesOrder) -> None:
    total = sum((item.line_amount for item in order.items.all()), Decimal("0.00"))
    order.total_amount = _money(total)
    order.save(update_fields=["total_amount", "updated_at"])


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


class CustomerReturnForm(forms.ModelForm):
    class Meta:
        model = CustomerReturn
        fields = ["customer", "sales_order", "return_date", "remark"]
        widgets = {
            "return_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(status=Customer.CustomerStatus.ACTIVE).order_by("customer_no")
        self.fields["sales_order"].queryset = (
            SalesOrder.objects.select_related("customer")
            .filter(status__in=[SalesOrder.Status.SHIPPED, SalesOrder.Status.COMPLETED])
            .order_by("-order_date", "-id")
        )
        self.fields["sales_order"].required = False
        self.fields["return_date"].initial = self.fields["return_date"].initial or timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get("customer")
        sales_order = cleaned.get("sales_order")
        if customer and sales_order and sales_order.customer_id != customer.id:
            self.add_error("sales_order", "来源销售订单必须属于所选客户")
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
        self.fields["sales_order_item"].required = False
        self.fields["material"].queryset = Material.objects.filter(
            status=Material.MaterialStatus.ACTIVE,
            material_type=Material.MaterialType.FINISHED,
        ).order_by("material_code")
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
    extra=3,
    can_delete=True,
)


def recalculate_customer_return_total(customer_return: CustomerReturn) -> None:
    total = sum((item.return_amount for item in customer_return.items.all()), Decimal("0.00"))
    customer_return.return_amount = _money(total)
    customer_return.save(update_fields=["return_amount"])


class SalesShipmentForm(forms.ModelForm):
    class Meta:
        model = SalesShipment
        fields = ["shipment_date", "remark"]
        widgets = {
            "shipment_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }


class SalesShipmentItemForm(forms.ModelForm):
    class Meta:
        model = SalesShipmentItem
        fields = ["shipment_qty", "batch", "location"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        self.fields["material"].queryset = Material.objects.filter(
            status=Material.MaterialStatus.ACTIVE,
            material_type=Material.MaterialType.FINISHED,
        ).order_by("material_code")
        self.fields["batch"].queryset = InventoryBatch.objects.filter(
            batch_status=InventoryBatch.BatchStatus.IN_STOCK,
            remaining_qty__gt=0,
        ).select_related("material", "location").order_by("material__material_code", "location__location_code", "batch_no")
        self.fields["batch"].required = False
        self.fields["location"].queryset = WarehouseLocation.objects.filter(
            status=WarehouseLocation.LocationStatus.ACTIVE
        ).order_by("location_code")
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
    extra=3,
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
    extra=3,
    can_delete=True,
)


def _sample_condition_inventory_type(sample_condition: str) -> str:
    if sample_condition == SampleLoanReturnItem.SampleCondition.GOOD:
        return InventoryBatch.InventoryType.AVAILABLE
    return InventoryBatch.InventoryType.PENDING
