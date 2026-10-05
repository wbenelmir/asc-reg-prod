"""System checks for the human check (UX-4, M01, S-16).

A disabled human check is the documented recovery switch, never a silent
default: `manage.py check` reports it whenever it is off.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def human_check_settings(app_configs=None, **kwargs):
    messages = []
    if not getattr(settings, "HUMAN_CHECK_ENABLED", True):
        messages.append(
            Warning(
                "HUMAN_CHECK_ENABLED is false: the OTP request form accepts requests without "
                "the proof-of-work check (recovery switch). The OTP throttles still apply. "
                "Turn it back on once the incident is resolved.",
                id="core.W001",
            )
        )
    cost = int(getattr(settings, "HUMAN_CHECK_COST", 0))
    if not 100 <= cost <= 1_000_000:
        messages.append(Error("HUMAN_CHECK_COST must be between 100 and 1000000.", id="core.E001"))
    if getattr(settings, "HUMAN_CHECK_ALGORITHM", "") != "PBKDF2/SHA-256":
        messages.append(
            Error(
                "HUMAN_CHECK_ALGORITHM must be PBKDF2/SHA-256, the only vendored ALTCHA worker.",
                id="core.E003",
            )
        )
    if int(getattr(settings, "HUMAN_CHECK_TTL_SECONDS", 0)) < 60:
        messages.append(Error("HUMAN_CHECK_TTL_SECONDS must be at least 60.", id="core.E002"))
    messages.extend(_issuance_counter_messages())
    return messages


def _issuance_counter_messages():
    """P4-4-C3 (CACHE-01): a misconfigured counter must not silently weaken the
    challenge-issuance limit. Staging and production are stricter still
    (`config/settings/validation.py`)."""
    from apps.core.issuance_counter import STORES

    messages = []
    if getattr(settings, "HUMAN_CHECK_COUNTER_STORE", None) not in STORES:
        messages.append(
            Error("HUMAN_CHECK_COUNTER_STORE must be 'redis' or 'local'.", id="core.E004")
        )
    normal = int(getattr(settings, "HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW", 0))
    fallback = int(getattr(settings, "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW", 0))
    if fallback < 1 or fallback > normal:
        messages.append(
            Error(
                "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW must be at least 1 and not above "
                "HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW.",
                id="core.E005",
            )
        )
    for name in (
        "HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS",
        "HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS",
        "HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS",
        "HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS",
        "HUMAN_CHECK_COUNTER_RETRY_SECONDS",
        "HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS",
    ):
        if int(getattr(settings, name, 0)) < 1:
            messages.append(Error(f"{name} must be a positive integer.", id="core.E006"))
    return messages
