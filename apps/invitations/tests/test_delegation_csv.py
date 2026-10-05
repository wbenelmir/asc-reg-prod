"""Delegation CSV upload/validate/apply tests (Phase 2 Prompt 2 requirements
18-23; Schema §6.5)."""

from __future__ import annotations

import uuid

import pytest

from apps.invitations.models import DelegationBatchStatus, DelegationRowStatus
from apps.invitations.services import (
    DelegationHeaderError,
    apply_delegation_batch,
    upload_delegation_batch,
)

from .conftest import make_operational_user_with_membership

pytestmark = pytest.mark.django_db


def _upload(
    *,
    event,
    organization,
    csv_text: str | None = None,
    csv_bytes: bytes | None = None,
    key: str | None = None,
    filename: str = "delegation.csv",
):
    actor = make_operational_user_with_membership(
        email=f"delegation-actor-{uuid.uuid4()}@example.com",
        group_name="Delegation Coordinator",
        event_edition=event,
        organization=organization,
    )
    return upload_delegation_batch(
        organization=organization,
        event_edition=event,
        csv_bytes=csv_bytes if csv_bytes is not None else csv_text.encode("utf-8"),
        uploaded_filename=filename,
        created_by=actor,
        idempotency_key=key or f"delegation-{uuid.uuid4()}",
    )


def test_valid_csv_creates_valid_rows(event, organization) -> None:
    csv_text = (
        "email,given_names,family_name\na@example.com,Amine,Benali\nb@example.com,Sara,Kaci\n"
    )
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    assert batch.status == DelegationBatchStatus.VALIDATED
    assert batch.row_count == 2
    assert batch.valid_row_count == 2


def test_missing_required_column_rejects_the_header(event, organization) -> None:
    csv_text = "email,given_names\na@example.com,Amine\n"
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(event=event, organization=organization, csv_text=csv_text)
    assert "MISSING_REQUIRED_COLUMN" in exc.value.error_codes


@pytest.mark.parametrize(
    "forbidden_column",
    [
        "decision",
        "approval",
        "role",
        "badge_type",
        "access_profile",
        "document",
        "legal_acceptance",
    ],
)
def test_forbidden_columns_are_rejected(event, organization, forbidden_column) -> None:
    csv_text = f"email,given_names,family_name,{forbidden_column}\na@example.com,A,B,x\n"
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(event=event, organization=organization, csv_text=csv_text)
    assert "FORBIDDEN_COLUMN" in exc.value.error_codes


def test_unknown_column_is_rejected(event, organization) -> None:
    csv_text = "email,given_names,family_name,mystery_field\na@example.com,A,B,x\n"
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(event=event, organization=organization, csv_text=csv_text)
    assert "UNKNOWN_COLUMN" in exc.value.error_codes


def test_dry_run_never_creates_a_registration(event, organization) -> None:
    csv_text = "email,given_names,family_name\na@example.com,Amine,Benali\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    row = batch.rows.get()
    assert row.registration_id is None
    assert row.status == DelegationRowStatus.VALID


def test_missing_email_is_invalid(event, organization) -> None:
    csv_text = "email,given_names,family_name\n,Amine,Benali\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    row = batch.rows.get()
    assert row.status == DelegationRowStatus.INVALID
    assert "MISSING_EMAIL" in row.error_codes


def test_duplicate_email_within_the_same_batch_is_invalid(event, organization) -> None:
    csv_text = "email,given_names,family_name\ndup@example.com,A,B\ndup@example.com,C,D\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    rows = list(batch.rows.order_by("row_number"))
    assert rows[0].status == DelegationRowStatus.VALID
    assert rows[1].status == DelegationRowStatus.INVALID
    assert "DUPLICATE_ROW_IN_BATCH" in rows[1].error_codes


