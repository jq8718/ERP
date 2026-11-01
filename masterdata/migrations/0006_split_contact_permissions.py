from django.db import migrations


def split_contact_permissions(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    customer, _ = Permission.objects.get_or_create(
        permission_code="masterdata.view_customer_contact",
        defaults={"permission_name": "查看客户联系信息", "permission_type": "field"},
    )
    supplier, _ = Permission.objects.get_or_create(
        permission_code="masterdata.view_supplier_contact",
        defaults={"permission_name": "查看供应商联系信息", "permission_type": "field"},
    )
    old = Permission.objects.filter(permission_code="masterdata.view_personal_info").first()
    if old:
        for role in Role.objects.filter(permissions=old).prefetch_related("permissions"):
            codes = set(role.permissions.values_list("permission_code", flat=True))
            if codes.intersection({"sales.view", "sales.process", "sales.view_all", "finance.payment_process"}):
                role.permissions.add(customer)
            if codes.intersection({"purchase.view", "purchase.process", "finance.payment_process"}):
                role.permissions.add(supplier)
        old.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_seed_module_view_permissions"),
        ("masterdata", "0005_alter_supplier_supplier_type"),
    ]

    operations = [migrations.RunPython(split_contact_permissions, migrations.RunPython.noop)]
