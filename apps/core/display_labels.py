"""Translated display labels for stored codes that are not model choices
(UI/UX Completion Gate Stage 3: no raw internal code is shown to a user).

Presentation only. Each family maps a stored code to a lazily translated
label; the stored values, their validation and every business rule stay in
their own apps. `apps/core/tests/test_display_labels.py` fails when a code
the platform can produce has no label here.

Template use: `{{ value|code_label:"export_purpose" }}` (asc_ui filter).
"""

from __future__ import annotations

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

CODE_LABELS: dict[str, dict[str, Promise]] = {
    # apps.exports.models.ALLOWED_EXPORT_PURPOSE_CODES
    "export_purpose": {
        "OPERATIONAL_REPORTING": _("Operational reporting"),
        "EVENT_LOGISTICS_PLANNING": _("Event logistics planning"),
        "ORGANIZATION_DELEGATION_OVERSIGHT": _("Organization delegation oversight"),
    },
    # apps.communications.purposes.CommunicationPurpose (template purpose codes)
    "communication_purpose": {
        "INVITATION": _("Invitation"),
        "REGISTRATION_CONFIRMATION": _("Registration confirmation"),
        "INFORMATION_REQUEST": _("Additional information request"),
        "INFORMATION_RESPONSE_CONFIRMATION": _("Information response confirmation"),
        "DECISION_STATUS": _("Decision status"),
        "ACCOUNT_ACCESS": _("Account access"),
        "DELEGATION_CLAIM": _("Delegation claim"),
        "IDENTITY_REJECTION": _("Identity rejection"),
        "APPROVAL_ATTENDANCE": _("Approval with attendance days"),
        "ATTENDANCE_CHANGE": _("Attendance days update"),
    },
    # apps.exports.models.ExportStatus (its model labels are not translated)
    "export_status": {
        "PENDING": pgettext_lazy("export status", "Pending"),
        "READY": pgettext_lazy("export status", "Ready"),
        "EXPIRED": pgettext_lazy("export status", "Expired"),
    },
    # apps.communications.models.CommunicationMessageStatus (idem)
    "message_status": {
        "QUEUED": pgettext_lazy("message status", "Queued"),
        "SENDING": pgettext_lazy("message status", "Sending"),
        "SENT": pgettext_lazy("message status", "Sent"),
        "DELIVERED": pgettext_lazy("message status", "Delivered"),
        "DEFERRED": pgettext_lazy("message status", "Deferred"),
        "FAILED": pgettext_lazy("message status", "Failed"),
        "BOUNCED": pgettext_lazy("message status", "Bounced"),
        "UNDELIVERABLE": pgettext_lazy("message status", "Undeliverable"),
        "SUPPRESSED": pgettext_lazy("message status", "Suppressed"),
        "CANCELLED": pgettext_lazy("message status", "Cancelled"),
    },
    # settings.LANGUAGES codes, as stored on messages and campaigns
    "language": {
        "en": _("English"),
        "fr": _("French"),
        "ar": _("Arabic"),
    },
    # apps.invitations.services._validate_row_values / header validation
    "delegation_error": {
        "MISSING_EMAIL": _("Email is missing"),
        "INVALID_EMAIL": _("Email is not valid"),
        "MISSING_GIVEN_NAMES": _("Given names are missing"),
        "NON_LATIN_GIVEN_NAMES": _("Given names must use Latin letters"),
        "NON_LATIN_FAMILY_NAME": _("The family name must use Latin letters"),
        "MISSING_FAMILY_NAME": _("Family name is missing"),
        "FORMULA_SHAPED_VALUE": _("A value looks like a spreadsheet formula"),
        "DUPLICATE_COLUMN": _("A column appears twice"),
        "FORBIDDEN_COLUMN": _("A column is not allowed"),
        "UNKNOWN_COLUMN": _("A column is not recognised"),
        "MISSING_REQUIRED_COLUMN": _("A required column is missing"),
    },
    # apps.accreditation.services preview/execution row reasons
    "bulk_reason": {
        "NOT_FOUND": _("Registration not found in your scope"),
        "WRONG_EVENT": _("Belongs to a different event"),
        "ALREADY_CURRENT": _("Already has this assignment"),
        "STALE_REGISTRATION": _("Changed since the preview"),
        "INVALID_TARGET": _("Can no longer receive this assignment"),
    },
    # apps.reviews.services.ALLOWED_CHECKLIST_ITEM_CODES
    "checklist_item": {
        "IDENTITY_PLAUSIBLE": _("Identity details are plausible"),
        "CONTACT_PLAUSIBLE": _("Contact details are plausible"),
        "PROFESSIONAL_INFO_COMPLETE": _("Professional information is complete"),
        "INTERESTS_COMPLETE": _("Interests are complete"),
        "IDENTITY_DOCUMENT_LEGIBLE": _("Identity document is legible"),
        "IDENTITY_NAME_MATCHES": _("Name matches the identity document"),
        "IDENTITY_DOB_MATCHES": _("Date of birth matches the identity document"),
        "IDENTITY_PHOTO_MATCHES": _("Photo matches the identity document"),
        "DUPLICATE_EVIDENCE_REVIEWED": _("Duplicate evidence reviewed"),
        "RESTRICTED_EVIDENCE_REVIEWED": _("Restriction evidence reviewed"),
        "RESTRICTED_ESCALATION_JUSTIFIED": _("Escalation is justified"),
        "RESPONSE_COMPLETE": _("Response is complete"),
        "RESPONSE_CONSISTENT": _("Response is consistent"),
    },
    # apps.reviews.forms._DOCUMENT_TYPE_CHOICES (requested evidence types)
    "request_document": {
        "REQUESTED_EVIDENCE": _("Requested evidence document"),
    },
    # apps.reviews.services.ALLOWED_REQUEST_FIELD_CODES
    "request_field": {
        "given_names": pgettext_lazy("registration field", "Given names"),
        "family_name": pgettext_lazy("registration field", "Family name"),
        "date_of_birth": pgettext_lazy("registration field", "Date of birth"),
        "nationality_code": pgettext_lazy("registration field", "Nationality"),
        "country_of_residence": pgettext_lazy("registration field", "Country of residence"),
        "mobile_number": pgettext_lazy("registration field", "Mobile number"),
        "organization_name": pgettext_lazy("registration field", "Organization name"),
        "job_title": pgettext_lazy("registration field", "Job title"),
        "department": pgettext_lazy("registration field", "Department"),
        "sector": pgettext_lazy("registration field", "Sector"),
        "professional_profile_url": pgettext_lazy("registration field", "Professional profile URL"),
    },
}


def code_label(family: str, code) -> str:
    """The translated label for `code`, or the code itself when unknown (so
    nothing is ever hidden; the coverage test keeps that case unreachable
    for every code the platform produces)."""
    if code in (None, ""):
        return ""
    label = CODE_LABELS.get(family, {}).get(str(code))
    return str(label) if label is not None else str(code)


def code_choices(family: str, codes) -> list[tuple[str, str]]:
    """`(code, label)` pairs for a select, in the given order."""
    return [(code, code_label(family, code)) for code in codes]
