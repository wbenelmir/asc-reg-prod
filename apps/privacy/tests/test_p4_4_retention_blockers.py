"""P4-4: unresolved retention facts are reported as release blockers
(`privacy.W002`), never as an approval. Synthetic data only."""

from __future__ import annotations

import pytest
from django.core.checks import run_checks

from apps.privacy import checks
from apps.privacy.models import RetentionCategory

pytestmark = pytest.mark.django_db


def _w002():
    return [m for m in run_checks(databases=["default"]) if m.id == "privacy.W002"]


def test_no_category_and_no_job_are_both_reported() -> None:
    RetentionCategory.objects.all().delete()
    blockers = checks.retention_release_blockers()
    assert "no active retention category is configured" in blockers
    assert "no approved participant-data retention or purge job exists" in blockers
    (warning,) = _w002()
    assert "release blockers" in warning.msg and "not a legal approval" in warning.msg


def test_a_category_without_a_period_is_reported_without_its_code() -> None:
    RetentionCategory.objects.create(code="P44_PENDING", description="Synthetic pending category")
    blockers = checks.retention_release_blockers()
    assert "1 retention category(ies) without an approved period" in blockers
    assert "P44_PENDING" not in " ".join(blockers)


def test_approved_periods_alone_do_not_clear_the_blocker_without_a_job() -> None:
    RetentionCategory.objects.all().delete()
    RetentionCategory.objects.create(
        code="P44_SET", description="Synthetic category", retention_period_days=30
    )
    assert checks.retention_release_blockers() == [
        "no approved participant-data retention or purge job exists"
    ]
    assert len(_w002()) == 1


def test_the_check_is_silent_without_a_database_and_never_an_error() -> None:
    assert checks.retention_markers(databases=None) == []
    messages = run_checks(databases=["default"])
    assert not [m for m in messages if m.id.startswith("privacy.E")]
