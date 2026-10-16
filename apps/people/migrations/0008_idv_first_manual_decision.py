"""IDV-FIRST: identity verification from the first manual review without a document.

State only: two new choice values, a verification source and an attempt
method, both meaning "a reviewer's manual decision recorded without an
identity document". Choices are not enforced by the database, so no SQL
runs and no existing row is changed.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("people", "0007_upd_2026_10_identity_corrections"),
    ]

    operations = [
        migrations.AlterField(
            model_name="identityverification",
            name="verification_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("MINISTRY_API", "Ministry service"),
                    ("SIMULATED_API", "Development simulation (not official)"),
                    ("MANUAL_NATIONAL_ID_CARD", "Manual review of the national identity card"),
                    ("MANUAL_PASSPORT", "Manual review of the passport identity page"),
                    ("STAFF_EXCEPTION", "Staff-assisted exception"),
                    ("MANUAL_NIN_EXEMPTION", "Manual documentary review (NIN exemption)"),
                    ("MANUAL_REVIEWER_DECISION", "Manual reviewer decision (no document)"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="identityverificationattempt",
            name="method",
            field=models.CharField(
                choices=[
                    ("EXTERNAL_SERVICE", "External service"),
                    ("MANUAL_DOCUMENT", "Manual document"),
                    ("SUPERVISED_CORRECTION", "Supervised correction"),
                    ("MANUAL_DECISION", "Manual reviewer decision"),
                ],
                max_length=24,
            ),
        ),
    ]
