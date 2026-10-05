"""TEST-ONLY MFA step-up doubles for the `apps.accounts.mfa` boundary.

They exist so tests can exercise the authorization boundary of actions that
require a step-up. They are not an authenticator: the accepted value is a
fixed synthetic marker, and `apps.entry.checks` refuses any `MFA_BACKEND`
pointing into a `.tests.` module outside the test settings.
"""

from __future__ import annotations

#: The only response the accepting double accepts. Synthetic, not a secret.
ACCEPTED_TEST_RESPONSE = "test-step-up-ok"  # noqa: S105


class AcceptingStepUpBackend:
    def verify_step_up(self, *, user, purpose: str, response: str) -> bool:
        return response == ACCEPTED_TEST_RESPONSE


class RejectingStepUpBackend:
    def verify_step_up(self, *, user, purpose: str, response: str) -> bool:
        return False


class FailingStepUpBackend:
    def verify_step_up(self, *, user, purpose: str, response: str) -> bool:
        raise RuntimeError("synthetic provider failure")
