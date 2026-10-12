"""UPD-03: record the interface language of the final submission.

Additive and non-destructive: one nullable-free column with an empty default.
Existing registrations keep an empty value; their language is derived at read
time from the submission evidence (`apps.registrations.language`), so no data
is rewritten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("registrations", "0010_ux2_grouped_interest_topics"),
    ]

    operations = [
        migrations.AddField(
            model_name="registration",
            name="submission_language",
            field=models.CharField(blank=True, db_default="", default="", max_length=8),
        ),
    ]
