"""Localized, safe messages for entry device and checkpoint failures.

Phase 3 Prompt 8 (P8-01). Views never render `str(exc)` from an entry
service exception: the service message is developer-facing English. They
call `service_error_message(exc)`, which maps the exception's class and
stable `code` to one of the translated messages below (see
`apps.core.service_errors`), keeping the distinctions an operator can act
on -- an expiry out of range, a gate or zone outside the device scope, an
authorization denial -- without the internal wording.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.core.service_errors import resolve_service_error_message
from apps.entry.services import (
    EntryConcurrencyError,
    EntryPermissionError,
    EntryServiceError,
    EntryStateError,
)
from apps.entry.services.devices import DeviceConfigurationError
from apps.entry.services.sessions import CheckpointSetupError

_MESSAGES = {
    (DeviceConfigurationError, "EXPIRY_REQUIRED"): _("Choose a future expiry for the device."),
    (DeviceConfigurationError, "EXPIRY_TOO_LONG"): _(
        "The device expiry exceeds the maximum enrollment period."
    ),
    (DeviceConfigurationError, "EXPIRY_AFTER_EVENT"): _(
        "A device cannot stay enrolled after its event edition has ended."
    ),
    (DeviceConfigurationError, "INVALID_SCOPE"): _(
        "The gate and the zones must be active and belong to the same venue of this event."
    ),
    (DeviceConfigurationError, "INVALID_METHODS"): _(
        "Choose at least one valid verification method."
    ),
    (DeviceConfigurationError, "NAME_REQUIRED"): _("Enter an operational name for the device."),
    (DeviceConfigurationError, "REASON_REQUIRED"): _("A reason is required."),
    (DeviceConfigurationError, None): _("The device configuration is not valid."),
    (EntryStateError, "DEVICE_EXPIRED"): _("This device has expired."),
    (EntryStateError, None): _(
        "This change is not possible in the device's current state. Reload the page."
    ),
    (CheckpointSetupError, "OUTSIDE_DEVICE_SCOPE"): _(
        "This gate or zone is outside this device's scope."
    ),
    (CheckpointSetupError, "CHECKPOINT_INACTIVE"): _("This checkpoint is not active."),
    (CheckpointSetupError, None): _("This checkpoint cannot be opened on this device."),
    (EntryPermissionError, None): _("You are not authorized to perform this action here."),
    (EntryConcurrencyError, None): _("This device changed since you loaded it. Reload and retry."),
    (EntryServiceError, None): _(
        "This entry operation could not be completed. Reload the page and try again."
    ),
}


def service_error_message(exc: BaseException) -> str:
    """The localized, safe message for an entry device or checkpoint failure."""
    return resolve_service_error_message(exc, _MESSAGES)


#: Phase 4 Prompt 2 (ADR-0023): safe, localized messages for offline
#: preparation codes -- shown in the device shell, the device API error
#: envelope, and device administration. Never an exception's own text.
_OFFLINE_MESSAGES = {
    "OFFLINE_DISABLED": _("Offline continuity is not enabled for this event."),
    "EVENT_CLOSED": _("This event is closed. Offline continuity is no longer available."),
    "DEVICE_NOT_OPERATIONAL": _("This device is not an active enrolled entry device."),
    "DEVICE_WIPE_ORDERED": _("An emergency wipe was ordered for this device."),
    "DEVICE_OFFLINE_BLOCKED": _("Offline use is blocked on this device. Prepare it again."),
    "SCOPE_NOT_OFFLINE": _("This device's checkpoint scope does not permit offline continuity."),
    "DEVICE_NOT_PREPARED": _("This device has not been prepared for offline use."),
    "DEVICE_NOT_READY": _("This device has not passed its offline self-test."),
    "DEVICE_NOT_AUTHENTICATED": _("This browser is not an enrolled, active entry device."),
    "NOT_SIGNED_IN": _("Sign in on this device first."),
    "NO_CHECKPOINT": _("Set up the checkpoint to start verifying."),
    "RATE_LIMITED": _("Too many offline data requests. Wait a few minutes and try again."),
    "PACKAGE_NOT_CURRENT": _("The offline data on this device is no longer current. Refresh it."),
    "PACKAGE_TOO_LARGE": _("The offline data for this checkpoint is too large to prepare."),
    "NO_VALIDITY": _("Offline data cannot be prepared this close to the end of the event day."),
    "STORAGE_UNAVAILABLE": _("Offline data could not be stored. Try again later."),
    "BUILD_PENDING": _("Offline data is still being prepared on the server. Try again shortly."),
    "KEY_INVALID": _("This browser produced an unusable device key. Use a supported browser."),
    "KEY_REUSED": _("This device key is already registered. Prepare the device again."),
    "PROOF_MISSING": _("The device could not prove its identity. Try again."),
    "PROOF_INVALID": _("The device could not prove its identity. Try again."),
    "NONCE_INVALID": _("The request expired. Try again."),
    "NONCE_REPLAYED": _("This request was already used. Try again."),
    "NOT_PROVISIONED": _("This device has not been prepared for offline use."),
    "REPORT_INVALID": _("The device report could not be read."),
    "MALFORMED_REQUEST": _("The request could not be read."),
    "FORBIDDEN": _("You are not authorized to perform this action here."),
    "MFA_UNAVAILABLE": _("MFA step-up is not available in this environment."),
    "MFA_STEP_UP_FAILED": _("The MFA step-up failed. Try again."),
    "CONFIRMATION_REQUIRED": _("Confirm that you accept the possible loss of local evidence."),
    "REASON_REQUIRED": _("A reason is required."),
    "NOTE_TOO_LONG": _("The note is too long."),
    "SENSITIVITY_REQUIRED": _(
        "A scope with a restricted zone must use the sensitive offline values."
    ),
    "INVALID_SENSITIVITY": _("The offline sensitivity is invalid."),
    # Phase 4 Prompt 3 (ADR-0024): synchronization and reconciliation.
    "MALFORMED_BATCH": _("The upload could not be read. Local records are kept."),
    "QUARANTINE_REFUSED": _("The upload was refused. Local records are kept."),
    "STORE_LOCKED": _(
        "Local records are locked on this device. Nothing new can be recorded here: "
        "follow the manual procedure."
    ),
    "RECORD_FAILED": _(
        "The decision could not be saved on this device. Do not admit on it: follow the "
        "manual procedure."
    ),
    "UPLOAD_FAILED": _(
        "Upload failed. Local records are kept and are uploaded again automatically."
    ),
    "NOTE_REQUIRED": _("A note is required."),
    "CASE_CLOSED": _("This case is already closed."),
    "INVALID_ACTION": _("This action is not available."),
}

_MESSAGES.update(
    {
        (DeviceConfigurationError, "SENSITIVITY_REQUIRED"): _OFFLINE_MESSAGES[
            "SENSITIVITY_REQUIRED"
        ],
        (DeviceConfigurationError, "INVALID_SENSITIVITY"): _OFFLINE_MESSAGES["INVALID_SENSITIVITY"],
    }
)


def offline_error_codes() -> list[str]:
    return sorted(_OFFLINE_MESSAGES)


def offline_error_message(code: str) -> str:
    """The localized message for an offline preparation code (generic fallback)."""
    from apps.core.service_errors import GENERIC_SERVICE_ERROR

    return str(_OFFLINE_MESSAGES.get(code, GENERIC_SERVICE_ERROR))
