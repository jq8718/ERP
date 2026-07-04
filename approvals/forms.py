import json

from django import forms

from system.display import code_label, set_form_labels

from .models import ApprovalRule


APPROVAL_DOC_TYPE_CHOICES = (
    ("sales_order", code_label("sales_order")),
    ("customer_return", code_label("customer_return")),
    ("sample_loan", code_label("sample_loan")),
    ("purchase_request", code_label("purchase_request")),
    ("purchase_order", code_label("purchase_order")),
    ("supplier_return", code_label("supplier_return")),
    ("customer_receipt", code_label("customer_receipt")),
    ("supplier_payment", code_label("supplier_payment")),
)


class ApprovalRuleForm(forms.ModelForm):
    doc_type = forms.ChoiceField(choices=APPROVAL_DOC_TYPE_CHOICES)
    condition_min_amount = forms.DecimalField(
        label="最低金额",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
        help_text="留空表示所有金额都适用。",
    )

    class Meta:
        model = ApprovalRule
        fields = [
            "doc_type",
            "condition_min_amount",
            "level_no",
            "approver_role",
            "approver_user",
            "allow_auto_skip_same_user",
            "require_second_verify",
            "status",
            "remark",
        ]
        widgets = {
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        if self.instance and isinstance(self.instance.condition_json, dict):
            self.fields["condition_min_amount"].initial = self.instance.condition_json.get("min_amount", "")

    def clean(self):
        cleaned_data = super().clean()
        approver_role = cleaned_data.get("approver_role")
        approver_user = cleaned_data.get("approver_user")
        if not approver_role and not approver_user:
            raise forms.ValidationError("审批角色和审批人员至少填写一个")
        cleaned_data["condition_json"] = self._clean_condition_json(cleaned_data)
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.condition_json = self.cleaned_data.get("condition_json", {})
        if commit:
            instance.save()
            self.save_m2m()
        return instance

    def _clean_condition_json(self, cleaned_data):
        min_amount = cleaned_data.get("condition_min_amount")
        if min_amount not in [None, ""]:
            return {"min_amount": str(min_amount)}

        legacy_value = self.data.get("condition_json", "") if self.is_bound else ""
        if not legacy_value:
            return {}
        try:
            parsed = json.loads(legacy_value)
        except json.JSONDecodeError:
            self.add_error("condition_min_amount", "条件配置格式不正确，请直接填写最低金额，或留空。")
            return {}
        if not isinstance(parsed, dict):
            self.add_error("condition_min_amount", "条件配置格式不正确，请直接填写最低金额，或留空。")
            return {}
        return parsed
