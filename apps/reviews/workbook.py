"""Review decision workbook: Excel export -> edit -> upload preview -> final validation.

A DECISION workflow, never a registration-data import: a workbook can only
propose ACCEPT (with an explicit attendance profile) or REJECT (with a
reason) for rows that were exported to the same operator, and only the
ordinary decision services (`record_approved_decision`,
`record_not_approved_decision`) ever record a decision. Participant details
are never read from the file, an omitted row changes nothing, and nothing is
created or deleted besides the decision history those services write.

1. Export (`export_decision_workbook`): every actionable row matching the
   queue filters in the operator's scope (not just one page), bound to a
   server-side manifest (`ReviewDecisionExport`) holding the exact registration,
   case and identity versions. Display cells are literal text.
2. Preview (`preview_decision_workbook`): the upload is read with strict
   bounds (`apps.core.xlsx`), every row is resolved against the manifest and
   the CURRENT records, and checked for scope, eligibility, staleness and the
   decision prerequisites. Nothing business-relevant is written: no decision,
   no state change, no attendance place, no invitation, badge or message.
   The preview rows are stored for the final step; the file is not.
3. Final validation (`apply_decision_import`): all or nothing. Under the
   locks, every proposed decision is checked again (permission, versions,
   eligibility, prerequisites) and then applied through the decision
   services in ONE transaction; any failure applies none and the preview is
   refused (a fresh export is needed). Applying twice changes nothing more.

Authorization: `reviews.bulk_registrationdecision` (the workbook role, never
granted automatically) AND, for every row, the very rule of an individual
decision: the case must be in `review_cases_visible_to(user,
"add_registrationdecision")`, plus in the bulk permission's own scope.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accreditation.models import AttendanceCategory
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.core.spreadsheet_safety import is_formula_shaped, neutralize_for_spreadsheet
from apps.core.xlsx import (
    STYLE_EDITABLE,
    STYLE_TECHNICAL,
    STYLE_TEXT,
    STYLE_TITLE,
    STYLE_WRAP,
    Column,
    ReadLimits,
    Sheet,
    WorkbookReadError,
    read_workbook,
    write_workbook,
)
from apps.registrations.models import Registration
from apps.reviews.intake import (
    ENTRY_PUBLIC_STATUSES,
    bulk_entry_facts,
    entry_facts,
    evaluate_entry,
)
from apps.reviews.models import (
    ReviewCase,
    ReviewCaseStatus,
    ReviewCaseType,
    ReviewDecisionExport,
    ReviewDecisionImport,
    ReviewDecisionImportRow,
    ReviewDecisionImportRowOutcome,
    ReviewDecisionImportStatus,
    ReviewQueueCode,
)
from apps.reviews.selectors import review_cases_visible_to

TEMPLATE_VERSION = "ASC-RQX-1"
DECISIONS_SHEET = "Decisions"
INSTRUCTIONS_SHEET = "Instructions"
BULK_PERMISSION = "reviews.bulk_registrationdecision"
DECISION_PERMISSION = "reviews.add_registrationdecision"
BULK_CODENAME = "bulk_registrationdecision"
DECISION_CODENAME = "add_registrationdecision"

KEEP = "KEEP"
ACCEPT = "ACCEPT"
REJECT = "REJECT"
DECISIONS = (KEEP, ACCEPT, REJECT)

#: Friendly workbook values -> the existing canonical attendance categories.
PROFILE_ALL_DAYS = "ALL_DAYS"
PROFILE_FOLLOWING_TWO_DAYS = "FOLLOWING_TWO_DAYS"
PROFILES = (PROFILE_ALL_DAYS, PROFILE_FOLLOWING_TWO_DAYS)
PROFILE_TO_CATEGORY = {
    PROFILE_ALL_DAYS: AttendanceCategory.ALL_CONFERENCE_DAYS,
    AttendanceCategory.ALL_CONFERENCE_DAYS: AttendanceCategory.ALL_CONFERENCE_DAYS,
    PROFILE_FOLLOWING_TWO_DAYS: AttendanceCategory.FOLLOWING_TWO_DAYS,
}
CATEGORY_TO_PROFILE = {
    AttendanceCategory.ALL_CONFERENCE_DAYS: PROFILE_ALL_DAYS,
    AttendanceCategory.FOLLOWING_TWO_DAYS: PROFILE_FOLLOWING_TWO_DAYS,
}

#: Bounds of the editable text fields, from the existing decision form/service.
REASON_MAX_LENGTH = 64
NOTE_MAX_LENGTH = 2000


@dataclass(frozen=True)
class _Col:
    key: str
    header: str
    width: float
    style: int


#: The Decisions sheet, in order. Headers are fixed English keys of the
#: template version; the Instructions sheet explains them in the operator's
#: language.
COLUMNS = (
    _Col("reference", "Registration reference", 20, STYLE_TEXT),
    _Col("participant", "Participant", 30, STYLE_TEXT),
    _Col("category", "Registration category", 18, STYLE_TEXT),
    _Col("identity", "Identity verification", 26, STYLE_TEXT),
    _Col("review_status", "Review status", 16, STYLE_TEXT),
    _Col("registration_status", "Registration status", 20, STYLE_TEXT),
    _Col("current_profile", "Current attendance profile", 24, STYLE_TEXT),
    _Col("decision", "Decision", 12, STYLE_EDITABLE),
    _Col("profile", "Attendance profile", 22, STYLE_EDITABLE),
    _Col("reason", "Rejection reason", 26, STYLE_EDITABLE),
    _Col("note", "Internal note", 36, STYLE_EDITABLE),
    _Col("registration_id", "registration_id", 38, STYLE_TECHNICAL),
    _Col("case_id", "review_case_id", 38, STYLE_TECHNICAL),
    _Col("registration_version", "registration_version", 12, STYLE_TECHNICAL),
    _Col("case_version", "review_case_version", 12, STYLE_TECHNICAL),
    _Col("identity_version", "identity_version", 12, STYLE_TECHNICAL),
    _Col("export_id", "export_id", 38, STYLE_TECHNICAL),
    _Col("template_version", "template_version", 14, STYLE_TECHNICAL),
)
INDEX = {column.key: position for position, column in enumerate(COLUMNS)}
HEADERS = [column.header for column in COLUMNS]
META_EXPORT_ID = "export_id"
META_TEMPLATE_VERSION = "template_version"


# ---------------------------------------------------------------------------
# Errors and their messages (codes are stored; messages are shown)
# ---------------------------------------------------------------------------


class WorkbookError(Exception):
    """A refusal of the whole file or request. `code` is safe to show."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class WorkbookTooLargeError(WorkbookError):
    def __init__(self, count: int) -> None:
        super().__init__("EXPORT_TOO_LARGE")
        self.count = count


