import json

from django import forms

from system.display import set_form_labels

from .models import ApprovalRule


class JsonDictField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 4}))
        kwargs.setdefault("help_text", '可留空；如需条件，请填写 JSON 字典，例如 {"min_amount": "5000"}')
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if value in self.empty_values:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values or not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise forms.ValidationError('条件配置格式不正确，请填写类似 {"min_amount": "5000"} 的 JSON，或留空。')
        if not isinstance(parsed, dict):
            raise forms.ValidationError('条件配置必须是 JSON 字典，例如 {"min_amount": "5000"}，不能只填普通文字。')
        return parsed


class ApprovalRuleForm(forms.ModelForm):
    condition_json = JsonDictField()

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
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)

    def clean(self):
        cleaned_data = super().clean()
        approver_role = cleaned_data.get("approver_role")
        approver_user = cleaned_data.get("approver_user")
        if not approver_role and not approver_user:
            raise forms.ValidationError("审批角色和审批人员至少填写一个")
        return cleaned_data
