"""Stable communication-purpose codes (Phase 2 Prompt 5 §4.1).

`REGISTRATION_SUBMISSION_CONFIRMATION` intentionally reuses the Phase 1
`REGISTRATION_CONFIRMATION` template code -- that pathway
(`apps.registrations.confirmation.send_registration_confirmation`) is
already implemented, tested, and approved; this prompt does not reopen it.
`INVITATION` is likewise already satisfied by the Phase 2 Prompt 2
delegated-claim template (`apps.invitations.services`'s
`DELEGATION_CLAIM_TEMPLATE_CODE`) -- a versioned, localized, per-recipient
invitation-to-claim email. Both are listed here only for discoverability;
neither purpose is queued through `apps.communications.services.messaging`.
The five purposes actually queued through the new generic pipeline are
`INFORMATION_REQUEST`, `INFORMATION_RESPONSE_CONFIRMATION`,
`DECISION_STATUS`, and `ACCOUNT_ACCESS`.
"""

from __future__ import annotations


class CommunicationPurpose:
    INVITATION = "INVITATION"  # satisfied by the existing delegation-claim template
    REGISTRATION_SUBMISSION_CONFIRMATION = "REGISTRATION_CONFIRMATION"  # existing, unchanged
    INFORMATION_REQUEST = "INFORMATION_REQUEST"
    INFORMATION_RESPONSE_CONFIRMATION = "INFORMATION_RESPONSE_CONFIRMATION"
    DECISION_STATUS = "DECISION_STATUS"
    ACCOUNT_ACCESS = "ACCOUNT_ACCESS"
    # Owner decision IDV-Q2 (2026-10-02): a final identity rejection, with how
    # to register again from the same account. Seeded by `communications.0007`.
    IDENTITY_REJECTION = "IDENTITY_REJECTION"
