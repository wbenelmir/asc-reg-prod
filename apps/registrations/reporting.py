"""Registration populations shared by the operations list and the statistics (UPD-05, UPD-08).

One definition per population, as a queryset filter on `Registration`, so a
count on the statistics page and the list it opens always agree (the list is
filtered by the same `segment_q`). Every definition uses `Exists` subqueries,
never joins, so a registration is counted once whatever its number of
decisions, identity attempts, review cases, imports or attendance days.

Identity status, participation decision and accreditation readiness stay
distinct populations:

* identity work (`identity_*`) reads `people.IdentityVerification.status` of
  ACTIVE registrations only (UPD-01: a withdrawn registration is history);
* the participation decision (`final_approved`, `rejected`) reads the CURRENT
  `reviews.RegistrationDecision` together with the registration's own state;
* accreditation readiness lives in `apps.registrations.statistics`.

Time basis: a registration "applied" when it was first submitted
(`Registration.submitted_at`); drafts are not applications. Every definition
here is a snapshot of the current state, never a reconstructed history.
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

#: Public statuses of a submitted registration still being processed.
PROCESSING_STATUSES = ("SUBMITTED", "UNDER_REVIEW", "ADDITIONAL_INFORMATION_REQUIRED")
#: Public statuses of an active (not closed) submitted registration.
ACTIVE_STATUSES = (*PROCESSING_STATUSES, "APPROVED")


def _current_decision(outcome: str):
    from apps.reviews.models import RegistrationDecision

    return Exists(
        RegistrationDecision.objects.filter(
            registration_id=OuterRef("pk"), is_current=True, outcome=outcome
        )
    )


def _identity(*statuses: str):
    from apps.people.models import IdentityVerification

    return Exists(
        IdentityVerification.objects.filter(registration_id=OuterRef("pk"), status__in=statuses)
    )


def _open_review_case():
    from apps.reviews.models import ReviewCase, ReviewCaseStatus

    return Exists(
        ReviewCase.objects.filter(
            registration_id=OuterRef("pk"), status__in=ReviewCaseStatus.open_statuses()
        )
    )


def _any_current_decision():
    from apps.reviews.models import RegistrationDecision

    return Exists(
        RegistrationDecision.objects.filter(registration_id=OuterRef("pk"), is_current=True)
    )


def applications_q() -> Q:
    """Every registration ever submitted, withdrawn and replaced ones included."""
    return Q(submitted_at__isnull=False)


def active_q() -> Q:
    """Submitted, not withdrawn, cancelled or not approved."""
    return applications_q() & Q(public_status__in=ACTIVE_STATUSES)


def final_approved_q() -> Q:
    """The authoritative approval: the CURRENT decision is APPROVED and the
    registration is live (approved, current context, not withdrawn, not
    cancelled). A re-registration releases the older context, so a person is
    never counted twice here."""
    return Q(
        public_status="APPROVED",
        is_current_context=True,
        withdrawn_at__isnull=True,
        cancelled_at__isnull=True,
    ) & Q(_current_decision("APPROVED"))


def processing_q() -> Q:
    return applications_q() & Q(public_status__in=PROCESSING_STATUSES)


def rejected_q() -> Q:
    """Not approved by the current decision (an identity rejection included)."""
    return Q(public_status="NOT_APPROVED") & Q(_current_decision("NOT_APPROVED"))


def withdrawn_q() -> Q:
    """Withdrawn by the participant or cancelled by the team (both included)."""
    return applications_q() & Q(public_status="WITHDRAWN")


def cancelled_by_team_q() -> Q:
    return withdrawn_q() & Q(cancelled_at__isnull=False)


def withdrawn_by_participant_q() -> Q:
    return withdrawn_q() & Q(cancelled_at__isnull=True)


def identity_pending_q() -> Q:
    """Active registrations whose identity check is queued or running."""
    return active_q() & Q(_identity("PENDING"))


def manual_review_q() -> Q:
    """Active registrations whose identity waits for a reviewer."""
    return active_q() & Q(_identity("MANUAL_REVIEW"))


def participant_correction_q() -> Q:
    """Waiting for the participant: an identity correction or an information
    request (both show Additional information required)."""
    return applications_q() & Q(public_status="ADDITIONAL_INFORMATION_REQUIRED")


def participation_review_q() -> Q:
    """Identity verified and waiting for the participation decision."""
    return (
        applications_q()
        & Q(public_status__in=("SUBMITTED", "UNDER_REVIEW"))
        & Q(_identity("API_VERIFIED", "MANUALLY_VERIFIED"))
        & ~Q(_any_current_decision())
    )


def verified_without_review_work_q() -> Q:
    """An exception to fix: identity verified, registration active and
    undecided, but no open participation-review case (it would never be
    decided). The review-queue backfill (`backfill_review_queue`) repairs it
    when the registration is eligible."""
    return participation_review_q() & ~Q(_open_review_case())


def drafts_q() -> Q:
    return Q(public_status="DRAFT")


#: Segment key -> (definition, label). The order is the display order.
SEGMENTS: dict[str, tuple] = {
    "applications": (applications_q, _("All applications (submitted)")),
    "active": (active_q, _("Active applications")),
    "final_approved": (final_approved_q, _("Final-approved")),
    "processing": (processing_q, _("In processing")),
    "rejected": (rejected_q, _("Not approved")),
    "withdrawn": (withdrawn_q, _("Withdrawn or cancelled")),
    "cancelled_by_team": (cancelled_by_team_q, _("Cancelled by the team")),
    "withdrawn_by_participant": (withdrawn_by_participant_q, _("Withdrawn by the participant")),
    "identity_pending": (identity_pending_q, _("Identity check pending")),
    "manual_review": (manual_review_q, _("Identity manual review")),
    "participant_correction": (participant_correction_q, _("Waiting for the participant")),
    "participation_review": (participation_review_q, _("Waiting for the participation decision")),
    "verified_without_review_work": (
        verified_without_review_work_q,
        _("Identity verified, no participation-review work"),
    ),
    "drafts": (drafts_q, pgettext_lazy("registration segment", "Drafts (not submitted)")),
}


def segment_q(key: str) -> Q | None:
    entry = SEGMENTS.get(key)
    return entry[0]() if entry else None


def segment_label(key: str) -> str:
    entry = SEGMENTS.get(key)
    label: Promise | str = entry[1] if entry else key
    return str(label)