FILE_ERROR_MESSAGES = {
    "FILE_TOO_LARGE": gettext_lazy("The file is larger than the accepted size."),
    "NOT_XLSX": gettext_lazy("The file is not an Excel workbook (.xlsx)."),
    "MALFORMED_FILE": gettext_lazy("The workbook could not be read safely."),
    "MACROS_NOT_ALLOWED": gettext_lazy("Workbooks with macros are not accepted."),
    "EXTERNAL_LINKS_NOT_ALLOWED": gettext_lazy("Workbooks with external links are not accepted."),
    "ROW_LIMIT_EXCEEDED": gettext_lazy("The workbook has more rows than one import accepts."),
    "MISSING_SHEET": gettext_lazy("The Decisions or Instructions sheet is missing."),
    "HEADER_MISMATCH": gettext_lazy("The Decisions sheet columns were changed."),
    "UNSUPPORTED_TEMPLATE": gettext_lazy("This workbook template version is not supported."),
    "UNKNOWN_EXPORT": gettext_lazy("This workbook does not come from an export of this queue."),
    "EXPORT_EXPIRED": gettext_lazy("This export has expired. Export the queue again."),
    "EXPORT_OF_ANOTHER_ACCOUNT": gettext_lazy(
        "This workbook was exported by another account. Import your own export."
    ),
    "NO_ROWS": gettext_lazy("The Decisions sheet has no rows."),
    "EXPORT_TOO_LARGE": gettext_lazy(
        "Too many rows match these filters for one workbook. Narrow the filters and export again."
    ),
    "NOTHING_TO_EXPORT": gettext_lazy("No row awaiting a decision matches these filters."),
}

ROW_ERROR_MESSAGES = {
    "FORMULA_NOT_ALLOWED": gettext_lazy("A cell holds a formula; type plain values only."),
    "UNKNOWN_ROW": gettext_lazy("This row is not part of the export."),
    "DUPLICATE_ROW": gettext_lazy("The same registration or case appears on several rows."),
    "TAMPERED_METADATA": gettext_lazy("The reference or technical columns were changed."),
    "WRONG_EXPORT": gettext_lazy("This row belongs to another export or template."),
    "INVALID_DECISION": gettext_lazy("Decision must be KEEP, ACCEPT or REJECT."),
    "KEEP_WITH_DECISION_FIELDS": gettext_lazy(
        "KEEP was chosen but a profile, reason or note was filled in. "
        "Clear them or choose a decision."
    ),
    "PROFILE_REQUIRED": gettext_lazy("ACCEPT requires an attendance profile."),
    "INVALID_PROFILE": gettext_lazy("Attendance profile must be ALL_DAYS or FOLLOWING_TWO_DAYS."),
    "REASON_NOT_ALLOWED": gettext_lazy("A rejection reason cannot accompany ACCEPT."),
    "NOTE_NOT_SUPPORTED": gettext_lazy("An internal note can be recorded only with REJECT."),
    "REASON_REQUIRED": gettext_lazy("REJECT requires a rejection reason."),
    "INVALID_REASON": gettext_lazy(
        "The rejection reason must be plain text of at most 64 characters."
    ),
    "INVALID_NOTE": gettext_lazy(
        "The internal note must be plain text of at most 2000 characters."
    ),
    "PROFILE_NOT_ALLOWED": gettext_lazy("An attendance profile cannot accompany REJECT."),
    "OUT_OF_SCOPE": gettext_lazy("You are not authorized to decide this registration."),
    "STALE": gettext_lazy("The registration, its case or its identity changed since the export."),
    "NOT_ELIGIBLE": gettext_lazy(
        "The registration is no longer awaiting a participation decision."
    ),
    "MISSING_ASSIGNMENTS": gettext_lazy(
        "Approval needs a current participant role, badge type and access profile first."
    ),
    "ATTENDANCE_NOT_CONFIGURED": gettext_lazy(
        "The attendance days and opening-day capacity are not configured for this event."
    ),
    "OPENING_DAY_CAPACITY": gettext_lazy(
        "Not enough opening-day places remain for every ALL_DAYS approval of this event."
    ),
}

