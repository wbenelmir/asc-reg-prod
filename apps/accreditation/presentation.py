"""Localized, safe messages for attendance refusals.

Views never render `str(exc)` of an `apps.accreditation.attendance` error (a
developer-facing English message): they call `attendance_error_message`,
which maps the error's stable `code` to one of the translated messages below,
and `readiness_label` for the activation checklist.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.accreditation import attendance

_MESSAGES = {
    "CHOICE_REQUIRED": _(
        "Choose the attendance days: all three conference days, or only the two days after "
        "the opening day. Nothing was approved."
    ),
    "NOT_CONFIGURED": _(
        "The conference days and the opening-day capacity are not configured for this event "
        "yet. Ask an attendance policy manager to configure them. Nothing was approved."
    ),
    "OPENING_DAY_FULL": _(
        "The opening day is full: no opening-day place is left. Nothing was changed. You can "
        "explicitly choose the two days after the opening day, or ask an attendance policy "
        "manager whether the capacity can be raised."
    ),
    "NOT_APPROVED": _(
        "Only an approved, current registration has attendance days. Reload the page."
    ),
    "REASON_REQUIRED": _("Enter a short reason (at least 3 characters, no personal data)."),
    "STALE": _("The attendance information changed since you loaded this page. Reload and retry."),
    "DAYS_INCOMPLETE": _("Enter all three conference days, or none."),
    "DAYS_ORDER": _("The three conference days must be in order: opening day first."),
    "DAYS_LOCKED": _(
        "The conference days can no longer change: participants have already been given "
        "attendance days, or enforcement is active."
    ),
    "CAPACITY_INVALID": _("Enter a valid capacity (a whole number, zero or more)."),
    "CAPACITY_REQUIRED": _(
        "An opening-day capacity is required while opening-day places are allocated or "
        "enforcement is active."
    ),
    "CAPACITY_BELOW_ALLOCATED": _(
        "The capacity cannot be lower than the number of opening-day places already "
        "allocated. Nothing was changed."
    ),
    "NOT_ACTIVE": _("Attendance enforcement is not active."),
    "ACTIVATION_REFUSED": _(
        "Attendance enforcement was not activated: some prerequisites are not met. See the "
        "checklist."
    ),
}

_FALLBACK = _("This attendance operation could not be completed. Reload the page and try again.")

_READINESS_LABELS = {
    attendance.READY_DAYS: _("The three conference days are configured"),
    attendance.READY_CAPACITY: _("The opening-day capacity is configured"),
    attendance.READY_CLASSIFIED: _("Every approved registration has its attendance days"),
    attendance.READY_WITHIN_CAPACITY: _("Opening-day places allocated are within the capacity"),
    attendance.READY_BADGES: _(
        "Every handed-over badge carries the marking of its current attendance days"
    ),
    attendance.READY_EDITION_DATES: _(
        "The event edition's own start and end dates contain the three days (advisory)"
    ),
    attendance.READY_OFFLINE: _(
        "No current offline package (activation revokes them so devices refresh; advisory)"
    ),
}


def attendance_error_message(exc: BaseException) -> str:
    return str(_MESSAGES.get(getattr(exc, "code", ""), _FALLBACK))


def readiness_label(code: str) -> str:
    return str(_READINESS_LABELS.get(code, code))
