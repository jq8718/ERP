from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0006_split_contact_permissions")]

    operations = [
        migrations.AddField(
            model_name="material",
            name="sourcing_type",
            field=models.CharField(
                "供货方式",
                choices=[("manufactured", "自产"), ("resale", "外购转售")],
                default="manufactured",
                max_length=24,
            ),
        ),
    ]
