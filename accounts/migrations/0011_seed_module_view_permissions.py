from django.db import migrations


MODULE_VIEW_PERMISSIONS = [
    ("sales.view", "查看本人销售数据", "module"),
    ("bom.view", "查看 BOM", "module"),
    ("purchase.view", "查看采购数据", "module"),
    ("inventory.view", "查看库存数据", "module"),
    ("production.view", "查看生产数据", "module"),
]


def seed_module_view_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    for code, name, permission_type in MODULE_VIEW_PERMISSIONS:
        Permission.objects.get_or_create(
            permission_code=code,
            defaults={"permission_name": name, "permission_type": permission_type},
        )


def remove_module_view_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code__in=[code for code, _, _ in MODULE_VIEW_PERMISSIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_alter_usersession_session_key"),
    ]

    operations = [
        migrations.RunPython(seed_module_view_permissions, remove_module_view_permissions),
    ]