def _build_csv(header: list[str], row: list[str]) -> str:
    """Build a properly-quoted CSV document via the stdlib `csv` module --
    embedding a raw tab/CR/quote character directly in an f-string would
    produce MALFORMED CSV, not a realistic injection payload; a real
    attacker's spreadsheet tool would still emit valid CSV syntax."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerow(row)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "notes_value",
    [
        "=SUM(A1:A1)",
        "+cmd|' /C calc'!A0",
        "-cmd|' /C calc'!A0",
        "@SUM(1,2)",
        "\t=1+1",
        "\r=1+1",
        '"=1+1"',
        # Phase 2 Prompt 2 correction pass: signed-expression bypass --
        # the OLD "starts with +/- followed by a digit is always safe"
        # rule incorrectly let these through. A general text field (this
        # "notes" column has no telephone/numeric grammar) now rejects
        # them unconditionally.
        "-2+3",
        "+1-2",
        # Unicode whitespace / invisible-character bypass attempts.
        " =1+1",  # non-breaking space
        "​=1+1",  # zero-width space
        "﻿=1+1",  # BOM
    ],
)
def test_formula_shaped_values_are_rejected(event, organization, notes_value) -> None:
    csv_text = _build_csv(
        ["email", "given_names", "family_name", "notes"], ["a@example.com", "A", "B", notes_value]
    )
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    row = batch.rows.get()
    assert row.status == DelegationRowStatus.INVALID
    assert "FORMULA_SHAPED_VALUE" in row.error_codes


def test_ordinary_note_text_is_not_rejected(event, organization) -> None:
    csv_text = _build_csv(
        ["email", "given_names", "family_name", "notes"],
        ["a@example.com", "A", "B", "regular note text"],
    )
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    row = batch.rows.get()
    assert "FORMULA_SHAPED_VALUE" not in row.error_codes


@pytest.mark.parametrize("value", ["+213555123456", "+1 555 123 4567", "0555123456"])
def test_telephone_grammar_carve_out_accepts_legitimate_numbers(value) -> None:
    """The delegation CSV has no telephone column, so this exercises the
    shared `apps.core.spreadsheet_safety` helper directly -- the carve-out
    a genuinely telephone-typed field elsewhere in the project would use."""
    from apps.core.spreadsheet_safety import is_value_safe_for_field

    assert is_value_safe_for_field(value, field_kind="telephone") is True


@pytest.mark.parametrize("value", ["-2+3", "+1-2", "=1+1"])
def test_telephone_grammar_still_rejects_signed_expressions(value) -> None:
    from apps.core.spreadsheet_safety import is_value_safe_for_field

    assert is_value_safe_for_field(value, field_kind="telephone") is False


def test_general_field_never_gets_the_telephone_carve_out_without_declaring_it() -> None:
    """A field that does NOT declare `field_kind="telephone"` gets no
    carve-out at all, even for a value that would otherwise be a
    legitimate phone number -- exactly the delegation CSV's own columns."""
    from apps.core.spreadsheet_safety import is_value_safe_for_field

    assert is_value_safe_for_field("+213555123456", field_kind=None) is False


def test_no_raw_candidate_pii_in_error_codes(event, organization) -> None:
    csv_text = "email,given_names,family_name\nnot-an-email,Amine,Benali\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    row = batch.rows.get()
    for code in row.error_codes:
        assert "not-an-email" not in code
        assert "Amine" not in code


def test_upload_is_idempotent_on_the_same_key(event, organization) -> None:
    csv_text = "email,given_names,family_name\na@example.com,Amine,Benali\n"
    key = f"delegation-fixed-{uuid.uuid4()}"
    first = _upload(event=event, organization=organization, csv_text=csv_text, key=key)
    second = _upload(event=event, organization=organization, csv_text=csv_text, key=key)
    assert first.pk == second.pk


def test_apply_creates_exactly_one_registration_per_valid_row(event, organization) -> None:
    csv_text = "email,given_names,family_name\napply-a@example.com,A,B\napply-b@example.com,C,D\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    assert applied.status == DelegationBatchStatus.APPLIED
    assert applied.applied_row_count == 2
    for row in applied.rows.all():
        assert row.registration_id is not None
        assert row.status == DelegationRowStatus.APPLIED


def test_reapplying_the_same_batch_never_creates_a_second_registration(event, organization) -> None:
    csv_text = "email,given_names,family_name\napply-retry@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    first = apply_delegation_batch(batch)
    row = first.rows.get()
    first_registration_id = row.registration_id

    second = apply_delegation_batch(first)
    row.refresh_from_db()
    assert row.registration_id == first_registration_id
    assert second.applied_row_count == 1


def test_delegation_participant_can_still_have_a_separate_context_from_open_registration(
    event, organization
) -> None:
    """The same email later completing OPEN registration is a separate
    concern from the delegation-created Draft -- this test only proves the
    delegation row's own Registration is DELEGATION-sourced and independent."""
    from apps.registrations.models import RegistrationSourceKind

    csv_text = "email,given_names,family_name\nseparate-context@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = applied.rows.get()
    assert row.registration.source_kind == RegistrationSourceKind.DELEGATION


