"""Phase 3 Prompt 2 correction pass: tighten BadgeTypeAssignment.public_reference.

The earlier partial unique constraint (`WHERE public_reference <> ''`)
existed only to survive the instant between adding the column and running its
backfill. Leaving it in place afterwards keeps "several rows share the empty
string" representable -- and an empty reference would produce an unusable
`bai` claim inside a signed credential.

This migration closes that: any row still blank is filled first, then the
constraint becomes unconditional and a check constraint forbids blanks
outright. The fill step runs before the constraints so the migration is safe
against existing data rather than assuming the earlier backfill reached every
row.
"""

from django.conf import settings
from django.db import migrations, models

import apps.accreditation.models


def fill_remaining_blank_references(apps_registry, schema_editor):
    """Give any still-blank assignment a distinct random reference."""
    BadgeTypeAssignment = apps_registry.get_model("accreditation", "BadgeTypeAssignment")
    generate = apps.accreditation.models.generate_assignment_public_reference
    for assignment in BadgeTypeAssignment.objects.filter(public_reference="").iterator():
        assignment.public_reference = generate()
        assignment.save(update_fields=["public_reference"])


def noop_reverse(apps_registry, schema_editor):
    """Reverse is a no-op: the values are opaque and must not be cleared here.

    Blanking them would violate the constraint this migration installs, and
    the preceding migration's own reverse already handles removal.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("accreditation", "0003_phase3_prompt2"),
        ("events", "0003_eventedition_passport_identity_page_upload_enabled"),
        ("organizations", "0003_professionalaffiliation_biography_and_more"),
        ("registrations", "0006_alter_registration_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(fill_remaining_blank_references, noop_reverse),
        migrations.RemoveConstraint(
            model_name="badgetypeassignment",
            name="acc_badge_assignment_public_ref_uq",
        ),
        migrations.AlterField(
            model_name="badgetypeassignment",
            name="public_reference",
            field=models.CharField(
                default=apps.accreditation.models.generate_assignment_public_reference,
                max_length=22,
            ),
        ),
        migrations.AddConstraint(
            model_name="badgetypeassignment",
            constraint=models.UniqueConstraint(
                fields=("public_reference",), name="acc_badge_assignment_public_ref_uq"
            ),
        ),
        migrations.AddConstraint(
            model_name="badgetypeassignment",
            constraint=models.CheckConstraint(
                condition=models.Q(("public_reference", ""), _negated=True),
                name="acc_badge_assignment_public_ref_not_blank",
            ),
        ),
    ]