APPLY_REFUSAL_MESSAGES = {
    "STALE": gettext_lazy(
        "Something changed since the preview. Nothing was applied. Export the queue again."
    ),
    "PREVIEW_EXPIRED": gettext_lazy("This preview has expired. Nothing was applied."),
    "INVALID_ROWS": gettext_lazy("The preview has invalid rows. Nothing was applied."),
    "NOTHING_TO_APPLY": gettext_lazy("The preview has no proposed decision."),
    "NOT_APPLICABLE": gettext_lazy("This preview can no longer be applied."),
    "DECISION_REFUSED": gettext_lazy(
        "A decision was refused at final validation. Nothing was applied. Export the queue again."
    ),
}


def row_error_message(code: str) -> str:
    """A translated message for a stored row error code (entry exclusion and
    identity clearance codes included)."""
    if code in ROW_ERROR_MESSAGES:
        return str(ROW_ERROR_MESSAGES[code])
    from apps.people.selectors.clearance import CLEARANCE_MESSAGES

    if code in CLEARANCE_MESSAGES:
        return str(CLEARANCE_MESSAGES[code])
    return str(ENTRY_EXCLUSION_MESSAGES.get(code, code))


ENTRY_EXCLUSION_MESSAGES = {
    "NOT_SUBMITTED": gettext_lazy("The registration is not submitted."),
    "WITHDRAWN": gettext_lazy("The registration was withdrawn or cancelled."),
    "FINAL_DECISION": gettext_lazy("The registration already has a participation decision."),
    "NOT_CURRENT_CONTEXT": gettext_lazy("The registration was replaced by a newer one."),
    "CORRECTION_OR_INFORMATION_REQUESTED": gettext_lazy(
        "A correction or additional information is requested from the participant."
    ),
    "UNEXPECTED_INTERNAL_STATUS": gettext_lazy(
        "The registration's internal status does not allow it."
    ),
    "DUPLICATE_REVIEW": gettext_lazy("A duplicate review is pending."),
    "SPECIAL_REVIEW_OPEN": gettext_lazy("A duplicate, restricted or specialist review is open."),
    "MULTIPLE_OPEN_STANDARD_CASES": gettext_lazy(
        "Several open review cases exist; resolve them first."
    ),
}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def max_rows() -> int:
    return int(settings.REVIEW_DECISION_WORKBOOK_MAX_ROWS)


def can_use_workbook(user) -> bool:
    from apps.accounts.policies import has_scoped_permission

    return has_scoped_permission(user, BULK_PERMISSION) and has_scoped_permission(
        user, DECISION_PERMISSION
    )


def decidable_case_ids(user, case_ids) -> set:
    """The cases among `case_ids` this user may decide through the workbook:
    the individual decision rule AND the bulk permission, each in its own scope."""
    case_ids = list(case_ids)
    if not case_ids:
        return set()
    decide = review_cases_visible_to(user, codename=DECISION_CODENAME).filter(pk__in=case_ids)
    bulk = review_cases_visible_to(user, codename=BULK_CODENAME).filter(pk__in=case_ids)
    return set(decide.values_list("pk", flat=True)) & set(bulk.values_list("pk", flat=True))


def actionable_cases(user, cases_queryset):
    """Open STANDARD / GENERAL cases of `cases_queryset` (the queue's filtered,
    scoped queryset) that this user may decide in bulk, with every check of
    the shared entry rule. Returns `(cases, facts)` in queue order."""
    candidates = list(
        cases_queryset.filter(
            case_type=ReviewCaseType.STANDARD,
            queue_code=ReviewQueueCode.GENERAL,
            status__in=ReviewCaseStatus.open_statuses(),
            registration__public_status__in=ENTRY_PUBLIC_STATUSES,
        )
        .select_related("registration", "registration__profile")
        .order_by("-priority", "opened_at", "pk")
        .distinct()
    )
    allowed = decidable_case_ids(user, [case.pk for case in candidates])
    candidates = [case for case in candidates if case.pk in allowed]
    facts = bulk_entry_facts([case.registration for case in candidates])
    actionable = []
    for case in candidates:
        registration_facts = facts[case.registration_id]
        if evaluate_entry(case.registration, registration_facts):
            continue
        if [open_case.pk for open_case in registration_facts.open_cases] != [case.pk]:
            continue
        actionable.append(case)
    return actionable, facts


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _participant_name(registration) -> str:
    profile = getattr(registration, "profile", None)
    if profile is None:
        return ""
    name = profile.submitted_full_name or " ".join(
        part for part in (profile.submitted_given_names, profile.submitted_family_name) if part
    )
    return name[:200]


def _identity_label(status: str) -> str:
    from apps.people.models import IdentityStatus

    return str(dict(IdentityStatus.choices).get(status, status))


def filter_codes(filters: dict) -> str:
    """The active filters as `key=value` codes ("" without any filter)."""
    return "; ".join(f"{key}={value}" for key, value in sorted(filters.items()) if value)


def describe_filters(filters: dict) -> str:
    """A short description of the export scope for the workbook."""
    return filter_codes(filters) or _("no filter (whole decision scope)")


