"""Localized, safe messages for Digital Entry Pass, verification-key and stock failures.

Phase 3 Prompt 8 (P8-01). Views never render `str(exc)` from a service
exception: the service message is developer-facing English. They call
`service_error_message(exc)` instead, which maps the exception's class and
stable `code` to one of the translated messages below (see
`apps.core.service_errors`). The useful distinctions -- not eligible,
stale state, insufficient stock, wrong Badge Type, invalid configuration --
are kept; the internal detail is not.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.badges.services import (
    CrossEventScopeError,
    DuplicateStockLocationError,
    FallbackLookupThrottled,
    InsufficientStockError,
    OperationConflictError,
    PassConcurrencyError,
    PassConfigurationError,
    PassNotEligibleError,
    PassServiceError,
    PassStateError,
    RegistrationNotEligibleError,
    StockConcurrencyError,
    StockServiceError,
    StockStateError,
    VerificationKeyError,
    WrongBadgeTypeError,
)
from apps.core.service_errors import resolve_service_error_message

#: Pass eligibility reason codes (`apps.accreditation.services.EligibilityReason`,
#: plus the credential layer's own `NO_CURRENT_ASSIGNMENTS`) as translated labels.
ELIGIBILITY_REASON_LABELS = {
    "NOT_APPROVED": _(
        "This registration is not, or is no longer, approved. No entry pass can be issued "
        "or used for it."
    ),
    "NO_CURRENT_ROLE": _(
        "This registration has no current Participant Role assignment. Assign one before "
        "generating an entry pass."
    ),
    "NO_CURRENT_BADGE": _(
        "This registration has no current Badge Type assignment. Assign one before "
        "generating an entry pass."
    ),
    "NO_CURRENT_ACCESS_PROFILE": _(
        "This registration has no current Access Profile assignment. Assign one before "
        "generating an entry pass."
    ),
    "BADGE_OUT_OF_SCOPE": _("The Badge Type assignment belongs to another event edition."),
    "NO_CURRENT_ASSIGNMENTS": _(
        "A current Participant Role, Badge Type and Access Profile assignment are all "
        "required first."
    ),
}

_NOT_ELIGIBLE_FALLBACK = _("This registration is not eligible for an entry pass.")

_MESSAGES = {
    # --- Digital Entry Pass ------------------------------------------------
    **{(PassNotEligibleError, code): label for code, label in ELIGIBILITY_REASON_LABELS.items()},
    (PassNotEligibleError, None): _NOT_ELIGIBLE_FALLBACK,
    (PassStateError, "ALREADY_CURRENT"): _("This registration already has a current entry pass."),
    (PassStateError, "VALIDITY_ENDED"): _(
        "This entry pass can no longer be activated or resumed: its validity period has ended."
    ),
    (PassStateError, "NO_CURRENT_PASS"): _(
        "This registration has no current entry pass to replace."
    ),
    (PassStateError, None): _(
        "This action is not possible in the entry pass's current state. Reload the page and "
        "check its status."
    ),
    (PassConcurrencyError, None): _(
        "This entry pass changed since you loaded it. Reload and try again."
    ),
    (PassConfigurationError, None): _(
        "The entry pass could not be issued because the signing key or the event "
        "configuration is not ready. Contact the credential key custodian."
    ),
    (OperationConflictError, None): _(
        "That operation identifier was already used for a different command."
    ),
    (FallbackLookupThrottled, None): _(
        "Too many fallback reference lookups. Wait a few minutes before trying again."
    ),
    # --- Verification keys -------------------------------------------------
    (VerificationKeyError, "INVALID_KEY_ID"): _(
        "The key identifier must be the letter v followed by one to four digits, such as v2."
    ),
    (VerificationKeyError, "INVALID_PUBLIC_KEY"): _(
        "A verification key must be a PEM-encoded EC public key on the NIST P-256 curve, "
        "containing public key material only."
    ),
    (VerificationKeyError, "DUPLICATE_KEY_ID"): _(
        "A verification key with this identifier already exists."
    ),
    (VerificationKeyError, "WRONG_STATUS"): _(
        "This operation is not possible in the verification key's current status."
    ),
    (VerificationKeyError, "PROVIDER_MISMATCH"): _(
        "The signing service does not hold this key yet. Configure the signing key before "
        "promoting it."
    ),
    (VerificationKeyError, "INVALID_REASON"): _("Choose a reason from the list."),
    (VerificationKeyError, None): _("This verification key operation was refused."),
    (PassServiceError, None): _(
        "This entry pass operation could not be completed. Reload the page and try again."
    ),
    # --- Physical badge stock ----------------------------------------------
    (InsufficientStockError, None): _(
        "There is not enough available stock of this Badge Type at this location."
    ),
    (WrongBadgeTypeError, None): _(
        "The Badge Type does not match this registration's current assignment. Reload the page."
    ),
    (CrossEventScopeError, None): _("These records belong to different event editions."),
    (RegistrationNotEligibleError, None): _(
        "This registration is no longer approved, so no physical badge can be issued or "
        "replaced for it."
    ),
    (DuplicateStockLocationError, None): _(
        "A stock location with this code already exists for this event."
    ),
    (StockConcurrencyError, None): _(
        "This record changed since you loaded it. Reload and try again."
    ),
    (StockStateError, None): _(
        "This action is not possible in the record's current state. Reload the page and check it."
    ),
    (StockServiceError, None): _(
        "This stock operation could not be completed. Reload the page and try again."
    ),
}


def service_error_message(exc: BaseException) -> str:
    """The localized, safe message for a pass, key or stock service failure."""
    return resolve_service_error_message(exc, _MESSAGES)


def eligibility_reason_label(reason: str) -> str:
    """The translated label for a pass eligibility reason code."""
    return str(ELIGIBILITY_REASON_LABELS.get(reason, _NOT_ELIGIBLE_FALLBACK))
