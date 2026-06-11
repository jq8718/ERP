from django.db import migrations


def seed_sales_view_all_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.get_or_create(
        permission_code="sales.view_all",
        defaults={"permission_name": "查看全部销售数据", "permission_type": "data_scope"},
    )


def remove_sales_view_all_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code="sales.view_all").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_seed_default_permissions"),
    ]

    operations = [
        migrations.RunPython(seed_sales_view_all_permission, remove_sales_view_all_permission),
    ]