def export_decision_workbook(*, user, cases_queryset, filters: dict) -> tuple:
    """Build the workbook for every actionable row (all pages) and its manifest.

    Returns `(export, workbook_bytes)`. Raises `WorkbookError` when nothing
    matches and `WorkbookTooLargeError` above the row bound (never truncated).
    """
    from apps.accreditation import attendance

    if not can_use_workbook(user):
        raise WorkbookError("NOT_AUTHORIZED")
    cases, facts = actionable_cases(user, cases_queryset)
    limit = int(settings.REVIEW_DECISION_WORKBOOK_MAX_ROWS)
    if not cases:
        raise WorkbookError("NOTHING_TO_EXPORT")
    if len(cases) > limit:
        raise WorkbookTooLargeError(len(cases))

    now = timezone.now()
    export = ReviewDecisionExport(
        created_by=user,
        template_version=TEMPLATE_VERSION,
        filters={key: str(value)[:64] for key, value in filters.items() if value},
        row_count=len(cases),
        expires_at=now + timedelta(seconds=int(settings.REVIEW_DECISION_EXPORT_TTL_SECONDS)),
    )
    manifest = []
    rows = []
    for case in cases:
        registration = case.registration
        registration_facts = facts[registration.pk]
        entitlement = attendance.current_entitlement(registration.pk)
        manifest.append(
            {
                "r": str(registration.pk),
                "c": str(case.pk),
                "rv": registration.version,
                "cv": case.version,
                "iv": registration_facts.identity_version,
            }
        )
        values = {
            "reference": registration.public_reference,
            "participant": _participant_name(registration),
            "category": registration.get_source_kind_display(),
            "identity": _identity_label(registration_facts.identity_status),
            "review_status": case.get_status_display(),
            "registration_status": registration.get_public_status_display(),
            "current_profile": CATEGORY_TO_PROFILE.get(getattr(entitlement, "category", ""), ""),
            "decision": KEEP,
            "profile": "",
            "reason": "",
            "note": "",
            "registration_id": str(registration.pk),
            "case_id": str(case.pk),
            "registration_version": str(registration.version),
            "case_version": str(case.version),
            "identity_version": str(registration_facts.identity_version or ""),
            "export_id": str(export.pk),
            "template_version": TEMPLATE_VERSION,
        }
        rows.append([str(values[column.key]) for column in COLUMNS])
    export.rows = manifest
    content = _build_workbook(export, rows, now=now)
    export.content_sha256 = hashlib.sha256(content).hexdigest()
    with transaction.atomic():
        export.save()
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=user.pk,
                action_code=action_codes.REVIEW_DECISION_WORKBOOK_EXPORTED,
                target_type="ReviewDecisionExport",
                target_uuid=export.pk,
                result="SUCCESS",
                after_summary={"row_count": export.row_count, "template": TEMPLATE_VERSION},
            )
        )
    return export, content


def _instructions(export, *, now) -> list[list[str]]:
    lines = [
        [_("ASC 2026 review decisions workbook"), ""],
        [META_EXPORT_ID, str(export.pk)],
        [META_TEMPLATE_VERSION, TEMPLATE_VERSION],
        [_("Exported at (UTC)"), now.strftime("%Y-%m-%d %H:%M")],
        [_("Rows"), str(export.row_count)],
        [_("Scope"), describe_filters(export.filters)],
        [_("Valid until (UTC)"), export.expires_at.strftime("%Y-%m-%d %H:%M")],
        ["", ""],
        [_("How to use"), _("Edit only the yellow columns of the Decisions sheet.")],
        [
            "Decision",
            _(
                "KEEP (default) changes nothing. ACCEPT approves. "
                "REJECT records a Not Approved decision."
            ),
        ],
        [
            "Attendance profile",
            _(
                "Required for ACCEPT: ALL_DAYS (all three conference days, including the "
                "opening day) or FOLLOWING_TWO_DAYS (the two days after the opening day). "
                "Leave empty for KEEP and REJECT."
            ),
        ],
        [
            "Rejection reason",
            _("Required for REJECT: a short internal reason code (at most 64 characters)."),
        ],
        [
            "Internal note",
            _("Optional, REJECT only. Internal: never shown to the participant."),
        ],
        [
            _("Technical columns"),
            _("Do not change the reference or the grey technical columns; the server checks them."),
        ],
        [
            _("Import"),
            _(
                "Upload the saved workbook from the review queue. The preview only checks it: "
                "nothing is decided until you choose “Validate and apply decisions”."
            ),
        ],
        [
            _("All or nothing"),
            _(
                "If any row is invalid or anything changed since the export, no decision is "
                "applied. Export again and repeat your decisions."
            ),
        ],
    ]
    return lines


def _build_workbook(export, rows, *, now) -> bytes:
    decisions = Sheet(
        name=DECISIONS_SHEET,
        columns=[Column(column.header, column.width, column.style) for column in COLUMNS],
        rows=rows,
        validations=[
            (INDEX["decision"], DECISIONS),
            (INDEX["profile"], PROFILES),
        ],
        protect=True,
    )
    instructions = Sheet(
        name=INSTRUCTIONS_SHEET,
        columns=[Column("", 28, STYLE_TEXT), Column("", 100, STYLE_WRAP)],
        rows=_instructions(export, now=now),
        header=False,
        freeze_header=False,
        auto_filter=False,
        protect=True,
        row_styles={0: STYLE_TITLE},
    )
    return write_workbook([decisions, instructions])


# ---------------------------------------------------------------------------
# Import preview
# ---------------------------------------------------------------------------


def _parse_uuid(value: str):
    try:
        return uuid.UUID(value.strip())
    except (AttributeError, ValueError):  # fmt: skip
        return None


def _sanitize(value: str, length: int) -> str:
    return "".join(char for char in value if ord(char) >= 0x20)[:length]


def _has_control_chars(value: str, *, allow_newlines: bool = False) -> bool:
    allowed = {"\n", "\r", "\t"} if allow_newlines else set()
    return any(ord(char) < 0x20 and char not in allowed for char in value)


@dataclass
class _ParsedRow:
    row_number: int
    reference: str
    registration_id: object
    case_id: object
    decision: str
    category: str
    reason: str
    note: str
    errors: list
    manifest: dict | None = None


