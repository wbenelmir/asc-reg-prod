"""UPD-06: an export may be limited to one invitation campaign's results.

Additive: one nullable foreign key; existing exports are unchanged.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("exports", "0001_initial"),
        ("invitations", "0002_delegationrow_claim_delivery_message_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="exportrequest",
            name="invitation_campaign",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="export_requests",
                to="invitations.invitationcampaign",
            ),
        ),
    ]