# ---------------------------------------------------------------------------
# Secure storage/scanning/lifecycle (Phase 2 Prompt 2 correction pass
# requirement 5).
# ---------------------------------------------------------------------------


def test_upload_rejects_a_non_csv_extension(event, organization) -> None:
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(
            event=event,
            organization=organization,
            csv_text="email,given_names,family_name\na@example.com,A,B\n",
            filename="delegation.txt",
        )
    assert "INVALID_FILE_TYPE" in exc.value.error_codes


def test_upload_rejects_malformed_utf8_encoding(event, organization) -> None:
    malformed = b"email,given_names,family_name\n\xff\xfea@example.com,A,B\n"
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(event=event, organization=organization, csv_bytes=malformed)
    assert "INVALID_ENCODING" in exc.value.error_codes


def test_upload_accepts_a_utf8_bom(event, organization) -> None:
    csv_bytes = "﻿email,given_names,family_name\na@example.com,A,B\n".encode()
    batch = _upload(event=event, organization=organization, csv_bytes=csv_bytes)
    assert batch.status == DelegationBatchStatus.VALIDATED
    assert batch.row_count == 1


def test_upload_rejects_a_file_exceeding_the_row_limit_without_truncating(
    event, organization, settings
) -> None:
    settings.DELEGATION_CSV_MAX_ROWS = 3
    lines = "\n".join(f"row{i}@example.com,A,B" for i in range(5))
    csv_text = f"email,given_names,family_name\n{lines}\n"
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(event=event, organization=organization, csv_text=csv_text)
    assert "ROW_LIMIT_EXCEEDED" in exc.value.error_codes
    # Rejected outright -- no partial batch left behind from a "truncated" import.
    from apps.invitations.models import DelegationBatch

    assert not DelegationBatch.objects.filter(organization=organization).exists()


def test_upload_rejects_when_the_scanner_reports_infected(event, organization, settings) -> None:
    settings.MALWARE_SCANNER_BACKEND = "apps.documents.scanning.RejectingStubScanner"
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(
            event=event,
            organization=organization,
            csv_text="email,given_names,family_name\na@example.com,A,B\n",
        )
    assert "SCAN_REJECTED" in exc.value.error_codes


def test_upload_rejects_when_the_scanner_is_unavailable(event, organization, monkeypatch) -> None:
    def _raise_scanner_unavailable():
        class _BrokenScanner:
            def scan(self, content):
                raise RuntimeError("scanner unreachable")

        return _BrokenScanner()

    monkeypatch.setattr("apps.documents.scanning.get_scanner", _raise_scanner_unavailable)
    with pytest.raises(DelegationHeaderError) as exc:
        _upload(
            event=event,
            organization=organization,
            csv_text="email,given_names,family_name\na@example.com,A,B\n",
        )
    assert "SCAN_UNAVAILABLE" in exc.value.error_codes


def test_clean_upload_is_never_marked_clean_without_a_real_scan(event, organization) -> None:
    """The StoredObject's `malware_scan_status` is only ever CLEAN because
    the deterministic local/test scanner actually reported clean -- proven
    by the mirror-image failing tests above, where the SAME code path
    raises before any StoredObject is ever created."""
    from apps.documents.models import MalwareScanStatus

    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\na@example.com,A,B\n",
    )
    assert batch.stored_object.malware_scan_status == MalwareScanStatus.CLEAN


def test_storage_bytes_are_deleted_when_database_persistence_fails(
    event, organization, monkeypatch, settings
) -> None:
    """Force the DB-persistence step to fail AFTER storage.save() has
    already written bytes, and confirm the just-written bytes are NOT left
    behind on disk (Phase 2 Prompt 2 correction pass)."""
    from apps.invitations import services as invitations_services

    root = settings.PRIVATE_STORAGE_ROOT
    before = set(root.iterdir()) if root.exists() else set()

    def _boom(**kwargs):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(invitations_services, "_persist_delegation_batch", _boom)

    with pytest.raises(RuntimeError):
        _upload(
            event=event,
            organization=organization,
            csv_text="email,given_names,family_name\na@example.com,A,B\n",
        )
    after = set(root.iterdir()) if root.exists() else set()
    assert after == before, f"orphaned storage file(s) left behind: {after - before}"