def _read_limits() -> ReadLimits:
    max_rows = int(settings.REVIEW_DECISION_WORKBOOK_MAX_ROWS)
    return ReadLimits(
        max_bytes=int(settings.REVIEW_DECISION_WORKBOOK_MAX_BYTES),
        max_cells=(max_rows + 50) * (len(COLUMNS) + 4) + 200,
    )


def _load_export(sheets, user):
    decisions = sheets.get(DECISIONS_SHEET)
    instructions = sheets.get(INSTRUCTIONS_SHEET)
    if decisions is None or instructions is None:
        raise WorkbookError("MISSING_SHEET")
    if decisions.row_values(1, len(COLUMNS)) != HEADERS:
        raise WorkbookError("HEADER_MISMATCH")
    meta = {}
    for row in range(1, min(instructions.max_row, 40) + 1):
        meta.setdefault(instructions.value(row, 0).strip(), instructions.value(row, 1).strip())
    if meta.get(META_TEMPLATE_VERSION) != TEMPLATE_VERSION:
        raise WorkbookError("UNSUPPORTED_TEMPLATE")
    export_id = _parse_uuid(meta.get(META_EXPORT_ID, ""))
    export = (
        ReviewDecisionExport.objects.filter(pk=export_id, template_version=TEMPLATE_VERSION).first()
        if export_id
        else None
    )
    if export is None:
        raise WorkbookError("UNKNOWN_EXPORT")
    if export.created_by_id != user.pk:
        raise WorkbookError("EXPORT_OF_ANOTHER_ACCOUNT")
    if export.expires_at <= timezone.now():
        raise WorkbookError("EXPORT_EXPIRED")
    return export, decisions


def _parse_rows(decisions, export) -> list[_ParsedRow]:
    manifest = {row["r"]: row for row in export.rows}
    data_rows = [
        row
        for row in range(2, decisions.max_row + 1)
        if any(value.strip() for value in decisions.row_values(row, len(COLUMNS)))
        or decisions.row_has_formula(row)
    ]
    if not data_rows:
        raise WorkbookError("NO_ROWS")
    if len(data_rows) > int(settings.REVIEW_DECISION_WORKBOOK_MAX_ROWS):
        raise WorkbookError("ROW_LIMIT_EXCEEDED")
    parsed = []
    for row in data_rows:
        values = decisions.row_values(row, len(COLUMNS))
        errors: list[str] = []
        if decisions.row_has_formula(row):
            errors.append("FORMULA_NOT_ALLOWED")
        registration_id = _parse_uuid(values[INDEX["registration_id"]])
        case_id = _parse_uuid(values[INDEX["case_id"]])
        entry = manifest.get(str(registration_id)) if registration_id else None
        if entry is None:
            errors.append("UNKNOWN_ROW")
        else:
            if (
                str(case_id) != entry["c"]
                or values[INDEX["registration_version"]].strip() != str(entry["rv"])
                or values[INDEX["case_version"]].strip() != str(entry["cv"])
                or values[INDEX["identity_version"]].strip() != str(entry["iv"] or "")
            ):
                errors.append("TAMPERED_METADATA")
            if (
                values[INDEX["export_id"]].strip() != str(export.pk)
                or values[INDEX["template_version"]].strip() != TEMPLATE_VERSION
            ):
                errors.append("WRONG_EXPORT")
        decision = values[INDEX["decision"]].strip().upper() or KEEP
        profile = values[INDEX["profile"]].strip().upper()
        reason = values[INDEX["reason"]].strip()
        note = values[INDEX["note"]].strip()
        category = ""
        if decision not in DECISIONS:
            errors.append("INVALID_DECISION")
        elif decision == KEEP:
            if profile or reason or note:
                errors.append("KEEP_WITH_DECISION_FIELDS")
        elif decision == ACCEPT:
            if not profile:
                errors.append("PROFILE_REQUIRED")
            elif profile not in PROFILE_TO_CATEGORY:
                errors.append("INVALID_PROFILE")
            else:
                category = PROFILE_TO_CATEGORY[profile]
            if reason:
                errors.append("REASON_NOT_ALLOWED")
            if note:
                errors.append("NOTE_NOT_SUPPORTED")
        else:  # REJECT
            if not reason:
                errors.append("REASON_REQUIRED")
            elif (
                len(reason) > REASON_MAX_LENGTH
                or _has_control_chars(reason)
                or is_formula_shaped(reason)
            ):
                errors.append("INVALID_REASON")
            if profile:
                errors.append("PROFILE_NOT_ALLOWED")
            if note and (
                len(note) > NOTE_MAX_LENGTH
                or _has_control_chars(note, allow_newlines=True)
                or is_formula_shaped(note)
            ):
                errors.append("INVALID_NOTE")
        parsed.append(
            _ParsedRow(
                row_number=row,
                reference=_sanitize(values[INDEX["reference"]].strip(), 40),
                registration_id=registration_id if entry else None,
                case_id=uuid.UUID(entry["c"]) if entry else None,
                decision=decision if decision in DECISIONS else "",
                category=category,
                reason=reason[:REASON_MAX_LENGTH],
                note=note[:NOTE_MAX_LENGTH],
                errors=errors,
                manifest=entry,
            )
        )
    _flag_duplicates(parsed, decisions)
    return parsed


def _flag_duplicates(parsed: list[_ParsedRow], decisions) -> None:
    seen_registrations: dict = {}
    seen_cases: dict = {}
    for item in parsed:
        values = decisions.row_values(item.row_number, len(COLUMNS))
        for key, seen in (
            (values[INDEX["registration_id"]].strip().lower(), seen_registrations),
            (values[INDEX["case_id"]].strip().lower(), seen_cases),
        ):
            if key:
                seen.setdefault(key, []).append(item)
    for seen in (seen_registrations, seen_cases):
        for items in seen.values():
            if len(items) > 1:
                for item in items:
                    if "DUPLICATE_ROW" not in item.errors:
                        item.errors.append("DUPLICATE_ROW")


