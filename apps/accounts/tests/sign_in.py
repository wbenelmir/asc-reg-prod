"""Test-only: sign a staff member in like a browser.

Opens the sign-in page (which issues the session's security image), reads
that challenge's answer from the ISOLATED TEST DATABASE -- a real browser only
ever sees the image -- and submits email, password and answer. Every test
helper that signs staff in goes through here.
"""

from __future__ import annotations

from django.urls import reverse


def captcha_answer(client) -> dict:
    """The hidden key and the answer of the sign-in challenge bound to this
    client's session (from the test database; never from the markup)."""
    from captcha.models import CaptchaStore

    from apps.accounts import captcha_guard

    key = client.session[captcha_guard.SESSION_KEY][captcha_guard.STAFF_SIGN_IN]
    return {"captcha_key": key, "captcha_answer": CaptchaStore.objects.get(hashkey=key).response}


def staff_sign_in(client, email: str, password: str, *, next_url: str | None = None, **extra):
    """GET the sign-in page, then POST the credentials with the current
    challenge's answer. Returns the POST response."""
    url = reverse("accounts:operational-sign-in")
    if next_url:
        url = f"{url}?next={next_url}"
    client.get(url)
    # The page's form posts back to its own URL, so `next` stays in the query.
    data = {"email": email, "password": password, **captcha_answer(client), **extra}
    return client.post(url, data)
