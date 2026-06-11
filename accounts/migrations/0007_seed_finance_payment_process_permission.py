from django.db import migrations


def seed_finance_payment_process_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.get_or_create(
        permission_code="finance.payment_process",
        defaults={"permission_name": "处理收付款和余额", "permission_type": "action"},
    )


def remove_finance_payment_process_permission(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Permission.objects.filter(permission_code="finance.payment_process").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_seed_personal_info_permission"),
    ]

    operations = [
        migrations.RunPython(seed_finance_payment_process_permission, remove_finance_payment_process_permission),
    ]
