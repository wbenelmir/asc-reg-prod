"""Deployment checks for the NIN lookup provider (IDV-2, amendment A-13).

Staging and production refuse every stub and simulation at startup
(`config/settings/validation.py`). The official adapter may still be
selected with incomplete settings -- the authentication response contract is
open (API-01) -- or the disabled backend may be selected on purpose. Both are
safe (every Algerian case goes to manual review), but `check --deploy` says so,
so the gap is never unnoticed. Never echoes a configured value.
"""

from __future__ import annotations

from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def nin_provider_readiness(app_configs=None, **kwargs):
    from apps.people.nin_provider import provider_readiness

    is_official, is_ready, problems = provider_readiness()
    if is_ready:
        return []
    return [
        Warning(
            "The ministry NIN service is not ready ("
            + (
                "the official adapter is missing settings"
                if is_official
                else "not the official adapter"
            )
            + f", {len(problems)} problem(s)): every Algerian identity goes to manual review. "
            "See docs/operations/identity_provider_integration_guide.md.",
            id="people.W001",
        )
    ]
