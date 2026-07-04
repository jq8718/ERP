import json

from django import forms

from system.display import set_form_labels

from .models import CustomerProduct, Material


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
