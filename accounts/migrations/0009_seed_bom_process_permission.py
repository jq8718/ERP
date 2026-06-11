from django.db import migrations


def seed_bom_process_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.get_or_create(
        permission_code="bom.process",
        defaults={
            "permission_name": "维护和启停 BOM",
            "permission_type": "action",
        },
    )


def remove_bom_process_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code="bom.process").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_seed_business_process_permissions"),
    ]

    operations = [
        migrations.RunPython(seed_bom_process_permission, remove_bom_process_permission),
    ]
