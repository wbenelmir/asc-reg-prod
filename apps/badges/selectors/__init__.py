"""Read-only projections for Digital Entry Pass credentials.

Every operational selector routes through
`apps.accounts.selectors.scope_filtered_queryset`, the single shared scope
primitive. There is deliberately no second filtering path: a credential a
user may not see is absent from the queryset, so a detail view returns 404
rather than leaking existence.
"""

from __future__ import annotations

from apps.accounts.selectors import scope_filtered_queryset
from apps.badges.models import (
    NON_TERMINAL_STATUSES,
    DigitalEntryPass,
    DigitalEntryPassStatus,
    PassCredentialSeries,
    VerificationKey,
    VerificationKeyStatus,
)


def passes_visible_to(user, *, codename: str = "view_digitalentrypass"):
    """Credentials this operational user may see, in their granted scope."""
    return scope_filtered_queryset(
        user,
        DigitalEntryPass.objects,
        app_label="badges",
        codename=codename,
        organization_field="registration__source_organization_id",
    ).select_related("series", "registration", "event_edition", "badge_assignment")


def current_pass_for_registration(registration) -> DigitalEntryPass | None:
    """The registration's current credential, or None if it has none.

    "Current" is defined by `NON_TERMINAL_STATUSES` -- the same tuple the
    database's partial unique constraint is built from, so the service
    layer and the constraint can never disagree about what current means.
    """
    return (
        DigitalEntryPass.objects.select_related("series", "badge_assignment", "event_edition")
        .filter(registration=registration, status__in=NON_TERMINAL_STATUSES)
        .first()
    )


def credential_history_for_registration(registration):
    """Every credential version for a registration, newest first.

    Operational surface only. The participant-facing view never calls this:
    a participant sees the current credential and nothing else.
    """
    return DigitalEntryPass.objects.filter(registration=registration).order_by(
        "-credential_version"
    )


def participant_current_pass(person):
    """The current, non-terminal credentials belonging to this participant.

    Scoped by person ownership, never by a supplied identifier, so one
    participant can never address another participant's credential.
    """
    return (
        DigitalEntryPass.objects.select_related(
            "series", "registration", "registration__event_edition", "badge_assignment"
        )
        .filter(registration__person=person, status__in=NON_TERMINAL_STATUSES)
        .order_by("registration__created_at")
    )


def participant_displayable_passes(person):
    """One credential per series to show the participant, terminal states included.

    Showing only non-terminal credentials made a revoked or expired pass look
    exactly like a pass that had never been issued -- the participant was told
    "you do not have an entry pass yet" when in fact theirs had been revoked,
    which is both wrong and alarming at a gate.

    Selection per series:

    * the current non-terminal credential when one exists;
    * otherwise the most recent terminal credential, so REVOKED and EXPIRED
      surface with safe localized guidance.

    REPLACED is never surfaced on its own: a replaced credential always has a
    newer current one in the same series, and that newer one is what the
    participant needs.
    """
    candidates = (
        DigitalEntryPass.objects.select_related(
            "series", "registration", "registration__event_edition"
        )
        .filter(registration__person=person)
        .order_by("series_id", "-credential_version")
    )

    chosen: dict[object, DigitalEntryPass] = {}
    for credential in candidates:
        series_id = credential.series_id
        existing = chosen.get(series_id)
        if existing is None:
            chosen[series_id] = credential
            continue
        # A non-terminal credential always wins over a terminal one.
        if (
            existing.status not in NON_TERMINAL_STATUSES
            and credential.status in NON_TERMINAL_STATUSES
        ):
            chosen[series_id] = credential

    displayable = [
        credential
        for credential in chosen.values()
        if credential.status != DigitalEntryPassStatus.REPLACED
    ]
    displayable.sort(key=lambda credential: credential.registration.created_at)
    return displayable


def series_for_registration(registration) -> PassCredentialSeries | None:
    return PassCredentialSeries.objects.filter(registration=registration).first()


def usable_verification_keys():
    """Keys that may currently verify a credential (ACTIVE or RETIRED)."""
    return VerificationKey.objects.filter(
        status__in=(VerificationKeyStatus.ACTIVE, VerificationKeyStatus.RETIRED)
    ).order_by("key_id")


def active_verification_key() -> VerificationKey | None:
    return VerificationKey.objects.filter(status=VerificationKeyStatus.ACTIVE).first()


def pass_is_qr_exposable(credential) -> bool:
    """True only when a usable QR may be rendered for this credential."""
    return credential is not None and credential.status == DigitalEntryPassStatus.ACTIVE


# ---------------------------------------------------------------------------
# Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
# ---------------------------------------------------------------------------

from apps.badges.selectors.stock import (  # noqa: E402, F401 -- re-exported for `apps.badges.selectors.*`
    active_allocations_for,
    allocations_available_for_issuance,
    current_issuance_for_assignment,
    event_balances,
    issuance_history_for_assignment,
    issuances_visible_to,
    ledger_entries_for,
    location_balances,
    print_batches_visible_to,
    reconciliations_for,
    stock_locations_visible_to,
    transfers_for,
)
