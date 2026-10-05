"""Register again after a withdrawal (correction package 2026-10-04): real
multi-connection PostgreSQL races, threads on their own connections (the
pattern of `test_idv_q_owner_decisions_concurrency.py`).

1. Two "register again" requests at once: one new draft, both get it.
2. "Register again" racing a staff reopening of the same withdrawn
   registration: never two live registrations -- either the reopening wins
   and no new draft exists, or the new draft wins and the reopening is
   refused.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import threading
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.registrations.models import Registration

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_named(callables: dict) -> tuple[dict, list[BaseException]]:
    barrier = threading.Barrier(len(callables))
    results: dict = {}
    errors: list[BaseException] = []

    def _worker(name, target) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(n, t), name=n) for n, t in callables.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    return results, errors


@pytest.fixture(autouse=True)
def _private_storage(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture
def withdrawn():
    from apps.core.models import Country, Sector
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import make_event
    from apps.privacy.models import LegalDocument, LegalDocumentVersion
    from apps.registrations.services import get_or_create_active_draft, submit_full_registration
    from apps.registrations.tests.factories import walk_draft_through_every_step
    from apps.reviews.services import withdraw_registration

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"wd-race-{code}",
                content="Synthetic",
                content_hash="e" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status="PUBLISHED",
            )
        )
    person = resolve_or_create_participant_for_email(f"wd-race-{uuid.uuid4().hex[:6]}@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=make_event("WDRACE"))
    walk_draft_through_every_step(draft)
    submit_full_registration(
        registration=draft,
        privacy_notice_version=versions[0],
        terms_version=versions[1],
        data_processing_consent_granted=True,
        session_reference="wd-race",
        idempotency_key=f"wd-race-{draft.pk}",
    )
    draft.refresh_from_db()
    return withdraw_registration(registration=draft, person=person, expected_version=draft.version)


def _live(registration) -> list[Registration]:
    return list(
        Registration.objects.filter(
            person_id=registration.person_id, event_edition_id=registration.event_edition_id
        ).exclude(public_status="WITHDRAWN")
    )


def test_two_simultaneous_requests_create_one_new_registration(withdrawn) -> None:
    from apps.registrations.services import start_registration_after_withdrawal

    def again():
        return start_registration_after_withdrawal(
            registration=Registration.objects.get(pk=withdrawn.pk), person=withdrawn.person
        ).pk

    results, errors = _run_named({"first": again, "second": again})
    assert not errors, errors
    assert results["first"] == results["second"]
    assert [r.pk for r in _live(withdrawn)] == [results["first"]]
    old = Registration.objects.get(pk=withdrawn.pk)
    assert old.public_status == "WITHDRAWN" and not old.is_current_context


def test_registering_again_racing_a_staff_reopening_never_leaves_two_live_registrations(
    withdrawn,
) -> None:
    from apps.registrations.services import (
        RegistrationNotRestartable,
        start_registration_after_withdrawal,
    )
    from apps.reviews.services import StaleVersionError, reopen_registration

    def again():
        try:
            return start_registration_after_withdrawal(
                registration=Registration.objects.get(pk=withdrawn.pk), person=withdrawn.person
            ).pk
        except RegistrationNotRestartable:
            return "refused"

    def reopen():
        try:
            reopen_registration(
                registration=withdrawn,
                expected_version=withdrawn.version,
                reason="Synthetic reopen race",
                actor=None,
            )
        except (ValueError, StaleVersionError):  # fmt: skip
            return "refused"
        return "reopened"

    results, errors = _run_named({"again": again, "reopen": reopen})
    assert not errors, errors
    live = _live(withdrawn)
    assert len(live) == 1  # exactly one live registration, never two
    old = Registration.objects.get(pk=withdrawn.pk)
    if results["reopen"] == "reopened":
        assert results["again"] == "refused"
        assert live[0].pk == old.pk and old.is_current_context
    else:
        assert results["again"] == live[0].pk
        assert old.public_status == "WITHDRAWN" and not old.is_current_context
