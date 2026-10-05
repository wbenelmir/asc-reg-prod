"""Operational MFA step-up BOUNDARY (Phase 4 Prompt 2, ADR-0023).

This module is an integration point, not an authenticator. It implements no
TOTP, recovery-code, or authenticator protocol and must never gain one: the
approved plan leaves the real MFA provider to a deployment decision (an
approved SSO/IdP step-up, or a separately reviewed mature package), and
Phase 4 Prompt 4 owns the wider readiness work.

`settings.MFA_BACKEND` names a class implementing `MfaStepUpBackend`.
Until P4-4-C3, staging and production refused to start without it, because
sign-in MFA was mandatory. Owner decision MFA-01 was revised on 2026-10-01
(amendment A-11): operational sign-in is email and password, so the provider
is now optional everywhere. Wherever it is unset, every step-up FAILS CLOSED
with `MfaStepUpUnavailable`, so an action that requires step-up (the
destructive emergency wipe, binding decision P2-F -- the only one today) is
simply not possible there; `entry.W001` reports this at `check --deploy` and
`manage.py release_readiness` reports it as unresolved. The revision does not
authorize any bypass of this boundary, and no stub may stand in for a
provider.
"""

from __future__ import annotations

from typing import Protocol

from django.utils.module_loading import import_string

#: Whether MFA is enforced at operational SIGN-IN. It is not: a correct
#: password alone creates the operational session (P4-4 finding P44-F06).
#: TRD §16.1, PRD FR-AUTH-009 and AF-AUTH-03 required it; owner decision
#: MFA-01, revised on 2026-10-01 (amendment A-11), replaced that requirement
#: with email and password sign-in in staging and production, so the release
#: assessment no longer reports this as a blocker -- and still reports it as
#: not enforced, never as implemented. Only a reviewed provider integration
#: may set it to True, together with its own enforcement tests;
#: `apps/core/tests/test_p4_4_c1_release_readiness.py` fails if this value
#: disagrees with the sign-in behaviour.
OPERATIONAL_SIGN_IN_MFA_ENFORCED = False


class MfaStepUpUnavailable(Exception):
    """No MFA provider is configured; the step-up cannot be performed."""


class MfaStepUpBackend(Protocol):
    def verify_step_up(self, *, user, purpose: str, response: str) -> bool:
        """True only if `response` is a fresh, valid step-up for `user` and
        `purpose`. Must never log or persist `response`."""


def get_mfa_backend() -> MfaStepUpBackend:
    from django.conf import settings

    path = getattr(settings, "MFA_BACKEND", None)
    if not path:
        raise MfaStepUpUnavailable("MFA_BACKEND is not configured.")
    try:
        backend_class = import_string(path)
    except ImportError as exc:
        raise MfaStepUpUnavailable("MFA_BACKEND cannot be loaded.") from exc
    return backend_class()


def verify_step_up(*, user, purpose: str, response: object) -> bool:
    """Ask the configured provider to verify a step-up. Fails closed.

    Raises `MfaStepUpUnavailable` when no provider exists; returns False for
    a missing, malformed, or rejected response. The response value is never
    logged, audited, or stored by this function.
    """
    backend = get_mfa_backend()
    if not isinstance(response, str) or not response.strip() or len(response) > 256:
        return False
    try:
        return bool(backend.verify_step_up(user=user, purpose=purpose, response=response.strip()))
    except Exception:  # noqa: BLE001 - a provider failure is a refusal, never a pass
        return False
