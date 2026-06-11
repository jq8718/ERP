from django.db import migrations


DEFAULT_PERMISSIONS = [
    ("admin.permission_manage", "权限与审批规则管理", "action"),
    ("sales.view_all", "查看全部销售数据", "data_scope"),
    ("finance.view_amount", "查看财务金额", "field"),
    ("masterdata.view_personal_info", "查看客户和供应商联系信息", "field"),
    ("files.attachment_sensitive_view", "查看敏感附件", "field"),
    ("files.attachment_delete", "删除附件", "action"),
]


def seed_default_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    for code, name, permission_type in DEFAULT_PERMISSIONS:
        Permission.objects.get_or_create(
            permission_code=code,
            defaults={"permission_name": name, "permission_type": permission_type},
        )


def remove_default_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code__in=[code for code, _, _ in DEFAULT_PERMISSIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_role_permissions_user_roles"),
    ]

    operations = [
        migrations.RunPython(seed_default_permissions, remove_default_permissions),
    ]
