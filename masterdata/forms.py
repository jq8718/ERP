import json
from decimal import Decimal

from django import forms

from system.display import set_form_labels

from .models import CustomerProduct, Material, MaterialSupplierPrice, MaterialUnitConversion, Supplier


class PlainTextOrJsonField(forms.CharField):
    widget = forms.Textarea

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 3}))
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if value in self.empty_values:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return {}

        text = value.strip()
        if not text:
            return {}

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


class CustomerProductForm(forms.ModelForm):
    label_requirements = PlainTextOrJsonField()
    packaging_requirements = PlainTextOrJsonField()

    class Meta:
        model = CustomerProduct
        fields = [
            "customer_product_no",
            "customer_product_name",
            "finished_material",
            "default_sale_price",
            "label_requirements",
            "packaging_requirements",
            "status",
        ]

    def __init__(self, *args, **kwargs):
        can_edit_amount = kwargs.pop("can_edit_amount", True)
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        if not can_edit_amount:
            self.fields.pop("default_sale_price", None)
        self.fields["finished_material"].queryset = Material.objects.filter(
            material_type=Material.MaterialType.FINISHED,
            status=Material.MaterialStatus.ACTIVE,
        ).order_by("material_code")
        self.fields["finished_material"].required = True
        self.fields["finished_material"].error_messages["required"] = "生产成品不能为空"

    def clean_default_sale_price(self):
        value = self.cleaned_data.get("default_sale_price")
        if value is not None and value < Decimal("0"):
            raise forms.ValidationError("默认销售价不能小于 0")
        return value


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = [
            "material_code",
            "material_name",
            "material_type",
            "spec",
            "base_unit",
            "qty_precision",
            "min_stock_qty",
            "latest_purchase_price",
            "status",
            "remark",
        ]
        widgets = {"remark": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        material_type_field = self.fields["material_type"]
        if self.instance and self.instance.pk and self.instance.material_type == Material.MaterialType.FINISHED:
            material_type_field.choices = Material.MaterialType.choices
        else:
            material_type_field.choices = [
                choice
                for choice in Material.MaterialType.choices
                if choice[0] != Material.MaterialType.FINISHED
            ]
            material_type_field.help_text = "成品请在“产品组成清单”新建页面维护。"

    def clean_min_stock_qty(self):
        value = self.cleaned_data.get("min_stock_qty")
        if value is not None and value < Decimal("0"):
            raise forms.ValidationError("最低库存不能小于 0")
        return value

    def clean_latest_purchase_price(self):
        value = self.cleaned_data.get("latest_purchase_price")
        if value is not None and value < Decimal("0"):
            raise forms.ValidationError("最近采购价不能小于 0")
        return value


class MaterialUnitConversionForm(forms.ModelForm):
    class Meta:
        model = MaterialUnitConversion
        fields = ["source_unit", "target_unit", "ratio", "status"]

    def clean(self):
        cleaned = super().clean()
        source_unit = cleaned.get("source_unit")
        target_unit = cleaned.get("target_unit")
        ratio = cleaned.get("ratio")
        if source_unit and target_unit and source_unit == target_unit:
            self.add_error("target_unit", "源单位和目标单位不能相同")
        if ratio is not None and ratio <= Decimal("0"):
            self.add_error("ratio", "换算比例必须大于 0")
        return cleaned


class MaterialSupplierPriceForm(forms.ModelForm):
    class Meta:
        model = MaterialSupplierPrice
        fields = ["supplier", "purchase_price", "currency", "effective_from", "effective_to", "is_default", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by(
            "supplier_no"
        )

    def clean(self):
        cleaned = super().clean()
        purchase_price = cleaned.get("purchase_price")
        effective_from = cleaned.get("effective_from")
        effective_to = cleaned.get("effective_to")
        if purchase_price is not None and purchase_price < Decimal("0"):
            self.add_error("purchase_price", "采购价格不能小于 0")
        if effective_from and effective_to and effective_to < effective_from:
            self.add_error("effective_to", "失效日期不能早于生效日期")
        return cleaned
