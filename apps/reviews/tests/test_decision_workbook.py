"""Review decision workbook: Excel export, upload preview, final validation.

Synthetic registrations only (submitted, verified identity, open STANDARD
case), built directly so each test stays fast; the identity paths themselves
are covered by `test_review_entry.py`. Covers the export scope (every page,
eligibility, decision scope), the literal-text workbook, the round trip of
mixed decisions through the ordinary services, KEEP and preview without
business effect, tampered / duplicated / unknown / formula / incomplete rows,
staleness after export and after preview, all-or-nothing application,
idempotent re-application, permissions and the hardened reader.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accreditation.models import AttendanceCategory, AttendanceEntitlement
from apps.accreditation.tests.attendance_fixtures import configure_attendance
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.communications.models import CommunicationMessage
from apps.core.xlsx import (
    Column,
    ReadLimits,
    Sheet,
    WorkbookReadError,
    read_workbook,
    write_workbook,
)
from apps.people.tests.conftest import staff_client
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import (
    Registration,
    RegistrationPublicStatus,
    RegistrationSubmission,
    RegistrationSubmissionKind,
)
from apps.reviews import intake, workbook
from apps.reviews.apps import (
    ACCREDITATION_MANAGERS_GROUP_NAME,
    REGISTRATION_REVIEWERS_GROUP_NAME,
    REVIEW_DECISION_WORKBOOK_GROUP_NAME,
)
from apps.reviews.models import (
    RegistrationDecision,
    RegistrationDecisionOutcome,
    ReviewCase,
    ReviewDecisionImport,
    ReviewDecisionImportStatus,
)

from .conftest import make_operational_user_with_membership, make_registration

pytestmark = pytest.mark.django_db

ALL = AttendanceCategory.ALL_CONFERENCE_DAYS
FOLLOWING = AttendanceCategory.FOLLOWING_TWO_DAYS


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _grant(user, group_name, *, event=None, organization=None):
    from django.contrib.auth.models import Group

    from apps.accounts.models import ScopedGroupMembership

    return ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        organization=organization,
        granted_by=user,
    )


@pytest.fixture
def operator(event):
    """An Accreditation Manager who also holds the workbook role, for `event`."""
    user = make_operational_user_with_membership(
        email="workbook-operator@example.test",
        group_name=ACCREDITATION_MANAGERS_GROUP_NAME,
        event_edition=event,
    )
    _grant(user, REVIEW_DECISION_WORKBOOK_GROUP_NAME, event=event)
    return user


def actionable(event, actor, *, organization=None, prerequisites=True, enqueue=True):
    """A submitted registration with a verified identity, waiting in review
    (or, with `enqueue=False`, verified before the queue entry existed)."""
    registration = make_registration(event=event, organization=organization)
    RegistrationSubmission.objects.create(
        registration=registration,
        sequence=1,
        submission_kind=RegistrationSubmissionKind.INITIAL,
        snapshot_json={"synthetic": True},
        snapshot_hash="0" * 64,
        submitted_by_type="PARTICIPANT",
        submitted_at=registration.submitted_at,
        idempotency_key=f"wb-{uuid.uuid4().hex}",
    )
    make_verified_identity_case(registration)
    if prerequisites:
        assign_approval_prerequisites(registration, actor)
    if not enqueue:
        registration.refresh_from_db()
        return registration
    result = intake.enqueue_for_participation_review(
        registration.pk, trigger=intake.EntryTrigger.BACKFILL
    )
    assert result.outcome == intake.EntryOutcome.CREATED, result
    registration.refresh_from_db()
    return registration


def _export(user, cases=None):
    cases = cases if cases is not None else ReviewCase.objects.all()
    return workbook.export_decision_workbook(user=user, cases_queryset=cases, filters={})


def _decisions_sheet(content):
    sheets = read_workbook(content, limits=ReadLimits(max_bytes=10 * 1024 * 1024))
    return sheets[workbook.DECISIONS_SHEET], sheets[workbook.INSTRUCTIONS_SHEET]


def _rows(content):
    decisions, _ = _decisions_sheet(content)
    width = len(workbook.COLUMNS)
    return [decisions.row_values(row, width) for row in range(2, decisions.max_row + 1)]


def edit(content, edits=None, *, extra_rows=(), drop=(), transform=None):
    """Simulate an operator editing the exported workbook: `edits` maps a
    registration reference to {column key: value}. Returns new .xlsx bytes."""
    edits = edits or {}
    _decisions, instructions = _decisions_sheet(content)
    rows = []
    for values in _rows(content):
        reference = values[workbook.INDEX["reference"]]
        if reference in drop:
            continue
        for key, value in edits.get(reference, {}).items():
            values[workbook.INDEX[key]] = value
        rows.append(values)
    rows.extend(list(row) for row in extra_rows)
    if transform:
        rows = transform(rows)
    meta = [instructions.row_values(row, 2) for row in range(1, instructions.max_row + 1)]
    return write_workbook(
        [
            Sheet(
                name=workbook.DECISIONS_SHEET,
                columns=[Column(header) for header in workbook.HEADERS],
                rows=rows,
            ),
            Sheet(
                name=workbook.INSTRUCTIONS_SHEET,
                columns=[Column(""), Column("")],
                rows=meta,
                header=False,
                auto_filter=False,
                freeze_header=False,
            ),
        ]
    )


def _preview(user, content):
    return workbook.preview_decision_workbook(user=user, data=content, filename="decisions.xlsx")


def _row(decision_import, registration):
    return decision_import.rows.get(registration=registration)


def _business_snapshot():
    """Every business fact a preview must leave untouched."""
    return {
        "registrations": sorted(
            Registration.objects.values_list("pk", "public_status", "internal_status", "version")
        ),
        "cases": sorted(ReviewCase.objects.values_list("pk", "status", "version")),
        "decisions": RegistrationDecision.objects.count(),
        "entitlements": AttendanceEntitlement.objects.count(),
        "messages": CommunicationMessage.objects.count(),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_covers_every_actionable_row_and_only_those(
    event, other_event, organization, operator
) -> None:
    configure_attendance(event)
    ready = [actionable(event, operator, prerequisites=False) for _ in range(27)]
    # Not exported: no verified identity, an information request, a decision,
    # another event (out of scope) and a draft.
    unverified = make_registration(event=event)
    decided = actionable(event, operator, prerequisites=False)
    from apps.reviews.services import record_not_approved_decision

    record_not_approved_decision(
        registration=decided,
        expected_version=decided.version,
        internal_reason_code="NOT_ELIGIBLE",
        decided_by=operator,
    )
    elsewhere = actionable(other_event, operator, prerequisites=False)

    export, content = _export(operator)
    rows = _rows(content)
    references = {row[workbook.INDEX["reference"]] for row in rows}
    assert export.row_count == len(rows) == 27  # more than one queue page (25)
    assert references == {registration.public_reference for registration in ready}
    for excluded in (unverified, decided, elsewhere):
        assert excluded.public_reference not in references
    # Every row defaults to KEEP, with the technical binding to the manifest.
    manifest = {entry["r"]: entry for entry in export.rows}
    for row in rows:
        assert row[workbook.INDEX["decision"]] == workbook.KEEP
        assert row[workbook.INDEX["profile"]] == row[workbook.INDEX["reason"]] == ""
        entry = manifest[row[workbook.INDEX["registration_id"]]]
        assert row[workbook.INDEX["case_id"]] == entry["c"]
        assert row[workbook.INDEX["registration_version"]] == str(entry["rv"])
        assert row[workbook.INDEX["export_id"]] == str(export.pk)
        assert row[workbook.INDEX["template_version"]] == workbook.TEMPLATE_VERSION
    audit = AuditEvent.objects.get(action_code=action_codes.REVIEW_DECISION_WORKBOOK_EXPORTED)
    assert audit.after_summary == {"row_count": 27, "template": workbook.TEMPLATE_VERSION}


def test_the_workbook_is_literal_text_with_dropdowns_frozen_header_and_filter(
    event, operator
) -> None:
    configure_attendance(event)
    registration = actionable(event, operator, prerequisites=False)
    from apps.registrations.models import RegistrationProfile

    RegistrationProfile.objects.update_or_create(
        registration=registration,
        defaults={"submitted_full_name": '=HYPERLINK("http://example.invalid")'},
    )
    _export_record, content = _export(operator)
    archive = zipfile.ZipFile(io.BytesIO(content))
    sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet  # no formula is ever written
    assert 'state="frozen"' in sheet and "<autoFilter" in sheet
    assert '"KEEP,ACCEPT,REJECT"' in sheet and '"ALL_DAYS,FOLLOWING_TWO_DAYS"' in sheet
    assert "<sheetProtection" in sheet and "password" not in sheet
    rows = _rows(content)
    assert rows[0][workbook.INDEX["participant"]] == '=HYPERLINK("http://example.invalid")'
    # No identity number, document link or token column exists.
    lowered = " ".join(workbook.HEADERS).lower()
    for forbidden in ("nin", "passport", "document", "photo", "token", "password", "otp"):
        assert forbidden not in lowered.split()


def test_export_refuses_more_rows_than_one_workbook_holds(event, operator, settings) -> None:
    configure_attendance(event)
    for _ in range(3):
        actionable(event, operator, prerequisites=False)
    settings.REVIEW_DECISION_WORKBOOK_MAX_ROWS = 2
    with pytest.raises(workbook.WorkbookTooLargeError) as refusal:
        _export(operator)
    assert refusal.value.count == 3
    assert not workbook.ReviewDecisionExport.objects.exists()


# ---------------------------------------------------------------------------
# Round trip: preview has no effect; final validation applies through the services
# ---------------------------------------------------------------------------


def test_mixed_round_trip_preview_then_apply_once(event, operator) -> None:
    configure_attendance(event, capacity=10)
    all_days, two_days, rejected, kept = (actionable(event, operator) for _ in range(4))
    _export_record, content = _export(operator)
    edited = edit(
        content,
        {
            all_days.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
            two_days.public_reference: {"decision": "accept", "profile": "FOLLOWING_TWO_DAYS"},
            rejected.public_reference: {
                "decision": "REJECT",
                "reason": "NOT_ELIGIBLE",
                "note": "Internal synthetic note",
            },
        },
    )
    before = _business_snapshot()
    preview = _preview(operator, edited)
    assert preview.counts == {
        "total_rows": 4,
        "no_change": 1,
        "accept_all_days": 1,
        "accept_following_two_days": 1,
        "reject": 1,
        "invalid": 0,
        "invalid_stale": 0,
        "invalid_out_of_scope": 0,
        "valid_changes": 3,
    }
    # Upload and preview change nothing of the business state.
    assert _business_snapshot() == before
    assert workbook.is_applicable(preview)
    assert _row(preview, rejected).internal_note_encrypted == "Internal synthetic note"

    applied = workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert applied.status == ReviewDecisionImportStatus.APPLIED
    assert applied.counts["applied"] == 3
    for registration, status in (
        (all_days, RegistrationPublicStatus.APPROVED),
        (two_days, RegistrationPublicStatus.APPROVED),
        (rejected, RegistrationPublicStatus.NOT_APPROVED),
        (kept, RegistrationPublicStatus.UNDER_REVIEW),
    ):
        registration.refresh_from_db()
        assert registration.public_status == status
    assert AttendanceEntitlement.objects.get(registration=all_days).category == ALL
    assert AttendanceEntitlement.objects.get(registration=two_days).category == FOLLOWING
    rejection = RegistrationDecision.objects.get(registration=rejected)
    assert rejection.outcome == RegistrationDecisionOutcome.NOT_APPROVED
    assert rejection.internal_reason_code == "NOT_ELIGIBLE"
    assert rejection.internal_note_encrypted == "Internal synthetic note"
    assert not RegistrationDecision.objects.filter(registration=kept).exists()
    # Each decision is linked from its row and audited with the batch correlation.
    for registration in (all_days, two_days, rejected):
        row = _row(applied, registration)
        assert row.applied_decision.registration_id == registration.pk
        assert AuditEvent.objects.filter(
            action_code=action_codes.REVIEW_DECISION_RECORDED,
            target_uuid=registration.pk,
            correlation_id=f"decision-workbook:{preview.pk}",
        ).exists()
    # The normal notifications were queued (one per decision), none for KEEP.
    for registration in (all_days, two_days, rejected):
        assert CommunicationMessage.objects.filter(registration=registration).count() == 1
    assert not CommunicationMessage.objects.filter(registration=kept).exists()
    # The note now lives in the decision only.
    assert _row(applied, rejected).internal_note_encrypted == ""

    # Applying the same batch again (double click, retry) changes nothing.
    after = _business_snapshot()
    again = workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert again.status == ReviewDecisionImportStatus.APPLIED
    assert _business_snapshot() == after
    # A second upload of the same workbook is stale now: nothing is applied twice.
    second = _preview(operator, edited)
    assert second.counts["invalid"] == 3 and second.counts["valid_changes"] == 0
    assert not workbook.is_applicable(second)


def test_an_untouched_workbook_proposes_nothing(event, operator) -> None:
    configure_attendance(event)
    for _ in range(3):
        actionable(event, operator)
    _export_record, content = _export(operator)
    before = _business_snapshot()
    preview = _preview(operator, content)
    assert preview.counts["no_change"] == 3 and preview.counts["valid_changes"] == 0
    assert not workbook.is_applicable(preview)
    with pytest.raises(workbook.ApplyRefused) as refusal:
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert refusal.value.code == "NOTHING_TO_APPLY"
    assert _business_snapshot() == before


# ---------------------------------------------------------------------------
# Invalid rows
# ---------------------------------------------------------------------------


def test_invalid_rows_are_reported_and_block_the_whole_batch(event, operator) -> None:
    configure_attendance(event)
    regs = [actionable(event, operator) for _ in range(9)]
    _export_record, content = _export(operator)
    ref = [registration.public_reference for registration in regs]
    original_rows = _rows(content)
    duplicate = next(row for row in original_rows if row[0] == ref[8])
    unknown = list(duplicate)
    unknown[workbook.INDEX["reference"]] = "REV-UNKNOWN"
    unknown[workbook.INDEX["registration_id"]] = str(uuid.uuid4())
    edited = edit(
        content,
        {
            ref[0]: {"decision": "ACCEPT"},  # no profile
            ref[1]: {"decision": "REJECT"},  # no reason
            ref[2]: {"decision": "ACCEPT", "profile": "ALL_DAYS", "reason": "X"},
            ref[3]: {"decision": "KEEP", "profile": "ALL_DAYS"},
            ref[4]: {"decision": "ACCEPT", "profile": "OPENING_DAY_ONLY"},
            ref[5]: {"decision": "ACCEPT", "profile": "ALL_DAYS", "registration_version": "999"},
            ref[6]: {"decision": "REJECT", "reason": "=1+1"},
            ref[7]: {"decision": "APPROVE"},
            ref[8]: {"decision": "ACCEPT", "profile": "FOLLOWING_TWO_DAYS"},
        },
        extra_rows=[duplicate, unknown],
    )
    before = _business_snapshot()
    preview = _preview(operator, edited)
    codes = {row.reference_text: row.error_codes for row in preview.rows.all() if row.error_codes}
    assert codes[ref[0]] == ["PROFILE_REQUIRED"]
    assert codes[ref[1]] == ["REASON_REQUIRED"]
    assert codes[ref[2]] == ["REASON_NOT_ALLOWED"]
    assert codes[ref[3]] == ["KEEP_WITH_DECISION_FIELDS"]
    assert codes[ref[4]] == ["INVALID_PROFILE"]
    assert codes[ref[5]] == ["TAMPERED_METADATA"]
    assert codes[ref[6]] == ["INVALID_REASON"]
    assert codes[ref[7]] == ["INVALID_DECISION"]
    assert codes[ref[8]] == ["DUPLICATE_ROW"]
    assert codes["REV-UNKNOWN"] == ["UNKNOWN_ROW", "DUPLICATE_ROW"]
    assert preview.counts["invalid"] == 11 and preview.counts["valid_changes"] == 0
    assert not workbook.is_applicable(preview)
    with pytest.raises(workbook.ApplyRefused) as refusal:
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert refusal.value.code == "INVALID_ROWS"
    assert _business_snapshot() == before
    # The correction report names rows and references, never a participant name.
    report = workbook.error_report_csv(preview).decode("utf-8-sig")
    assert ref[0] in report and "PROFILE_REQUIRED" in report


def test_one_invalid_row_prevents_every_valid_one(event, operator) -> None:
    configure_attendance(event)
    good, bad = actionable(event, operator), actionable(event, operator)
    _export_record, content = _export(operator)
    edited = edit(
        content,
        {
            good.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
            bad.public_reference: {"decision": "REJECT"},
        },
    )
    preview = _preview(operator, edited)
    assert preview.counts["valid_changes"] == 1 and preview.counts["invalid"] == 1
    assert not workbook.is_applicable(preview)
    with pytest.raises(workbook.ApplyRefused):
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    good.refresh_from_db()
    assert good.public_status == RegistrationPublicStatus.UNDER_REVIEW


def test_formulas_are_refused_never_evaluated(event, operator) -> None:
    configure_attendance(event)
    registration = actionable(event, operator)
    _export_record, content = _export(operator)
    edited = edit(content, {registration.public_reference: {"decision": "ACCEPT"}})
    # Put a formula into the decision cell, as Excel stores one.
    archive = zipfile.ZipFile(io.BytesIO(edited))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for item in archive.infolist():
            data = archive.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text = data.decode()
                start = text.index('<c r="H2"')
                end = text.index("</c>", start) + 4
                text = (
                    text[:start]
                    + '<c r="H2" t="str"><f>"ACC"&amp;"EPT"</f><v>ACCEPT</v></c>'
                    + text[end:]
                )
                data = text.encode()
            target.writestr(item, data)
    preview = _preview(operator, out.getvalue())
    assert _row(preview, registration).error_codes[0] == "FORMULA_NOT_ALLOWED"
    assert preview.counts["valid_changes"] == 0


# ---------------------------------------------------------------------------
# Staleness, scope, capacity
# ---------------------------------------------------------------------------


def test_a_change_after_export_makes_the_row_stale(event, operator) -> None:
    from apps.reviews.services import assign_review_case

    configure_attendance(event)
    registration = actionable(event, operator)
    _export_record, content = _export(operator)
    case = ReviewCase.objects.get(registration=registration)
    assign_review_case(
        review_case=case,
        expected_version=case.version,
        assigned_by=operator,
        assigned_user=operator,
    )
    preview = _preview(
        operator,
        edit(
            content, {registration.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"}}
        ),
    )
    assert _row(preview, registration).error_codes == ["STALE"]
    assert preview.counts["invalid_stale"] == 1


def test_a_change_after_preview_refuses_the_whole_batch(event, operator) -> None:
    from apps.reviews.services import record_not_approved_decision

    configure_attendance(event)
    first, second = actionable(event, operator), actionable(event, operator)
    _export_record, content = _export(operator)
    preview = _preview(
        operator,
        edit(
            content,
            {
                first.public_reference: {"decision": "ACCEPT", "profile": "FOLLOWING_TWO_DAYS"},
                second.public_reference: {"decision": "REJECT", "reason": "NOT_ELIGIBLE"},
            },
        ),
    )
    assert workbook.is_applicable(preview)
    # Someone decides the second registration individually meanwhile.
    second.refresh_from_db()
    record_not_approved_decision(
        registration=second,
        expected_version=second.version,
        internal_reason_code="OTHER",
        decided_by=operator,
    )
    with pytest.raises(workbook.ApplyRefused) as refusal:
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert refusal.value.code == "STALE"
    first.refresh_from_db()
    assert first.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert not RegistrationDecision.objects.filter(registration=first).exists()
    preview.refresh_from_db()
    assert preview.status == ReviewDecisionImportStatus.REFUSED
    assert AuditEvent.objects.filter(
        action_code=action_codes.REVIEW_DECISION_WORKBOOK_APPLY_REFUSED, target_uuid=preview.pk
    ).exists()
    # A refused preview can never be applied later.
    with pytest.raises(workbook.ApplyRefused) as again:
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert again.value.code == "NOT_APPLICABLE"


def test_a_decision_refused_at_final_validation_rolls_back_all(event, operator) -> None:
    """Capacity taken between preview and apply: the service refuses one
    approval, so none of the batch is recorded."""
    configure_attendance(event, capacity=2)
    first, second, other = (actionable(event, operator) for _ in range(3))
    _export_record, content = _export(operator, ReviewCase.objects.exclude(registration=other))
    preview = _preview(
        operator,
        edit(
            content,
            {
                first.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
                second.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
            },
        ),
    )
    assert workbook.is_applicable(preview)
    from apps.reviews.services import record_approved_decision

    other.refresh_from_db()
    record_approved_decision(
        registration=other,
        expected_version=other.version,
        decided_by=operator,
        attendance_category=ALL,
    )
    messages_before = CommunicationMessage.objects.count()
    with pytest.raises(workbook.ApplyRefused) as refusal:
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert refusal.value.code == "DECISION_REFUSED"
    for registration in (first, second):
        registration.refresh_from_db()
        assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert CommunicationMessage.objects.count() == messages_before


def test_opening_day_capacity_is_checked_in_the_preview(event, operator) -> None:
    configure_attendance(event, capacity=1)
    first, second = actionable(event, operator), actionable(event, operator)
    _export_record, content = _export(operator)
    preview = _preview(
        operator,
        edit(
            content,
            {
                first.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
                second.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
            },
        ),
    )
    assert preview.counts["invalid"] == 2
    assert _row(preview, first).error_codes == ["OPENING_DAY_CAPACITY"]


def test_missing_approval_prerequisites_are_reported(event, operator) -> None:
    configure_attendance(event)
    registration = actionable(event, operator, prerequisites=False)
    _export_record, content = _export(operator)
    preview = _preview(
        operator,
        edit(
            content, {registration.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"}}
        ),
    )
    assert _row(preview, registration).error_codes == ["MISSING_ASSIGNMENTS"]


def test_a_row_outside_the_decision_scope_is_refused(event, organization, operator) -> None:
    from apps.accounts.models import ScopedGroupMembership

    configure_attendance(event)
    registration = actionable(event, operator, organization=organization)
    _export_record, content = _export(operator)
    # The decision scope is narrowed to another organization after the export.
    from apps.organizations.models import Organization, OrganizationType

    elsewhere = Organization.objects.create(
        official_name="Elsewhere",
        normalized_name="elsewhere",
        organization_type=OrganizationType.MINISTRY,
    )
    ScopedGroupMembership.objects.filter(
        user=operator, group__name=ACCREDITATION_MANAGERS_GROUP_NAME
    ).update(organization=elsewhere)
    preview = _preview(
        operator,
        edit(content, {registration.public_reference: {"decision": "REJECT", "reason": "X1"}}),
    )
    assert _row(preview, registration).error_codes == ["OUT_OF_SCOPE"]
    assert preview.counts["invalid_out_of_scope"] == 1


def test_identity_changes_after_export_are_refused(event, operator) -> None:
    """An identity decision since the export makes the row stale; an identity
    document that expired since (no version change) makes it ineligible."""
    from django.db.models import F

    from apps.people.models import IdentityIdentifier, IdentityVerification

    configure_attendance(event)
    reopened, expired = actionable(event, operator), actionable(event, operator)
    _export_record, content = _export(operator)
    IdentityVerification.objects.filter(registration=reopened).update(
        status="MANUAL_REVIEW", verification_source="", version=F("version") + 1
    )
    IdentityIdentifier.objects.filter(
        pk=IdentityVerification.objects.get(registration=expired).current_revision.identifier_id
    ).update(expires_at=timezone.now().date() - timedelta(days=1))
    accept = {"decision": "ACCEPT", "profile": "ALL_DAYS"}
    preview = _preview(
        operator,
        edit(content, {reopened.public_reference: accept, expired.public_reference: accept}),
    )
    assert _row(preview, reopened).error_codes == ["STALE"]
    assert _row(preview, expired).error_codes == ["NOT_ELIGIBLE", "DOCUMENT_EXPIRED"]


# ---------------------------------------------------------------------------
# Provenance and permissions
# ---------------------------------------------------------------------------


def test_another_accounts_export_or_an_expired_export_is_refused(event, operator) -> None:
    configure_attendance(event)
    actionable(event, operator)
    export, content = _export(operator)
    colleague = make_operational_user_with_membership(
        email="workbook-colleague@example.test",
        group_name=ACCREDITATION_MANAGERS_GROUP_NAME,
        event_edition=event,
    )
    _grant(colleague, REVIEW_DECISION_WORKBOOK_GROUP_NAME, event=event)
    with pytest.raises(workbook.WorkbookError) as refusal:
        _preview(colleague, content)
    assert refusal.value.code == "EXPORT_OF_ANOTHER_ACCOUNT"
    workbook.ReviewDecisionExport.objects.filter(pk=export.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(workbook.WorkbookError) as expired:
        _preview(operator, content)
    assert expired.value.code == "EXPORT_EXPIRED"
    assert not ReviewDecisionImport.objects.exists()


def test_bulk_permission_is_required_on_top_of_the_decision_permission(event) -> None:
    configure_attendance(event)
    manager_only = make_operational_user_with_membership(
        email="workbook-manager-only@example.test",
        group_name=ACCREDITATION_MANAGERS_GROUP_NAME,
        event_edition=event,
    )
    workbook_only = make_operational_user_with_membership(
        email="workbook-only@example.test",
        group_name=REVIEW_DECISION_WORKBOOK_GROUP_NAME,
        event_edition=event,
    )
    reviewer = make_operational_user_with_membership(
        email="workbook-reviewer@example.test",
        group_name=REGISTRATION_REVIEWERS_GROUP_NAME,
        event_edition=event,
    )
    _grant(workbook_only, REGISTRATION_REVIEWERS_GROUP_NAME, event=event)
    actionable(event, manager_only)
    for user in (manager_only, workbook_only, reviewer):
        assert not workbook.can_use_workbook(user)
        with pytest.raises(workbook.WorkbookError):
            _export(user)
    for user in (manager_only, reviewer):
        client = staff_client(user)
        assert client.post(reverse("reviews:decision-workbook-export")).status_code == 403
        assert client.post(reverse("reviews:decision-workbook-import")).status_code == 403
        page = client.get(reverse("reviews:queue-list")).content.decode()
        assert "data-decision-workbook" not in page
    # The workbook role alone passes the view gate but the service refuses it.
    client = staff_client(workbook_only)
    response = client.post(reverse("reviews:decision-workbook-export"))
    assert response.status_code == 302
    assert not workbook.ReviewDecisionExport.objects.exists()


def test_the_workbook_role_is_never_granted_automatically() -> None:
    from django.contrib.auth.models import Group

    from apps.accounts.models import ScopedGroupMembership

    group = Group.objects.get(name=REVIEW_DECISION_WORKBOOK_GROUP_NAME)
    assert list(group.permissions.values_list("codename", flat=True)) == [
        "bulk_registrationdecision"
    ]
    assert not ScopedGroupMembership.objects.filter(group=group).exists()
    for name in (ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME):
        assert (
            not Group.objects.get(name=name)
            .permissions.filter(codename="bulk_registrationdecision")
            .exists()
        )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def test_queue_tabs_panel_export_import_preview_and_apply_views(event, operator) -> None:
    configure_attendance(event)
    pending = actionable(event, operator)
    done = actionable(event, operator)
    from apps.reviews.services import record_not_approved_decision

    done.refresh_from_db()
    record_not_approved_decision(
        registration=done,
        expected_version=done.version,
        internal_reason_code="NOT_ELIGIBLE",
        decided_by=operator,
    )
    client = staff_client(operator)
    queue = client.get(reverse("reviews:queue-list")).content.decode()
    assert pending.public_reference in queue and done.public_reference not in queue
    assert "data-workbook-export-count>1<" in queue
    history = client.get(reverse("reviews:queue-list") + "?view=history").content.decode()
    assert done.public_reference in history and pending.public_reference not in history

    response = client.post(reverse("reviews:decision-workbook-export"), {"view": "awaiting"})
    assert response.status_code == 200
    assert response["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response["X-ASC-Export-Rows"] == "1"
    assert "attachment" in response["Content-Disposition"]
    edited = edit(
        response.content, {pending.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"}}
    )
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("decisions.xlsx", edited, content_type=workbook_content_type())
    response = client.post(reverse("reviews:decision-workbook-import"), {"workbook": upload})
    assert response.status_code == 302
    decision_import = ReviewDecisionImport.objects.get()
    assert response["Location"] == reverse(
        "reviews:decision-import-detail", kwargs={"pk": decision_import.pk}
    )
    page = client.get(response["Location"]).content.decode()
    assert "data-preview-not-applied" in page and "data-apply-form" in page
    pending.refresh_from_db()
    assert pending.public_status == RegistrationPublicStatus.UNDER_REVIEW
    # Another operator cannot see or apply this preview.
    colleague = make_operational_user_with_membership(
        email="workbook-colleague2@example.test",
        group_name=ACCREDITATION_MANAGERS_GROUP_NAME,
        event_edition=event,
    )
    _grant(colleague, REVIEW_DECISION_WORKBOOK_GROUP_NAME, event=event)
    other = staff_client(colleague)
    assert other.get(response["Location"]).status_code == 404
    assert (
        other.post(
            reverse("reviews:decision-import-apply", kwargs={"pk": decision_import.pk})
        ).status_code
        == 404
    )
    applied = client.post(
        reverse("reviews:decision-import-apply", kwargs={"pk": decision_import.pk})
    )
    assert applied.status_code == 302
    pending.refresh_from_db()
    assert pending.public_status == RegistrationPublicStatus.APPROVED
    page = client.get(response["Location"]).content.decode()
    assert "data-preview-applied" in page and "data-apply-form" not in page


def test_a_bad_upload_is_refused_with_a_message(event, operator) -> None:
    client = staff_client(operator)
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("decisions.xlsx", b"not a workbook", content_type="text/plain")
    response = client.post(reverse("reviews:decision-workbook-import"), {"workbook": upload})
    assert response.status_code == 302
    assert not ReviewDecisionImport.objects.exists()
    assert AuditEvent.objects.filter(
        action_code=action_codes.REVIEW_DECISION_WORKBOOK_REFUSED, reason_code="NOT_XLSX"
    ).exists()


def workbook_content_type() -> str:
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# Retention and the shared entry rule
# ---------------------------------------------------------------------------


def test_expired_previews_lose_their_notes_and_cannot_be_applied(event, operator) -> None:
    configure_attendance(event)
    registration = actionable(event, operator)
    _export_record, content = _export(operator)
    preview = _preview(
        operator,
        edit(
            content,
            {registration.public_reference: {"decision": "REJECT", "reason": "R1", "note": "N1"}},
        ),
    )
    ReviewDecisionImport.objects.filter(pk=preview.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert workbook.purge_expired_decision_workbooks() == 1
    preview.refresh_from_db()
    assert preview.status == ReviewDecisionImportStatus.EXPIRED
    assert _row(preview, registration).internal_note_encrypted == ""
    with pytest.raises(workbook.ApplyRefused):
        workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)
    assert workbook.purge_expired_decision_workbooks() == 0


def test_bulk_and_single_entry_facts_agree(event, operator) -> None:
    configure_attendance(event)
    ready = actionable(event, operator, prerequisites=False)
    unverified = make_registration(event=event)
    special = actionable(event, operator, prerequisites=False)
    from apps.reviews.services import open_review_case

    open_review_case(registration=special, case_type="RESTRICTED", queue_code="RESTRICTED")
    registrations = list(Registration.objects.filter(pk__in=[ready.pk, unverified.pk, special.pk]))
    bulk = intake.bulk_entry_facts(registrations)
    for registration in registrations:
        single = intake.entry_facts(registration)
        assert bulk[registration.pk] == single
        assert intake.evaluate_entry(registration, single) == intake.entry_problem(registration)[0]
    assert intake.entry_problem(ready)[0] == ""
    assert intake.entry_problem(special)[0] == intake.EntryExclusion.SPECIAL_REVIEW_OPEN


# ---------------------------------------------------------------------------
# The hardened reader
# ---------------------------------------------------------------------------


def _zip(members: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


def _minimal(extra: dict | None = None, sheet: str = "") -> dict:
    members = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/worksheets/sheet1.xml": sheet
        or (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData/></worksheet>"
        ),
    }
    members.update(extra or {})
    return members


LIMITS = ReadLimits(max_bytes=1024 * 1024)


def test_reader_reads_shared_strings_numbers_and_flags_formulas() -> None:
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>7.0</v></c>'
        '<c r="C1"><f>1+1</f><v>2</v></c><c r="D1" t="b"><v>1</v></c></row>'
        "</sheetData></worksheet>"
    )
    shared = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><r><t>AC</t></r><r><t>CEPT</t></r></si></sst>"
    )
    data = _zip(_minimal({"xl/sharedStrings.xml": shared}, sheet))
    cells = read_workbook(data, limits=LIMITS)["S"]
    assert cells.value(1, 0) == "ACCEPT"
    assert cells.value(1, 1) == "7"
    assert cells.cells[(1, 2)].has_formula and cells.row_has_formula(1)
    assert cells.value(1, 3) == "TRUE"


@pytest.mark.parametrize(
    ("members", "code"),
    [
        (_minimal({"xl/vbaProject.bin": "x"}), "MACROS_NOT_ALLOWED"),
        (_minimal({"xl/externalLinks/externalLink1.xml": "<x/>"}), "EXTERNAL_LINKS_NOT_ALLOWED"),
        (
            _minimal(sheet='<!DOCTYPE x [<!ENTITY a "aaaa">]><worksheet><sheetData/></worksheet>'),
            "MALFORMED_FILE",
        ),
        (_minimal({"../evil.xml": "<x/>"}), "MALFORMED_FILE"),
    ],
)
def test_reader_refuses_unsafe_content(members, code) -> None:
    with pytest.raises(WorkbookReadError) as refusal:
        read_workbook(_zip(members), limits=LIMITS)
    assert refusal.value.code == code


def test_reader_refuses_non_zip_and_oversized_input() -> None:
    with pytest.raises(WorkbookReadError) as refusal:
        read_workbook(b"plain text", limits=LIMITS)
    assert refusal.value.code == "NOT_XLSX"
    with pytest.raises(WorkbookReadError) as large:
        read_workbook(b"x" * 20, limits=ReadLimits(max_bytes=10))
    assert large.value.code == "FILE_TOO_LARGE"
    bomb = _zip(_minimal({"xl/big.xml": "a" * 4096}))
    with pytest.raises(WorkbookReadError) as expanded:
        read_workbook(bomb, limits=ReadLimits(max_bytes=1024 * 1024, max_member_bytes=1024))
    assert expanded.value.code == "FILE_TOO_LARGE"
