"""UPD-02 / UPD-03: staff identity corrections and participant notes.

Additive and non-destructive: four new columns on the append-only identity
decision history (an encrypted note to the participant, the notification
language and its basis, and the encrypted before/after values of a staff
correction), plus two new choice values (a decision action and a revision
source). No existing row is changed.
"""

from django.db import migrations, models

import apps.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("people", "0006_ministry_nin_diagnostics_permission"),
    ]

    operations = [
        migrations.AddField(
            model_name="identitydecision",
            name="changes_encrypted",
            field=apps.core.fields.EncryptedTextField(blank=True, db_default="", default=""),
        ),
        migrations.AddField(
            model_name="identitydecision",
            name="notification_language",
            field=models.CharField(blank=True, db_default="", default="", max_length=8),
        ),
        migrations.AddField(
            model_name="identitydecision",
            name="notification_language_basis",
            field=models.CharField(blank=True, db_default="", default="", max_length=24),
        ),
        migrations.AddField(
            model_name="identitydecision",
            name="participant_note_encrypted",
            field=apps.core.fields.EncryptedTextField(blank=True, db_default="", default=""),
        ),
        migrations.AlterField(
            model_name="identitydecision",
            name="action",
            field=models.CharField(
                choices=[
                    ("MANUAL_VERIFY", "Verified manually"),
                    ("EXCEPTION_VERIFY", "Verified by staff-assisted exception"),
                    ("RETURN_FOR_CORRECTION", "Returned for correction"),
                    ("NIN_CORRECTION", "NIN corrected and rechecked"),
                    ("RECHECK", "Rechecked with the ministry service"),
                    ("REJECT", "Rejected"),
                    ("PARTICIPANT_RESUBMISSION", "Participant resubmitted"),
                    (
                        "LINKED_IDENTITY_CHANGE",
                        "Identifier changed in another registration of the same person",
                    ),
                    ("OFFICIAL_NAME_APPLIED", "Official name form applied"),
                    ("REGISTRATION_CLOSED", "Registration closed"),
                    ("CORRECT_AND_VERIFY", "Corrected and verified by staff"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="identityrevision",
            name="source",
            field=models.CharField(
                choices=[
                    ("SUBMISSION", "Final submission"),
                    ("PARTICIPANT_RESUBMISSION", "Participant correction"),
                    ("STAFF_NIN_CORRECTION", "Staff NIN correction"),
                    ("STAFF_RECHECK", "Staff recheck"),
                    (
                        "LINKED_IDENTITY_CHANGE",
                        "Identifier corrected in another registration of the same person",
                    ),
                    (
                        "OFFICIAL_NAME_NORMALIZATION",
                        "Official name form applied (case or spacing only)",
                    ),
                    ("STAFF_IDENTITY_CORRECTION", "Staff correction from evidence"),
                ],
                max_length=32,
            ),
        ),
    ]
