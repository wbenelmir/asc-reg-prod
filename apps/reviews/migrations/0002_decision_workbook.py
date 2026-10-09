"""Decision workbook tables (export manifest, import preview, preview rows).

Additive only: three new tables and their indexes. No existing table, column
or row is altered, and no data is moved -- in particular the review queue
backfill is NOT run here (`manage.py backfill_review_queue`, dry run then
--apply, is a separate, explicit deployment step). Rolling the code back
leaves these tables unused; they need not be dropped.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("registrations", "0010_ux2_grouped_interest_topics"),
        ("reviews", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReviewDecisionExport",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("template_version", models.CharField(max_length=32)),
                ("filters", models.JSONField(blank=True, default=dict)),
                ("row_count", models.PositiveIntegerField(default=0)),
                ("rows", models.JSONField(blank=True, default=list)),
                ("content_sha256", models.CharField(blank=True, default="", max_length=64)),
                ("expires_at", models.DateTimeField()),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "reviews_decision_workbook_export",
            },
        ),
        migrations.CreateModel(
            name="ReviewDecisionImport",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("content_sha256", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PREVIEWED", "Previewed, not applied"),
                            ("APPLIED", "Applied"),
                            ("REFUSED", "Refused at final validation, nothing applied"),
                            ("EXPIRED", "Expired, nothing applied"),
                        ],
                        default="PREVIEWED",
                        max_length=16,
                    ),
                ),
                ("counts", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField()),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("refusal_code", models.CharField(blank=True, default="", max_length=64)),
                (
                    "export",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="imports",
                        to="reviews.reviewdecisionexport",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "reviews_decision_workbook_import",
                "permissions": [
                    ("bulk_registrationdecision", "Can export and apply review decision workbooks")
                ],
            },
        ),
        migrations.CreateModel(
            name="ReviewDecisionImportRow",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("row_number", models.PositiveIntegerField()),
                ("reference_text", models.CharField(blank=True, default="", max_length=40)),
                ("decision", models.CharField(blank=True, default="", max_length=8)),
                ("attendance_category", models.CharField(blank=True, default="", max_length=32)),
                ("rejection_reason_code", models.CharField(blank=True, default="", max_length=64)),
                (
                    "internal_note_encrypted",
                    apps.core.fields.EncryptedTextField(blank=True, default=""),
                ),
                ("registration_version", models.PositiveIntegerField(blank=True, null=True)),
                ("case_version", models.PositiveIntegerField(blank=True, null=True)),
                ("identity_version", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("NO_CHANGE", "No change"),
                            ("VALID", "Valid proposed decision"),
                            ("INVALID", "Invalid"),
                        ],
                        max_length=16,
                    ),
                ),
                ("error_codes", models.JSONField(blank=True, default=list)),
                (
                    "applied_decision",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="reviews.registrationdecision",
                    ),
                ),
                (
                    "decision_import",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rows",
                        to="reviews.reviewdecisionimport",
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="registrations.registration",
                    ),
                ),
                (
                    "review_case",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="reviews.reviewcase",
                    ),
                ),
            ],
            options={
                "db_table": "reviews_decision_workbook_import_row",
                "ordering": ["row_number"],
            },
        ),
        migrations.AddIndex(
            model_name="reviewdecisionexport",
            index=models.Index(fields=["created_by", "created_at"], name="rev_wbexport_user_idx"),
        ),
        migrations.AddIndex(
            model_name="reviewdecisionimport",
            index=models.Index(fields=["uploaded_by", "created_at"], name="rev_wbimport_user_idx"),
        ),
        migrations.AddIndex(
            model_name="reviewdecisionimport",
            index=models.Index(fields=["status", "expires_at"], name="rev_wbimport_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="reviewdecisionimportrow",
            constraint=models.UniqueConstraint(
                fields=("decision_import", "row_number"), name="rev_wbimportrow_row_uq"
            ),
        ),
    ]
