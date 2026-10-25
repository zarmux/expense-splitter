from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0003_profile_upi_id_split_upi_id_payment"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="split",
            name="upi_id",
        ),
    ]