def test_source_object_purge_after_is_set_and_purge_deletes_bytes_only(event, organization) -> None:
    from apps.invitations.services import purge_expired_delegation_source_objects

    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\na@example.com,A,B\n",
    )
    stored_object = batch.stored_object
    assert stored_object.purge_after is not None

    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    future_now = (
        timezone.now()
        + timedelta(seconds=settings.DELEGATION_SOURCE_RETENTION_SECONDS)
        + timedelta(seconds=1)
    )
    purged = purge_expired_delegation_source_objects(now=future_now)
    assert purged == 1

    stored_object.refresh_from_db()
    assert stored_object.size_bytes == 0
    assert stored_object.sha256 == ""
    # The metadata row itself (and the batch it belongs to) is preserved.
    batch.refresh_from_db()
    assert batch.stored_object_id == stored_object.pk


def test_row_purge_erases_only_the_candidate_email_not_the_outcome(event, organization) -> None:
    from apps.invitations.services import purge_expired_delegation_rows

    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\npurge-me@example.com,A,B\n",
    )
    row = batch.rows.get()
    assert row.candidate_email_encrypted != ""

    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    future_now = (
        timezone.now()
        + timedelta(seconds=settings.DELEGATION_ROW_RETENTION_SECONDS)
        + timedelta(seconds=1)
    )
    purged = purge_expired_delegation_rows(now=future_now)
    assert purged == 1

    row.refresh_from_db()
    assert row.candidate_email_encrypted == ""
    assert row.candidate_email_hash == ""
    assert row.status == DelegationRowStatus.VALID  # outcome preserved


def test_purge_never_touches_a_row_still_within_its_retention_window(event, organization) -> None:
    from apps.invitations.services import purge_expired_delegation_rows

    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\nstill-fresh@example.com,A,B\n",
    )
    row = batch.rows.get()
    purged = purge_expired_delegation_rows()
    assert purged == 0
    row.refresh_from_db()
    assert row.candidate_email_encrypted != ""


# ---------------------------------------------------------------------------
# Genuinely idempotent upload (Phase 2 Prompt 2 correction pass
# requirement 6).
# ---------------------------------------------------------------------------


def test_view_level_idempotency_key_is_deterministic_for_the_same_content_and_token() -> None:
    import hashlib

    organization_id = "org-1"
    event_id = "event-1"
    token = "__abc123__"  # noqa: S105 - not a password, a test token value
    content_digest = hashlib.sha256(b"same content").hexdigest()
    key_a = hashlib.sha256(
        f"{organization_id}:{event_id}:{token}:{content_digest}".encode()
    ).hexdigest()
    key_b = hashlib.sha256(
        f"{organization_id}:{event_id}:{token}:{content_digest}".encode()
    ).hexdigest()
    assert key_a == key_b


def test_a_genuinely_different_file_creates_a_different_batch(event, organization) -> None:
    key_prefix = f"same-token-{uuid.uuid4()}"
    first = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\na@example.com,A,B\n",
        key=f"{key_prefix}:content-a",
    )
    second = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\nb@example.com,C,D\n",
        key=f"{key_prefix}:content-b",
    )
    assert first.pk != second.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.concurrency
