from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_created_by_to_purchase_owner(apps, schema_editor):
    PurchaseOrder = apps.get_model("purchase", "PurchaseOrder")
    PurchaseOrder.objects.filter(purchase_owner__isnull=True, created_by__isnull=False).update(
        purchase_owner=models.F("created_by")
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("purchase", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="purchase_owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_purchase_orders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="purchaseorder",
            index=models.Index(fields=["purchase_owner", "status"], name="purchase_or_purchas_bbeb00_idx"),
        ),
        migrations.RunPython(copy_created_by_to_purchase_owner, migrations.RunPython.noop),
    ]
