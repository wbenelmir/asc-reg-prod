"""Test helpers shared across apps. Never imported by application code."""

from __future__ import annotations

from apps.core import human_check


def solved_human_check(*, session, action: str = human_check.ACTION_OTP_REQUEST) -> str:
    """A freshly issued and correctly solved ALTCHA payload bound to `session`,
    as the widget would post it. Each call is a new, single-use solution."""
    return human_check.solve(human_check.issue_challenge(action, session=session))


def otp_request_data(email: str, *, client) -> dict:
    """POST data for `accounts:otp-request` with a real, solved human check.

    Fetches the challenge through `client`, exactly as the widget does, so the
    challenge is bound to that client's session (UX-C2)."""
    from django.urls import reverse

    response = client.get(reverse("accounts:human-check-challenge"))
    return {"email": email, "human_check": human_check.solve(response.json())}
