from django.db import migrations


def seed_personal_info_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.get_or_create(
        permission_code="masterdata.view_personal_info",
        defaults={"permission_name": "查看客户和供应商联系信息", "permission_type": "field"},
    )


def remove_personal_info_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code="masterdata.view_personal_info").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_seed_sales_view_all_permission"),
    ]

    operations = [
        migrations.RunPython(seed_personal_info_permission, remove_personal_info_permission),
    ]
