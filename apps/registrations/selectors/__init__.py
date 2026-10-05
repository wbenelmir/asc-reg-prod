"""apps.registrations.selectors -- empty in Phase 1 Prompt 2.

No project model and no project migration exists until Prompt 3 (accepted plan section
6.2).

Phase 3 Prompt 8 adds the ONE definition of an "active approved Registration
Context" that credential, physical-badge and entry code share, so a
withdrawal or an operational cancellation means the same thing everywhere
(P8-02, P8-06). Participant withdrawal and operational cancellation both set
`public_status` to WITHDRAWN and stamp `withdrawn_at` / `cancelled_at`; the
predicate checks every one of those facts rather than relying on any single
column.

Owner decision IDV-Q1 (2026-10-02) adds one more fact to the same shared
definition: the registration's identity is cleared -- a current, valid,
verified identity (`apps.people.selectors.clearance`). An approval recorded
before the identity was rejected, replaced, invalidated or found to be only
simulated therefore stops being eligible everywhere at once, without the
participation decision being rewritten. A registration without an identity
case (submitted before identity verification existed) is not eligible.
"""

from __future__ import annotations

from django.db.models import Q


def active_approved_context_q() -> Q:
    """Queryset filter: approved, current, not withdrawn, not cancelled, linked
    to a person, with a cleared identity (IDV-Q1)."""
    from apps.people.selectors.clearance import identity_cleared_q
    from apps.registrations.models import RegistrationPublicStatus

    return (
        Q(
            public_status=RegistrationPublicStatus.APPROVED,
            is_current_context=True,
            withdrawn_at__isnull=True,
            cancelled_at__isnull=True,
            person__isnull=False,
        )
        & identity_cleared_q()
    )


def is_active_approved_context(registration) -> bool:
    """In-memory twin of `active_approved_context_q()` for an already-loaded row.

    Callers that must be race-free read `registration` under a row lock
    first; this function evaluates the registration facts it is given, and
    reads the identity clearance (IDV-Q1) from the database rather than from
    any cached relation. A test pins that the two definitions agree on every
    combination of facts.
    """
    from apps.people.selectors.clearance import identity_clearance
    from apps.registrations.models import RegistrationPublicStatus

    return (
        registration is not None
        and registration.public_status == RegistrationPublicStatus.APPROVED
        and bool(registration.is_current_context)
        and registration.withdrawn_at is None
        and registration.cancelled_at is None
        and registration.person_id is not None
        and identity_clearance(registration.pk).cleared
    )


def accommodation_requests_visible_to(user):
    """Accommodation requests a support coordinator may read (UX-3, D-10).

    Requires `registrations.coordinate_accommodation_support` through a scoped
    membership whose own group grants it (the same isolation rule as every
    scoped selector). Only YES answers that were not withdrawn are listed;
    general registration permissions never reveal them.

    Consent binding (UX-C1, UX-F01; UX-C2, G): a draft is never listed, and
    a request is listed only when THIS registration's own INITIAL submission
    records the explicit sensitive-support consent
    (`sensitive_consent_submissions`). This applies to every YES answer, with
    or without details. Another registration's consent, or the person-level
    consent history, never qualifies a row. The owner still sees their own
    request through `accommodation_request_for`.

    A request of a withdrawn or operationally cancelled registration is not
    listed (UXR-C1, developer decision). Nothing is deleted or rewritten, so
    retention and legal-hold handling are unchanged.
    """
    from django.db.models import Exists, OuterRef

    from apps.accounts.selectors import scope_filtered_queryset
    from apps.registrations.models import (
        AccommodationAnswer,
        AccommodationRequest,
        Registration,
        RegistrationPublicStatus,
    )

    registrations = scope_filtered_queryset(
        user,
        Registration.objects,
        app_label="registrations",
        codename="coordinate_accommodation_support",
        organization_field="source_organization_id",
    )
    consented = sensitive_consent_submissions().filter(registration_id=OuterRef("registration_id"))
    return (
        AccommodationRequest.objects.filter(
            registration__in=registrations.values("pk"),
            answer=AccommodationAnswer.YES,
            withdrawn_at__isnull=True,
        )
        .exclude(registration__public_status=RegistrationPublicStatus.DRAFT)
        # UXR-C1 (developer decision): support no longer sees a request once
        # its registration is withdrawn or operationally cancelled. Only the
        # visibility changes; the rows, retention and legal holds are untouched.
        .exclude(registration__public_status=RegistrationPublicStatus.WITHDRAWN)
        .exclude(registration__withdrawn_at__isnull=False)
        .exclude(registration__cancelled_at__isnull=False)
        .filter(Exists(consented))
        .select_related("registration", "registration__event_edition")
        .order_by("-updated_at")
    )


def sensitive_consent_submissions():
    """INITIAL submissions whose snapshot records the registration-scoped
    sensitive-support consent (UX-C2, G).

    * Snapshots from UX-C2 on carry `sensitive_support_consent_recorded`; only
      the value `true` qualifies. `sensitive_data_provided` is never read as
      consent for them.
    * Compatibility rule: a snapshot WITHOUT that key (written by UX-3 to
      UX-C1) qualifies only when `sensitive_data_provided` is `true`. Every
      service version that could write such a snapshot refused the submission
      unless the explicit sensitive consent was given and recorded in the same
      transaction (`SensitiveConsentRequired`, introduced with the
      AccommodationRequest model in UX-3). A detail-free YES from those
      versions recorded no consent and never qualifies.
    """
    from apps.registrations.models import RegistrationSubmission, RegistrationSubmissionKind
    from apps.registrations.services import SENSITIVE_SUPPORT_CONSENT_KEY

    key = f"snapshot_json__accommodation__{SENSITIVE_SUPPORT_CONSENT_KEY}"
    current_rule = Q(**{key: True})
    compatibility_rule = ~Q(
        **{"snapshot_json__accommodation__has_key": SENSITIVE_SUPPORT_CONSENT_KEY}
    ) & Q(snapshot_json__accommodation__sensitive_data_provided=True)
    return RegistrationSubmission.objects.filter(
        submission_kind=RegistrationSubmissionKind.INITIAL
    ).filter(current_rule | compatibility_rule)


def has_sensitive_support_consent(registration) -> bool:
    """True when this registration's own INITIAL submission records the
    sensitive-support consent (see `sensitive_consent_submissions`)."""
    return sensitive_consent_submissions().filter(registration=registration).exists()
