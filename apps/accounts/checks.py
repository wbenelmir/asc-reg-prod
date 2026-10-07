"""System checks for the staff sign-in image CAPTCHA (`apps.accounts.captcha_guard`).

* accounts.E001 -- `ATOMIC_REQUESTS` on the default database would wrap the
  sign-in request in a transaction, so a failed sign-in could roll back the
  committed consumption of a challenge (it would become reusable).
* accounts.E002 -- the package's test answer must never be accepted.
* accounts.E003 -- the package's own URLconf (with its unbound refresh and
  audio views) must not be mounted.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def staff_captcha_configuration(app_configs=None, **kwargs):
    errors = []
    if settings.DATABASES.get("default", {}).get("ATOMIC_REQUESTS"):
        errors.append(
            Error(
                "ATOMIC_REQUESTS must stay off: the staff sign-in CAPTCHA consumption has to "
                "commit before the password is checked.",
                id="accounts.E001",
            )
        )
    if getattr(settings, "CAPTCHA_TEST_MODE", False):
        errors.append(Error("CAPTCHA_TEST_MODE must be False.", id="accounts.E002"))
    if _captcha_urls_mounted():
        errors.append(
            Error(
                "Do not include captcha.urls: the staff sign-in uses its own bound views.",
                id="accounts.E003",
            )
        )
    return errors


def _captcha_urls_mounted() -> bool:
    from django.urls import get_resolver

    def walk(patterns) -> bool:
        for pattern in patterns:
            module = getattr(pattern, "urlconf_name", None)
            if getattr(module, "__name__", module) == "captcha.urls":
                return True
            if hasattr(pattern, "url_patterns") and walk(pattern.url_patterns):
                return True
        return False

    return walk(get_resolver().url_patterns)
