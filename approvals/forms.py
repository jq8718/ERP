from django import forms

from system.display import set_form_labels

from .models import ApprovalRule


class ApprovalRuleForm(forms.ModelForm):
    class Meta:
        model = ApprovalRule
        fields = [
            "doc_type",
            "condition_json",
            "level_no",
            "approver_role",
            "approver_user",
            "allow_auto_skip_same_user",
            "require_second_verify",
            "status",
            "remark",
        ]
        widgets = {
            "condition_json": forms.Textarea(attrs={"rows": 4}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)

    def clean_condition_json(self):
        value = self.cleaned_data.get("condition_json")
        return value or {}

    def clean(self):
        cleaned_data = super().clean()
        approver_role = cleaned_data.get("approver_role")
        approver_user = cleaned_data.get("approver_user")
        if not approver_role and not approver_user:
            raise forms.ValidationError("审批角色和审批人员至少填写一个")
        return cleaned_data