def _business_errors(
    *, item: _ParsedRow, registration, case, facts, allowed_case_ids, check_assignments
) -> list[str]:
    """Scope, staleness, eligibility and decision prerequisites of one proposed
    decision. The same checks run again under the locks at final validation."""
    from apps.accreditation import attendance
    from apps.accreditation.services import has_required_assignments_for_approval

    entry = item.manifest
    if item.case_id not in allowed_case_ids:
        return ["OUT_OF_SCOPE"]
    if case is None or registration is None or facts is None:
        return ["STALE"]
    if (
        registration.version != entry["rv"]
        or case.version != entry["cv"]
        or facts.identity_version != entry["iv"]
        or case.registration_id != registration.pk
        or case.status not in ReviewCaseStatus.open_statuses()
    ):
        return ["STALE"]
    problem = evaluate_entry(registration, facts)
    if problem:
        return ["NOT_ELIGIBLE", problem]
    if [open_case.pk for open_case in facts.open_cases] != [case.pk]:
        return ["STALE"]
    errors = []
    if item.decision == ACCEPT:
        if check_assignments and not has_required_assignments_for_approval(registration):
            errors.append("MISSING_ASSIGNMENTS")
        if not attendance.is_configured(attendance.policy_for(registration.event_edition_id)):
            errors.append("ATTENDANCE_NOT_CONFIGURED")
    return errors


def preview_decision_workbook(*, user, data: bytes, filename: str) -> ReviewDecisionImport:
    """Validate an uploaded workbook and store its preview. No business effect.

    Raises `WorkbookError` for a refusal of the whole file (audited, with the
    code only). Row problems are recorded per row, never raised.
    """
    from apps.accreditation import attendance

    recorder = PersistentAuditRecorder()

    def refuse(code: str):
        recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=user.pk,
                action_code=action_codes.REVIEW_DECISION_WORKBOOK_REFUSED,
                target_type="ReviewDecisionImport",
                result="DENIED",
                reason_code=code,
            )
        )
        return WorkbookError(code)

    if not can_use_workbook(user):
        raise refuse("NOT_AUTHORIZED")
    if not (filename or "").lower().endswith(".xlsx"):
        raise refuse("NOT_XLSX")
    try:
        sheets = read_workbook(data, limits=_read_limits())
        export, decisions = _load_export(sheets, user)
        parsed = _parse_rows(decisions, export)
    except WorkbookReadError as error:
        raise refuse(error.code) from None
    except WorkbookError as error:
        raise refuse(error.code) from None

    registration_ids = [item.registration_id for item in parsed if item.registration_id]
    case_ids = [item.case_id for item in parsed if item.case_id]
    registrations = Registration.objects.in_bulk(registration_ids)
    cases = ReviewCase.objects.in_bulk(case_ids)
    facts = bulk_entry_facts(list(registrations.values()))
    allowed = decidable_case_ids(user, case_ids)
    for item in parsed:
        if item.errors or item.decision == KEEP or not item.decision:
            continue
        item.errors.extend(
            _business_errors(
                item=item,
                registration=registrations.get(item.registration_id),
                case=cases.get(item.case_id),
                facts=facts.get(item.registration_id),
                allowed_case_ids=allowed,
                check_assignments=True,
            )
        )
    # Opening-day places: every ALL_DAYS approval of an event must fit at once.
    by_event: dict = {}
    for item in parsed:
        if not item.errors and item.category == AttendanceCategory.ALL_CONFERENCE_DAYS:
            event_id = registrations[item.registration_id].event_edition_id
            by_event.setdefault(event_id, []).append(item)
    for event_id, items in by_event.items():
        policy = attendance.policy_for(event_id)
        remaining = attendance.attendance_counts(event_id, policy).opening_remaining
        if remaining is not None and len(items) > remaining:
            for item in items:
                item.errors.append("OPENING_DAY_CAPACITY")

    counts = _counts(parsed)
    now = timezone.now()
    with transaction.atomic():
        decision_import = ReviewDecisionImport.objects.create(
            export=export,
            uploaded_by=user,
            content_sha256=hashlib.sha256(data).hexdigest(),
            status=ReviewDecisionImportStatus.PREVIEWED,
            counts=counts,
            expires_at=now + timedelta(seconds=int(settings.REVIEW_DECISION_PREVIEW_TTL_SECONDS)),
        )
        ReviewDecisionImportRow.objects.bulk_create(
            [
                ReviewDecisionImportRow(
                    decision_import=decision_import,
                    row_number=item.row_number,
                    reference_text=(
                        registrations[item.registration_id].public_reference
                        if item.registration_id in registrations
                        else item.reference
                    ),
                    registration_id=item.registration_id
                    if item.registration_id in registrations
                    else None,
                    review_case_id=item.case_id if item.case_id in cases else None,
                    decision=item.decision,
                    attendance_category=item.category,
                    rejection_reason_code=item.reason if item.decision == REJECT else "",
                    internal_note_encrypted=item.note if item.decision == REJECT else "",
                    registration_version=(item.manifest or {}).get("rv"),
                    case_version=(item.manifest or {}).get("cv"),
                    identity_version=(item.manifest or {}).get("iv"),
                    outcome=_outcome(item),
                    error_codes=item.errors,
                )
                for item in parsed
            ]
        )
        recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=user.pk,
                action_code=action_codes.REVIEW_DECISION_WORKBOOK_PREVIEWED,
                target_type="ReviewDecisionImport",
                target_uuid=decision_import.pk,
                result="SUCCESS",
                after_summary={"export": str(export.pk), **counts},
            )
        )
    return decision_import


