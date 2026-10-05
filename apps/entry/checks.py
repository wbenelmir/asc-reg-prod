"""System checks for the Phase 3 Prompt 5 entry settings (ADR-0022).

A misconfigured limit must fail `manage.py check` loudly rather than
silently disable a protection (for example an anomaly threshold above the
refusal limit would never signal before refusing).
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

_POSITIVE = (
    "ENTRY_CONNECTION_POLL_SECONDS",
    "ENTRY_LOOKUP_WINDOW_SECONDS",
    "ENTRY_LOOKUP_ANOMALY_THRESHOLD",
    "ENTRY_LOOKUP_MAX_PER_WINDOW",
    "ENTRY_INVALID_SCAN_WINDOW_SECONDS",
    "ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD",
    "ENTRY_INVALID_SCAN_MAX_PER_WINDOW",
    "ENTRY_VERIFICATION_TARGET_P95_MS",
    "ENTRY_DEGRADED_WINDOW_SECONDS",
    "ENTRY_DEGRADED_MIN_SAMPLES",
    "ENTRY_DEVICE_STALE_SECONDS",
    "ENTRY_METRICS_RETENTION_DAYS",
    "ENTRY_METRICS_DEFAULT_WINDOW_SECONDS",
)


@register()
def entry_limit_settings(app_configs=None, **kwargs):
    errors = []
    for name in _POSITIVE:
        if int(getattr(settings, name)) <= 0:
            errors.append(Error(f"{name} must be a positive integer.", id="entry.E001"))
    for threshold, maximum in (
        ("ENTRY_LOOKUP_ANOMALY_THRESHOLD", "ENTRY_LOOKUP_MAX_PER_WINDOW"),
        ("ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD", "ENTRY_INVALID_SCAN_MAX_PER_WINDOW"),
    ):
        if int(getattr(settings, threshold)) > int(getattr(settings, maximum)):
            errors.append(Error(f"{threshold} must not exceed {maximum}.", id="entry.E002"))
    if not 1 <= int(settings.ENTRY_DEGRADED_ERROR_PERCENT) <= 100:
        errors.append(Error("ENTRY_DEGRADED_ERROR_PERCENT must be 1-100.", id="entry.E003"))
    if "/entry/status/" not in settings.OPERATIONAL_PASSIVE_PATHS:
        errors.append(
            Error(
                "OPERATIONAL_PASSIVE_PATHS must include /entry/status/, or the connection "
                "check would keep idle operator sessions alive.",
                id="entry.E004",
            )
        )
    return errors


@register()
def entry_offline_settings(app_configs=None, **kwargs):
    """Fail-closed checks for offline preparation (Phase 4 Prompt 2, ADR-0023)."""
    import os

    from apps.core.crypto.package_signing import (
        EnvPackageSigningKeyProvider,
        get_package_signing_key_provider,
    )
    from apps.entry.offline_contract import (
        APPROVED_HEALTH,
        APPROVED_INACTIVITY_LOCK_SECONDS,
        APPROVED_RESULT_CLEAR_SECONDS,
        APPROVED_VALIDITY_MAXIMA,
    )

    errors = []
    provider = get_package_signing_key_provider()
    if settings.ENTRY_OFFLINE_ENABLED and not getattr(provider, "is_configured", lambda: False)():
        errors.append(
            Error(
                "ENTRY_OFFLINE_ENABLED requires OFFLINE_PACKAGE_SIGNING_KEY_V<n> for the current "
                "OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION.",
                id="entry.E005",
            )
        )
    if (
        isinstance(provider, EnvPackageSigningKeyProvider)
        and settings.OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION
        not in settings.OFFLINE_PACKAGE_SIGNING_ACTIVE_KEY_VERSIONS
    ):
        errors.append(
            Error(
                "OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION must be an active version.",
                id="entry.E005",
            )
        )
    validity = getattr(settings, "ENTRY_OFFLINE_VALIDITY", {})
    for sensitivity, maxima in APPROVED_VALIDITY_MAXIMA.items():
        values = validity.get(sensitivity)
        if not isinstance(values, dict) or set(values) != set(maxima):
            errors.append(
                Error(f"ENTRY_OFFLINE_VALIDITY[{sensitivity}] is incomplete.", id="entry.E006")
            )
            continue
        for name, maximum in maxima.items():
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
                errors.append(
                    Error(
                        f"ENTRY_OFFLINE_VALIDITY[{sensitivity}][{name}] must be a positive "
                        f"integer no greater than the approved {maximum} seconds.",
                        id="entry.E006",
                    )
                )
        ordered = (
            isinstance(values["aging_after_seconds"], int)
            and isinstance(values["stale_after_seconds"], int)
            and isinstance(values["expires_after_seconds"], int)
            and values["aging_after_seconds"]
            <= values["stale_after_seconds"]
            <= values["expires_after_seconds"]
        )
        if not ordered:
            errors.append(
                Error(
                    f"ENTRY_OFFLINE_VALIDITY[{sensitivity}] bands must be ordered: "
                    "aging <= stale <= expiry.",
                    id="entry.E006",
                )
            )
    if dict(getattr(settings, "ENTRY_OFFLINE_HEALTH", {})) != APPROVED_HEALTH:
        errors.append(
            Error("ENTRY_OFFLINE_HEALTH must equal the approved values.", id="entry.E007")
        )
    if settings.ENTRY_OFFLINE_OPERATOR_INACTIVITY_LOCK_SECONDS != APPROVED_INACTIVITY_LOCK_SECONDS:
        errors.append(
            Error(
                "ENTRY_OFFLINE_OPERATOR_INACTIVITY_LOCK_SECONDS must equal the approved value.",
                id="entry.E007",
            )
        )
    if int(settings.ENTRY_RESULT_CLEAR_SECONDS) > APPROVED_RESULT_CLEAR_SECONDS:
        errors.append(
            Error(
                "ENTRY_RESULT_CLEAR_SECONDS must not exceed the approved 45 seconds.",
                id="entry.E007",
            )
        )
    for name in (
        "ENTRY_OFFLINE_HEALTH_RETRY_SECONDS",
        "ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS",
        "ENTRY_OFFLINE_DELTA_REFRESH_SECONDS",
        "ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS",
        "ENTRY_OFFLINE_DOWNLOAD_WINDOW_SECONDS",
        "ENTRY_OFFLINE_DOWNLOADS_PER_WINDOW",
        "ENTRY_OFFLINE_NONCE_SECONDS",
        "ENTRY_OFFLINE_MIN_FREE_STORAGE_BYTES",
        "ENTRY_OFFLINE_MAX_PACKAGE_ENTRIES",
        "ENTRY_OFFLINE_BUILD_LEASE_SECONDS",
        "ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS",
        "ENTRY_OFFLINE_BUILD_RETRY_AFTER_SECONDS",
        "ENTRY_OFFLINE_BUILD_POLL_LIMIT",
        "ENTRY_OFFLINE_DELTA_MAX_CHANGES",
        "ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS",
        # Phase 4 Prompt 3: synchronization tuning.
        "ENTRY_OFFLINE_SYNC_BATCH_SIZE",
        "ENTRY_OFFLINE_SYNC_BATCH_BYTES",
        "ENTRY_OFFLINE_SYNC_RETRY_BASE_SECONDS",
        "ENTRY_OFFLINE_SYNC_RETRY_MAX_SECONDS",
        "ENTRY_OFFLINE_SYNC_WINDOW_SECONDS",
        "ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW",
        "ENTRY_OFFLINE_ACK_RETENTION_SECONDS",
        "ENTRY_OFFLINE_CLOCK_TOLERANCE_SECONDS",
    ):
        if int(getattr(settings, name)) <= 0:
            errors.append(Error(f"{name} must be a positive integer.", id="entry.E001"))
    if int(settings.ENTRY_OFFLINE_SYNC_BATCH_BYTES) > 56 * 1024:
        errors.append(
            Error(
                "ENTRY_OFFLINE_SYNC_BATCH_BYTES must leave room within the 64 KiB device API "
                "request limit.",
                id="entry.E001",
            )
        )
    errors.extend(_offline_action_matrix_errors())
    if "/entry/api/v1/offline/heartbeat/" not in settings.OPERATIONAL_PASSIVE_PATHS:
        errors.append(
            Error(
                "OPERATIONAL_PASSIVE_PATHS must include /entry/api/v1/offline/heartbeat/, or "
                "the device heartbeat would keep idle operator sessions alive.",
                id="entry.E008",
            )
        )
    mfa_backend = getattr(settings, "MFA_BACKEND", None) or ""
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    if ".tests." in mfa_backend and settings_module != "config.settings.test":
        errors.append(Error("MFA_BACKEND must never point to a test double.", id="entry.E009"))
    permissions = settings.REST_FRAMEWORK.get("DEFAULT_PERMISSION_CLASSES", [])
    if list(permissions) != ["apps.entry.api.permissions.DenyAll"]:
        errors.append(
            Error(
                "REST_FRAMEWORK DEFAULT_PERMISSION_CLASSES must be exactly the DenyAll default.",
                id="entry.E010",
            )
        )
    longest_package = max(
        int(values.get("expires_after_seconds", 0))
        for values in settings.ENTRY_OFFLINE_VALIDITY.values()
        if isinstance(values, dict)
    )
    if int(settings.ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS) <= longest_package + int(
        settings.ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS
    ):
        errors.append(
            Error(
                "ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS must exceed the longest package "
                "lifetime plus the refresh interval, or a live package could lose the "
                "journal rows its next delta needs.",
                id="entry.E011",
            )
        )
    return errors


def _offline_action_matrix_errors() -> list:
    """entry.E012 (Phase 4 Prompt 3): the permitted-action matrix must state
    exactly the approved Stale and Expired behaviour (binding decision P2-B):
    Stale permits only Manual Review or Do Not Admit, with no override;
    Expired permits only the manual procedure and its referral; no offline
    admission outside Offline Active."""
    from apps.entry.offline_contract import (
        ADMISSION_ACTIONS,
        EXPIRED_PERMITTED,
        EXPIRED_POLICY,
        PERMITTED_ACTIONS,
        STALE_PERMITTED,
        STALE_POLICY,
        STATE_EXPIRED,
        STATE_OFFLINE_ACTIVE,
        STATE_STALE,
    )

    errors = []
    if STALE_POLICY != "MANUAL_REVIEW_OR_DO_NOT_ADMIT" or (
        PERMITTED_ACTIONS[STATE_STALE] != STALE_PERMITTED
    ):
        errors.append(Error("The Stale offline policy departs from P2-B.", id="entry.E012"))
    if EXPIRED_POLICY != "MANUAL_PROCEDURE_ONLY" or (
        PERMITTED_ACTIONS[STATE_EXPIRED] != EXPIRED_PERMITTED
    ):
        errors.append(Error("The Expired offline policy departs from P2-B.", id="entry.E012"))
    for state, actions in PERMITTED_ACTIONS.items():
        if state != STATE_OFFLINE_ACTIVE and actions & ADMISSION_ACTIONS:
            errors.append(Error(f"Offline admission is permitted in {state}.", id="entry.E012"))
    return errors


@register(Tags.security, deploy=True)
def emergency_wipe_step_up_provider(app_configs=None, **kwargs):
    """P4-4-C3 (owner decision MFA-01 revised, amendment A-11). Operational
    sign-in no longer needs an MFA provider, so static validation no longer
    requires `MFA_BACKEND`. The emergency device wipe still requires an MFA
    step-up and fails closed without a provider; say so at deployment time
    instead of letting the gap go unnoticed."""
    if getattr(settings, "MFA_BACKEND", None):
        return []
    return [
        Warning(
            "MFA_BACKEND is not configured: no MFA step-up provider exists, so the "
            "emergency device wipe is unavailable (it fails closed with MFA_UNAVAILABLE). "
            "Operational sign-in is unaffected (email and password, owner decision MFA-01).",
            id="entry.W001",
        )
    ]
