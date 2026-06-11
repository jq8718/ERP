from django.db import migrations


BUSINESS_PROCESS_PERMISSIONS = [
    ("sales.process", "处理销售单据", "action"),
    ("purchase.process", "处理采购单据", "action"),
    ("inventory.process", "处理库存单据", "action"),
    ("production.process", "处理生产单据", "action"),
]


def seed_business_process_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    for code, name, permission_type in BUSINESS_PROCESS_PERMISSIONS:
        Permission.objects.get_or_create(
            permission_code=code,
            defaults={"permission_name": name, "permission_type": permission_type},
        )


def remove_business_process_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code__in=[code for code, _, _ in BUSINESS_PROCESS_PERMISSIONS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_seed_finance_payment_process_permission"),
    ]

    operations = [
        migrations.RunPython(seed_business_process_permissions, remove_business_process_permissions),
    ]
