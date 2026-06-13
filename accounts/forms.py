from django import forms
from django.contrib.auth.password_validation import validate_password

from system.display import set_form_labels

from .models import Permission, Role, User


ADMIN_PERMISSION_CODE = "admin.permission_manage"


class AccountUserCreateForm(forms.ModelForm):
    password1 = forms.CharField(label="初始密码", widget=forms.PasswordInput)
    password2 = forms.CharField(label="确认密码", widget=forms.PasswordInput)
    reason = forms.CharField(label="操作原因", max_length=255)
    current_password = forms.CharField(label="当前登录密码", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = [
            "username",
            "display_name",
            "email",
            "department",
            "position",
            "security_level",
            "status",
            "is_active",
            "is_deleted",
            "roles",
        ]
        widgets = {
            "roles": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, operator=None, **kwargs):
        self.operator = operator
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        if "username" in self.fields:
            self.fields["username"].help_text = "必填。150 个字符以内，可使用字母、数字和 @/./+/-/_。"
        self.fields["roles"].queryset = Role.objects.order_by("role_code")

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "两次输入的密码不一致")
        if password1:
            validate_password(password1, self.instance)
        current_password = cleaned_data.get("current_password")
        if self.operator and (not current_password or not self.operator.check_password(current_password)):
            self.add_error("current_password", "当前登录密码不正确")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
            self.save_m2m()
        return user


class AccountUserUpdateForm(forms.ModelForm):
    reason = forms.CharField(label="操作原因", max_length=255)
    current_password = forms.CharField(label="当前登录密码", widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = [
            "display_name",
            "email",
            "department",
            "position",
            "security_level",
            "status",
            "is_active",
            "is_deleted",
            "roles",
        ]
        widgets = {
            "roles": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, operator=None, **kwargs):
        self.operator = operator
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["roles"].queryset = Role.objects.order_by("role_code")

    def clean_current_password(self):
        current_password = self.cleaned_data.get("current_password")
        if self.operator and (not current_password or not self.operator.check_password(current_password)):
            raise forms.ValidationError("当前登录密码不正确")
        return current_password

    def clean(self):
        cleaned_data = super().clean()
        if not self.operator or self.instance.pk != self.operator.pk:
            return cleaned_data

        if not cleaned_data.get("is_active"):
            self.add_error("is_active", "不能停用自己的当前账号")
        if cleaned_data.get("status") != User.AccountStatus.ACTIVE:
            self.add_error("status", "不能将自己的当前账号改为非启用状态")
        if cleaned_data.get("is_deleted"):
            self.add_error("is_deleted", "不能删除自己的当前账号")
        if not self.operator.is_superuser:
            selected_roles = cleaned_data.get("roles")
            has_admin_role = False
            if selected_roles is not None:
                has_admin_role = selected_roles.filter(
                    status=Role.RoleStatus.ACTIVE,
                    permissions__permission_code=ADMIN_PERMISSION_CODE,
                ).exists()
            if not has_admin_role:
                self.add_error("roles", "不能移除自己的最后一个权限管理角色")
        return cleaned_data


class AccountUserPasswordResetForm(forms.Form):
    new_password1 = forms.CharField(label="新密码", widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="确认新密码", widget=forms.PasswordInput)
    reason = forms.CharField(label="操作原因", max_length=255)
    current_password = forms.CharField(label="当前登录密码", widget=forms.PasswordInput)

    def __init__(self, *args, operator=None, target_user=None, **kwargs):
        self.operator = operator
        self.target_user = target_user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("new_password1")
        password2 = cleaned_data.get("new_password2")
        if password1 and password2 and password1 != password2:
            self.add_error("new_password2", "两次输入的密码不一致")
        if password1 and self.target_user:
            validate_password(password1, self.target_user)
        current_password = cleaned_data.get("current_password")
        if self.operator and (not current_password or not self.operator.check_password(current_password)):
            self.add_error("current_password", "当前登录密码不正确")
        return cleaned_data


class RoleForm(forms.ModelForm):
    reason = forms.CharField(label="操作原因", max_length=255)
    current_password = forms.CharField(label="当前登录密码", widget=forms.PasswordInput)

    class Meta:
        model = Role
        fields = ["role_code", "role_name", "status", "permissions", "remark"]
        widgets = {
            "permissions": forms.CheckboxSelectMultiple,
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, operator=None, **kwargs):
        self.operator = operator
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["permissions"].queryset = Permission.objects.order_by("permission_type", "permission_name")

    def clean_current_password(self):
        current_password = self.cleaned_data.get("current_password")
        if self.operator and (not current_password or not self.operator.check_password(current_password)):
            raise forms.ValidationError("当前登录密码不正确")
        return current_password

    def clean(self):
        cleaned_data = super().clean()
        if not self.operator or self.operator.is_superuser or not self.instance.pk:
            return cleaned_data
        if not self.operator.roles.filter(pk=self.instance.pk).exists():
            return cleaned_data

        selected_permissions = cleaned_data.get("permissions")
        keeps_admin_permission = False
        if selected_permissions is not None:
            keeps_admin_permission = selected_permissions.filter(permission_code=ADMIN_PERMISSION_CODE).exists()
        keeps_active = cleaned_data.get("status") == Role.RoleStatus.ACTIVE
        if keeps_admin_permission and keeps_active:
            return cleaned_data

        has_other_admin_role = self.operator.roles.filter(
            status=Role.RoleStatus.ACTIVE,
            permissions__permission_code=ADMIN_PERMISSION_CODE,
        ).exclude(pk=self.instance.pk).exists()
        if not has_other_admin_role:
            if not keeps_active:
                self.add_error("status", "不能停用自己最后一个权限管理角色")
            if not keeps_admin_permission:
                self.add_error("permissions", "不能移除自己最后一个权限管理角色的管理权限")
        return cleaned_data