def _outcome(item: _ParsedRow) -> str:
    if item.errors:
        return ReviewDecisionImportRowOutcome.INVALID
    if item.decision == KEEP:
        return ReviewDecisionImportRowOutcome.NO_CHANGE
    return ReviewDecisionImportRowOutcome.VALID


def _counts(parsed: list[_ParsedRow]) -> dict:
    valid = [item for item in parsed if not item.errors]
    invalid = [item for item in parsed if item.errors]
    counts = {
        "total_rows": len(parsed),
        "no_change": sum(1 for item in valid if item.decision == KEEP),
        "accept_all_days": sum(
            1 for item in valid if item.category == AttendanceCategory.ALL_CONFERENCE_DAYS
        ),
        "accept_following_two_days": sum(
            1 for item in valid if item.category == AttendanceCategory.FOLLOWING_TWO_DAYS
        ),
        "reject": sum(1 for item in valid if item.decision == REJECT),
        "invalid": len(invalid),
        "invalid_stale": sum(1 for item in invalid if "STALE" in item.errors),
        "invalid_out_of_scope": sum(1 for item in invalid if "OUT_OF_SCOPE" in item.errors),
    }
    counts["valid_changes"] = (
        counts["accept_all_days"] + counts["accept_following_two_days"] + counts["reject"]
    )
    return counts


def is_applicable(decision_import: ReviewDecisionImport) -> bool:
    counts = decision_import.counts or {}
    return (
        decision_import.status == ReviewDecisionImportStatus.PREVIEWED
        and decision_import.expires_at > timezone.now()
        and counts.get("invalid", 1) == 0
        and counts.get("valid_changes", 0) > 0
    )


# ---------------------------------------------------------------------------
# Final validation and application (all or nothing)
# ---------------------------------------------------------------------------


class ApplyRefused(Exception):
    """Nothing was applied. `code` is an `APPLY_REFUSAL_MESSAGES` key; `rows`
    holds (row number, error codes) of the rows that failed the recheck."""

    def __init__(self, code: str, rows: list | None = None) -> None:
        self.code = code
        self.rows = rows or []
        super().__init__(code)


class _Refusal(Exception):
    def __init__(self, code: str, rows: list | None = None, *, status: str = "") -> None:
        self.code = code
        self.rows = rows or []
        self.status = status or ReviewDecisionImportStatus.REFUSED
        super().__init__(code)