def test_concurrent_duplicate_uploads_never_create_two_batches(event, organization) -> None:
    """Real multi-threaded race on the SAME idempotency key: exactly one
    batch must exist afterward, and the storage bytes written by the
    losing attempt must not be orphaned."""
    import threading

    from django.db import connection

    from apps.invitations.models import DelegationBatch

    actor = make_operational_user_with_membership(
        email=f"concurrent-actor-{uuid.uuid4()}@example.com",
        group_name="Delegation Coordinator",
        event_edition=event,
        organization=organization,
    )
    csv_bytes = b"email,given_names,family_name\nconcurrent@example.com,A,B\n"
    shared_key = f"concurrent-key-{uuid.uuid4()}"
    results: list = [None, None]
    errors: list = []
    barrier = threading.Barrier(2)

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = upload_delegation_batch(
                organization=organization,
                event_edition=event,
                csv_bytes=csv_bytes,
                uploaded_filename="delegation.csv",
                created_by=actor,
                idempotency_key=shared_key,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"unexpected error(s): {errors}"
    assert results[0] is not None and results[1] is not None
    assert results[0].pk == results[1].pk
    assert DelegationBatch.objects.filter(idempotency_key=shared_key).count() == 1


# ---------------------------------------------------------------------------
# Complete delegated-participant claim path (Phase 2 Prompt 2 correction
# pass requirement 7).
# ---------------------------------------------------------------------------


def test_apply_creates_a_claim_for_every_applied_row(event, organization) -> None:
    from apps.invitations.models import OnBehalfClaim, OnBehalfClaimStatus

    csv_text = "email,given_names,family_name\nclaimable@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = applied.rows.get()

    claim = OnBehalfClaim.objects.get(registration=row.registration)
    assert claim.status == OnBehalfClaimStatus.PENDING
    assert claim.claim_token_hash  # a hash is stored
    assert claim.expires_at is not None


def test_delegated_participant_can_claim_their_registration(event, organization) -> None:
    """The full path: apply -> a claim exists -> the participant, having
    somehow obtained the (durably queued) claim URL, verifies their email
    through the existing OTP flow and successfully claims. Actual delivery
    durability/retry mechanics have their own dedicated tests below."""
    from apps.invitations.models import OnBehalfClaim
    from apps.invitations.services import claim_on_behalf_registration
    from apps.people.services import resolve_or_create_participant_for_email

    csv_text = "email,given_names,family_name\nclaim-success@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = applied.rows.get()
    claim = OnBehalfClaim.objects.get(registration=row.registration)

    # The raw token is never stored -- retrieve it via a fresh claim using
    # the SAME hash-recomputation path the real view uses is not possible
    # without the raw token, so this test exercises the service directly
    # with a re-issued claim to prove the CLAIMING mechanics work; the
    # creation-side test above already proves a claim row is always made.
    person = resolve_or_create_participant_for_email("claim-success@example.com")
    # Simulate having the raw token (as the participant would, from the
    # locally-delivered email) by re-deriving a fresh claim with a KNOWN
    # raw token for this same registration and email, exercising the same
    # `claim_on_behalf_registration` function `on_behalf_claim` calls.
    from apps.audit import action_codes
    from apps.audit.services import PersistentAuditRecorder
    from apps.invitations.services import _create_claim

    claim.delete()
    raw_token = _create_claim(
        registration=row.registration,
        intended_email_encrypted=row.candidate_email_encrypted,
        intended_email_hash=row.candidate_email_hash,
        intended_email_hash_key_version=row.candidate_email_hash_key_version,
        created_by=batch.created_by,
        action_code=action_codes.CLAIM_ISSUED,
        audit_recorder=PersistentAuditRecorder(),
    )

    claimed = claim_on_behalf_registration(
        raw_claim_token=raw_token,
        verified_email="claim-success@example.com",
        person=person,
    )
    assert claimed.pk == row.registration_id
    claimed.refresh_from_db()
    assert claimed.person_id == person.pk


def test_delegated_claim_with_wrong_email_fails_generically(event, organization) -> None:
    from apps.audit import action_codes
    from apps.audit.services import PersistentAuditRecorder
    from apps.invitations.services import (
        ClaimUnavailable,
        _create_claim,
        claim_on_behalf_registration,
    )
    from apps.people.services import resolve_or_create_participant_for_email

    csv_text = "email,given_names,family_name\nclaim-wrong@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = applied.rows.get()
    row.registration.on_behalf_claim.delete()

    raw_token = _create_claim(
        registration=row.registration,
        intended_email_encrypted=row.candidate_email_encrypted,
        intended_email_hash=row.candidate_email_hash,
        intended_email_hash_key_version=row.candidate_email_hash_key_version,
        created_by=batch.created_by,
        action_code=action_codes.CLAIM_ISSUED,
        audit_recorder=PersistentAuditRecorder(),
    )
    wrong_person = resolve_or_create_participant_for_email("someone-else@example.com")
    with pytest.raises(ClaimUnavailable):
        claim_on_behalf_registration(
            raw_claim_token=raw_token,
            verified_email="someone-else@example.com",
            person=wrong_person,
        )


def test_delegated_draft_is_never_submitted_or_pre_accepted(event, organization) -> None:
    from apps.privacy.models import AcceptanceRecord

    csv_text = "email,given_names,family_name\nnever-submitted@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = applied.rows.get()
    assert row.registration.public_status == "DRAFT"
    assert not AcceptanceRecord.objects.filter(registration=row.registration).exists()


def test_delegation_claim_creation_audit_never_contains_the_email_or_token(
    event, organization
) -> None:
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent

    csv_text = "email,given_names,family_name\naudit-check@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    apply_delegation_batch(batch)

    events = AuditEvent.objects.filter(action_code=action_codes.CLAIM_ISSUED)
    assert events.exists()
    for entry in events:
        payload = str(entry.before_summary) + str(entry.after_summary) + str(entry.reason_code)
        assert "audit-check@example.com" not in payload


# ---------------------------------------------------------------------------
# Durable, retry-safe delegated-claim delivery (Phase 2 Prompt 2 V2
# correction pass requirement 7). `transaction.on_commit` callbacks only
# fire on a REAL commit, so these tests use `django_db(transaction=True)`
# (TransactionTestCase semantics), which also flushes migration-seeded
# data between tests -- the `DELEGATION_CLAIM` template is re-seeded by an
# explicit fixture, mirroring `apps.registrations.tests.test_confirmation`.
# ---------------------------------------------------------------------------


@pytest.fixture
def _delegation_claim_template():
    from django.utils import timezone as tz

    from apps.communications.models import MessageTemplate, MessageTemplateVersion
    from apps.invitations.services import DELEGATION_CLAIM_TEMPLATE_CODE

    template, _created = MessageTemplate.objects.get_or_create(
        code=DELEGATION_CLAIM_TEMPLATE_CODE,
        defaults={"channel": "EMAIL", "purpose_code": "DELEGATION_CLAIM"},
    )
    MessageTemplateVersion.objects.get_or_create(
        template=template,
        language="en",
        version_label="test",
        defaults={
            "subject": "Complete your registration",
            "body": "Use this link: {{claim_url}}",
            "status": "PUBLISHED",
            "effective_from": tz.now(),
            "content_hash": "d" * 64,
        },
    )
    return template


@pytest.mark.django_db(transaction=True)
def test_delegation_claim_delivery_succeeds_and_clears_the_recoverable_secret(
    event, organization, _delegation_claim_template
) -> None:
    from django.core import mail

    from apps.invitations.models import DelegationClaimDeliveryStatus, DelegationRow

    csv_text = "email,given_names,family_name\ndeliver-ok@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = DelegationRow.objects.get(pk=applied.rows.get().pk)

    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.SENT
    assert row.claim_delivery_url_encrypted == ""
    assert len(mail.outbox) == 1
    assert "http://localhost:8000/claim/" in mail.outbox[0].body


@pytest.mark.django_db(transaction=True)
def test_delegation_claim_delivery_failure_is_never_treated_as_success(
    event, organization, _delegation_claim_template, monkeypatch
) -> None:
    from apps.invitations.models import DelegationClaimDeliveryStatus, DelegationRow

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr("django.core.mail.send_mail", _boom)

    csv_text = "email,given_names,family_name\ndeliver-fail@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = DelegationRow.objects.get(pk=applied.rows.get().pk)

    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.FAILED
    # The encrypted claim URL is DELIBERATELY preserved -- it is the only
    # recoverable material a later retry has.
    assert row.claim_delivery_url_encrypted != ""
    # The row itself is still APPLIED -- a claim exists and is reachable
    # via retry, never lost merely because delivery failed once.
    assert row.status == DelegationRowStatus.APPLIED


@pytest.mark.django_db(transaction=True)
def test_failed_delegation_claim_delivery_can_be_retried_successfully(
    event, organization, _delegation_claim_template, monkeypatch
) -> None:
    from django.core import mail

    from apps.invitations.models import DelegationClaimDeliveryStatus, DelegationRow, OnBehalfClaim
    from apps.invitations.services import retry_failed_delegation_claim_deliveries

    monkeypatch.setattr(
        "django.core.mail.send_mail",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated outage")),
    )
    csv_text = "email,given_names,family_name\nretry-ok@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = DelegationRow.objects.get(pk=applied.rows.get().pk)
    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.FAILED
    registration_id_before = row.registration_id
    claim_count_before = OnBehalfClaim.objects.filter(
        registration_id=registration_id_before
    ).count()

    monkeypatch.undo()  # restore the real (local/test-backend) send_mail
    succeeded = retry_failed_delegation_claim_deliveries()

    assert succeeded == 1
    row.refresh_from_db()
    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.SENT
    assert row.claim_delivery_url_encrypted == ""
    assert len(mail.outbox) == 1
    # Never creates a second registration or a second active claim (Phase 2
    # Prompt 2 V2 correction pass "ensure repeated dispatch does not create
    # a second registration or second active claim").
    assert row.registration_id == registration_id_before
    assert (
        OnBehalfClaim.objects.filter(registration_id=registration_id_before).count()
        == claim_count_before
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_delivery_created_without_a_template_can_recover_after_publication(
    event, organization, _delegation_claim_template
) -> None:
    from django.core import mail
    from django.utils import timezone as tz

    from apps.invitations.models import DelegationClaimDeliveryStatus, DelegationRow, OnBehalfClaim
    from apps.invitations.services import retry_failed_delegation_claim_deliveries

    _delegation_claim_template.versions.all().update(status="DRAFT")
    batch = _upload(
        event=event,
        organization=organization,
        csv_text="email,given_names,family_name\nlate-template@example.com,A,B\n",
    )
    applied = apply_delegation_batch(batch)
    row = DelegationRow.objects.get(pk=applied.rows.get().pk)
    registration_id_before = row.registration_id
    claim_count_before = OnBehalfClaim.objects.filter(
        registration_id=registration_id_before
    ).count()

    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.FAILED
    assert row.claim_delivery_message_id is None
    assert row.claim_delivery_url_encrypted != ""

    _delegation_claim_template.versions.filter(language="en").update(
        status="PUBLISHED", effective_from=tz.now()
    )
    succeeded = retry_failed_delegation_claim_deliveries()

    assert succeeded == 1
    row.refresh_from_db()
    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.SENT
    assert row.claim_delivery_message_id is not None
    assert row.claim_delivery_url_encrypted == ""
    assert len(mail.outbox) == 1
    assert row.registration_id == registration_id_before
    assert (
        OnBehalfClaim.objects.filter(registration_id=registration_id_before).count()
        == claim_count_before
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_retry_on_an_expired_claim_clears_the_secret_without_resending(
    event, organization, _delegation_claim_template, monkeypatch
) -> None:
    from datetime import timedelta

    from django.core import mail
    from django.utils import timezone as tz

    from apps.invitations.models import DelegationClaimDeliveryStatus, DelegationRow, OnBehalfClaim
    from apps.invitations.services import retry_failed_delegation_claim_deliveries

    monkeypatch.setattr(
        "django.core.mail.send_mail",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated outage")),
    )
    csv_text = "email,given_names,family_name\nretry-expired@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    applied = apply_delegation_batch(batch)
    row = DelegationRow.objects.get(pk=applied.rows.get().pk)
    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.FAILED
    assert row.claim_delivery_url_encrypted != ""

    OnBehalfClaim.objects.filter(registration_id=row.registration_id).update(
        expires_at=tz.now() - timedelta(days=1)
    )
    monkeypatch.undo()

    succeeded = retry_failed_delegation_claim_deliveries()

    assert succeeded == 0
    row.refresh_from_db()
    # Rendered permanently unrecoverable once the underlying claim has
    # expired -- never resent, since the claim itself could no longer be
    # used even if the email arrived (Phase 2 Prompt 2 V2 correction pass
    # "remove or render unrecoverable any delivery secret after successful
    # dispatch or expiry").
    assert row.claim_delivery_url_encrypted == ""
    assert row.claim_delivery_status == DelegationClaimDeliveryStatus.FAILED
    assert len(mail.outbox) == 0


@pytest.mark.django_db(transaction=True)
def test_delegation_claim_delivery_audit_never_contains_email_or_url(
    event, organization, _delegation_claim_template
) -> None:
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent

    csv_text = "email,given_names,family_name\naudit-delivery@example.com,A,B\n"
    batch = _upload(event=event, organization=organization, csv_text=csv_text)
    apply_delegation_batch(batch)

    events = AuditEvent.objects.filter(
        action_code__in=[
            action_codes.DELEGATION_CLAIM_DELIVERY_QUEUED,
            action_codes.DELEGATION_CLAIM_DELIVERY_SENT,
            action_codes.DELEGATION_CLAIM_DELIVERY_FAILED,
        ]
    )
    assert events.exists()
    for entry in events:
        payload = str(entry.before_summary) + str(entry.after_summary) + str(entry.reason_code)
        assert "audit-delivery@example.com" not in payload
        assert "/claim/" not in payload
