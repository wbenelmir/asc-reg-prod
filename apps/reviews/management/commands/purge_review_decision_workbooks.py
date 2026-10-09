"""Retention for decision-workbook previews (`apps.reviews.workbook`).

    uv run --env-file .env python manage.py purge_review_decision_workbooks

Marks previews past their validity EXPIRED (they can no longer be applied)
and erases the encrypted internal notes of every preview that was not
applied (an applied note lives in its decision). The uploaded files are never
stored, so there is nothing else to delete; counts and row outcomes stay as
audit evidence. Idempotent; reports a count only.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.reviews.workbook import purge_expired_decision_workbooks


class Command(BaseCommand):
    help = "Expire old decision-workbook previews and erase their unapplied internal notes."

    def handle(self, *args, **options) -> None:
        expired = purge_expired_decision_workbooks()
        self.stdout.write(self.style.SUCCESS(f"Expired {expired} decision-workbook preview(s)."))