def apply_decision_import(*, decision_import_id, user) -> ReviewDecisionImport:
    """Apply exactly the server-validated preview, all or nothing.

    The preview row is locked first, so a repeated click waits and then finds
    it APPLIED (returned unchanged: no duplicate decision, message,
    invitation or badge). Every Registration of the batch is then locked in
    id order BEFORE any decision runs, so the attendance-policy lock each
    approval takes later can never wait behind a Registration held by another
    command (no lock cycle with an individual decision). Every proposed
    decision is checked again under those locks; any difference refuses the
    whole batch. Decisions go through the ordinary services, whose messages
    are queued in this transaction and dispatched only after commit.
    """
    from apps.accreditation.attendance import AttendanceError
    from apps.reviews.services import (
        ApprovalRequiresAssignmentsError,
        ApprovalRequiresVerifiedIdentityError,
        InvalidStateTransitionError,
        StaleVersionError,
        record_approved_decision,
        record_not_approved_decision,
    )

    recorder = PersistentAuditRecorder()
    correlation_id = f"decision-workbook:{decision_import_id}"
    try:
        with transaction.atomic():
            locked = ReviewDecisionImport.objects.select_for_update().get(
                pk=decision_import_id, uploaded_by=user
            )
            if locked.status == ReviewDecisionImportStatus.APPLIED:
                return locked
            if locked.status != ReviewDecisionImportStatus.PREVIEWED:
                raise ApplyRefused("NOT_APPLICABLE")
            if locked.expires_at <= timezone.now():
                raise _Refusal("PREVIEW_EXPIRED", status=ReviewDecisionImportStatus.EXPIRED)
            counts = locked.counts or {}
            if counts.get("invalid", 1):
                raise ApplyRefused("INVALID_ROWS")
            if not counts.get("valid_changes"):
                raise ApplyRefused("NOTHING_TO_APPLY")
            if not can_use_workbook(user):
                raise _Refusal("STALE", [(0, ["OUT_OF_SCOPE"])])
            rows = list(
                locked.rows.filter(outcome=ReviewDecisionImportRowOutcome.VALID)
                .exclude(decision="KEEP")
                .order_by("registration_id")
            )
            if len(rows) != counts.get("valid_changes"):
                raise _Refusal("STALE")
            registration_ids = [row.registration_id for row in rows]
            registrations = {
                registration.pk: registration
                for registration in Registration.objects.select_for_update()
                .filter(pk__in=registration_ids)
                .order_by("pk")
            }
            allowed = decidable_case_ids(user, [row.review_case_id for row in rows])
            failures = []
            for row in rows:
                registration = registrations.get(row.registration_id)
                facts = entry_facts(registration, lock=True) if registration else None
                case = next(
                    (
                        case
                        for case in (facts.open_cases if facts else ())
                        if case.pk == row.review_case_id
                    ),
                    None,
                )
                item = _ParsedRow(
                    row_number=row.row_number,
                    reference=row.reference_text,
                    registration_id=row.registration_id,
                    case_id=row.review_case_id,
                    decision=row.decision,
                    category=row.attendance_category,
                    reason=row.rejection_reason_code,
                    note="",
                    errors=[],
                    manifest={
                        "rv": row.registration_version,
                        "cv": row.case_version,
                        "iv": row.identity_version,
                    },
                )
                errors = (
                    _business_errors(
                        item=item,
                        registration=registration,
                        case=case,
                        facts=facts,
                        allowed_case_ids=allowed,
                        check_assignments=True,
                    )
                    if facts is not None
                    else ["STALE"]
                )
                if case is None and not errors:
                    errors = ["STALE"]
                if errors:
                    failures.append((row.row_number, errors))
            if failures:
                raise _Refusal("STALE", failures)

            applied = 0
            for row in rows:
                registration = registrations[row.registration_id]
                try:
                    if row.decision == ACCEPT:
                        decision = record_approved_decision(
                            registration=registration,
                            expected_version=row.registration_version,
                            decided_by=user,
                            attendance_category=row.attendance_category,
                            audit_recorder=recorder,
                            correlation_id=correlation_id,
                        )
                    else:
                        decision = record_not_approved_decision(
                            registration=registration,
                            expected_version=row.registration_version,
                            internal_reason_code=row.rejection_reason_code,
                            decided_by=user,
                            internal_note=row.internal_note_encrypted,
                            audit_recorder=recorder,
                            correlation_id=correlation_id,
                        )
                except (
                    StaleVersionError,
                    InvalidStateTransitionError,
                    ApprovalRequiresAssignmentsError,
                    ApprovalRequiresVerifiedIdentityError,
                    AttendanceError,
                    ValueError,
                ) as error:  # fmt: skip
                    raise _Refusal(
                        "DECISION_REFUSED",
                        [(row.row_number, [getattr(error, "code", "") or type(error).__name__])],
                    ) from None
                row.applied_decision = decision
                # The note now lives (encrypted) in the decision itself.
                row.internal_note_encrypted = ""
                row.save(update_fields=["applied_decision", "internal_note_encrypted"])
                applied += 1
            locked.status = ReviewDecisionImportStatus.APPLIED
            locked.applied_at = timezone.now()
            locked.counts = {**counts, "applied": applied}
            locked.save(update_fields=["status", "applied_at", "counts", "updated_at"])
            recorder.record(
                AuditRecord(
                    actor_type="OPERATIONAL_USER",
                    actor_user_id=user.pk,
                    action_code=action_codes.REVIEW_DECISION_WORKBOOK_APPLIED,
                    target_type="ReviewDecisionImport",
                    target_uuid=locked.pk,
                    result="SUCCESS",
                    after_summary={
                        "applied": applied,
                        "accept_all_days": counts.get("accept_all_days", 0),
                        "accept_following_two_days": counts.get("accept_following_two_days", 0),
                        "reject": counts.get("reject", 0),
                    },
                    correlation_id=correlation_id,
                )
            )
            return locked
    except _Refusal as refusal:
        _record_refusal(decision_import_id, user, refusal)
        raise ApplyRefused(refusal.code, refusal.rows) from None


def _record_refusal(decision_import_id, user, refusal: _Refusal) -> None:
    """After the rollback: mark the preview refused (or expired) so it can never
    be applied, and audit the refusal (codes and counts only)."""
    with transaction.atomic():
        updated = ReviewDecisionImport.objects.filter(
            pk=decision_import_id, status=ReviewDecisionImportStatus.PREVIEWED
        ).update(status=refusal.status, refusal_code=refusal.code[:64], updated_at=timezone.now())
        if updated:
            ReviewDecisionImportRow.objects.filter(decision_import_id=decision_import_id).update(
                internal_note_encrypted=""
            )
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=user.pk,
                action_code=action_codes.REVIEW_DECISION_WORKBOOK_APPLY_REFUSED,
                target_type="ReviewDecisionImport",
                target_uuid=decision_import_id,
                result="DENIED",
                reason_code=refusal.code,
                after_summary={"failed_rows": len(refusal.rows)},
            )
        )


# ---------------------------------------------------------------------------
# Error report and retention
# ---------------------------------------------------------------------------


def error_report_csv(decision_import: ReviewDecisionImport) -> bytes:
    """Row number, reference, decision and messages of every invalid row
    (spreadsheet-neutralized). No participant name and no note."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["row", "registration_reference", "decision", "error_codes", "messages"])
    for row in decision_import.rows.filter(outcome=ReviewDecisionImportRowOutcome.INVALID):
        writer.writerow(
            [
                str(row.row_number),
                neutralize_for_spreadsheet(row.reference_text),
                row.decision,
                " ".join(row.error_codes),
                neutralize_for_spreadsheet(
                    " | ".join(row_error_message(code) for code in row.error_codes)
                ),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def purge_expired_decision_workbooks(*, now=None) -> int:
    """Expire previews past their validity and erase the internal notes of every
    preview that was not applied. Counts and row outcomes stay as evidence.
    Idempotent. Returns the number of previews expired."""
    now = now or timezone.now()
    expired = ReviewDecisionImport.objects.filter(
        status=ReviewDecisionImportStatus.PREVIEWED, expires_at__lte=now
    ).update(status=ReviewDecisionImportStatus.EXPIRED, updated_at=now)
    ReviewDecisionImportRow.objects.filter(
        decision_import__status__in=(
            ReviewDecisionImportStatus.EXPIRED,
            ReviewDecisionImportStatus.REFUSED,
        )
    ).exclude(internal_note_encrypted="").update(internal_note_encrypted="")
    return expired
