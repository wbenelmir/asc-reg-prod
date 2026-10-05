"""`purge_delegation_retention` management command tests (Phase 2 Prompt 2
V2 correction pass requirement 8: "wire delegation retention into an
operational entry point"). The command itself adds no new purge logic --
these tests confirm it is idempotent, reports bounded counts only, and
never prints a personal value or storage identifier.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from apps.invitations.tests.test_delegation_csv import _upload

pytestmark = pytest.mark.django_db


def _run_command() -> str:
    out = io.StringIO()
    call_command("purge_delegation_retention", stdout=out)
    return out.getvalue()


def test_command_purges_nothing_for_a_fresh_batch(event, organization) -> None:
    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\nfresh-command@example.com,A,B\n",
    )
    output = _run_command()
    assert (
        "Purged 0 expired delegation source object(s) and 0 expired delegation row value(s)."
        in output
    )

    batch.stored_object.refresh_from_db()
    row = batch.rows.get()
    assert batch.stored_object.size_bytes != 0
    assert row.candidate_email_encrypted != ""


def test_command_purges_an_expired_batch_and_row_and_reports_counts_only(
    event, organization
) -> None:
    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\nexpired-command@example.com,A,B\n",
    )
    stored_object = batch.stored_object
    row = batch.rows.get()

    stored_object.purge_after = timezone.now() - timedelta(seconds=1)
    stored_object.save(update_fields=["purge_after"])
    batch.created_at = timezone.now() - timedelta(
        seconds=settings.DELEGATION_ROW_RETENTION_SECONDS + 3600
    )
    batch.save(update_fields=["created_at"])

    output = _run_command()
    assert (
        "Purged 1 expired delegation source object(s) and 1 expired delegation row value(s)."
        in output
    )
    assert "expired-command@example.com" not in output
    assert stored_object.storage_key not in output
    assert row.candidate_email_hash not in output

    stored_object.refresh_from_db()
    row.refresh_from_db()
    assert stored_object.size_bytes == 0
    assert row.candidate_email_encrypted == ""


def test_command_is_idempotent_on_a_second_run(event, organization) -> None:
    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\nidempotent-command@example.com,A,B\n",
    )
    stored_object = batch.stored_object
    stored_object.purge_after = timezone.now() - timedelta(seconds=1)
    stored_object.save(update_fields=["purge_after"])
    batch.created_at = timezone.now() - timedelta(
        seconds=settings.DELEGATION_ROW_RETENTION_SECONDS + 3600
    )
    batch.save(update_fields=["created_at"])

    first_output = _run_command()
    assert (
        "Purged 1 expired delegation source object(s) and 1 expired delegation row value(s)."
        in first_output
    )

    second_output = _run_command()
    assert (
        "Purged 0 expired delegation source object(s) and 0 expired delegation row value(s)."
        in second_output
    )
