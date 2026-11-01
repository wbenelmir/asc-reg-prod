"""The restricted verified-identities dataset and its dedicated permission.

State and permission only: no column changes, and no group is granted it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exports", "0002_export_campaign"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="exportrequest",
            options={
                "permissions": [
                    (
                        "export_verified_identities",
                        "Can export the full identity values of verified participants",
                    )
                ]
            },
        ),
        migrations.AlterField(
            model_name="exportrequest",
            name="dataset_kind",
            field=models.CharField(
                choices=[
                    ("REGISTRATIONS_SUMMARY", "Registrations summary"),
                    ("VERIFIED_IDENTITIES", "Verified identities (full values)"),
                ],
                max_length=32,
            ),
        ),
    ]
