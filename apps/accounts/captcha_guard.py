"""Session-bound, single-use image CAPTCHA for the staff (operational) sign-in.

Built on django-simple-captcha (self-hosted: challenges in this application's
database, images rendered locally with Pillow; no external service, account,
key or network call). This module adds what the package's form field does
not:

- a challenge is bound to the browser session AND to one purpose (only
  `STAFF_SIGN_IN` exists), so another session's or another purpose's
  challenge is never accepted in its place;
- each challenge allows exactly one attempt: verification consumes it whatever
  the outcome (no replay, no guessing several answers against one image), and
  the page then shows a fresh one. Consumption is atomic (row lock plus a
  checked DELETE in one transaction that commits before any answer is
  compared), so simultaneous submissions of one challenge cannot both pass and
  a later failed sign-in can never roll the consumption back;
- it expires after `CAPTCHA_TIMEOUT` minutes, checked again at verification;
- the answer is generated with `secrets`, exists only server-side (database
  row, never in markup, URLs, the session or logs) and is compared in
  constant time.

It is basic bot deterrence, not strong authentication: the password, the
per-email and per-network sign-in limits, CSRF and session expiry all remain.
The participant email OTP and its ALTCHA proof of work are not affected.

Transaction boundary: the consumption commits when its own `atomic()` block
exits, which requires that the sign-in request does not run inside an outer
transaction. `ATOMIC_REQUESTS` is off in every settings module and the system
check `accounts.E001` refuses it (`apps.accounts.checks`).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from captcha.models import CaptchaStore
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _

SESSION_KEY = "staff_captcha"
STAFF_SIGN_IN = "staff-sign-in"
MAX_BOUND = 4  # challenges kept per session (one per purpose; one purpose today)
# Unambiguous characters only (no 0/O, 1/I/L, 2/Z, 5/S, 8/B): readable, still
# 23^length combinations.
ALPHABET = "ACDEFHJKMNPRTUVWXY34679"

REQUIRED = "CAPTCHA_REQUIRED"
INVALID = "CAPTCHA_INVALID"
EXPIRED = "CAPTCHA_EXPIRED"
INCORRECT = "CAPTCHA_INCORRECT"
MESSAGES = {
    REQUIRED: _("Type the characters shown in the security image."),
    INVALID: _(
        "This security image is not valid for this page (it was already used, or it belongs "
        "to another page or tab). Type the characters of the new image."
    ),
    EXPIRED: _("The security image expired. Type the characters of the new image."),
    INCORRECT: _("The characters did not match the image. Type the characters of the new image."),
}


def challenge():
    """`CAPTCHA_CHALLENGE_FUNCT`: (text drawn in the image, expected answer),
    from a cryptographic generator."""
    text = "".join(secrets.choice(ALPHABET) for _ in range(int(settings.STAFF_CAPTCHA_LENGTH)))
    return text, text.lower()


@dataclass(frozen=True)
class Challenge:
    key: str
    image_url: str
    timeout_minutes: int
    length: int


def _bound(session) -> dict:
    value = session.get(SESSION_KEY, {})
    return dict(value) if isinstance(value, dict) else {}


def issue(session, purpose: str = STAFF_SIGN_IN) -> Challenge:
    """Create a fresh challenge for one purpose of this session, replacing
    that purpose's previous one (a page never has two valid challenges)."""
    CaptchaStore.remove_expired()
    bound = _bound(session)
    previous = bound.pop(purpose, None)
    if previous:
        CaptchaStore.objects.filter(hashkey=previous).delete()
    while len(bound) >= MAX_BOUND:  # forget the oldest purpose's challenge
        CaptchaStore.objects.filter(hashkey=bound.pop(next(iter(bound)))).delete()
    key = CaptchaStore.generate_key()
    bound[purpose] = key
    session[SESSION_KEY] = bound
    return Challenge(
        key=key,
        image_url=reverse("accounts:staff-captcha-image", kwargs={"key": key}),
        timeout_minutes=int(settings.CAPTCHA_TIMEOUT),
        length=int(settings.STAFF_CAPTCHA_LENGTH),
    )


def _consume(hashkey: str):
    """Atomically take one challenge out of the store and return
    `(response, expiration)`, or None when it does not exist or another
    request consumed it first.

    The row is read under a row lock (SELECT ... FOR UPDATE) and deleted in
    the same transaction, which commits before any answer is compared: a
    concurrent verification of the same challenge waits for the lock, then
    finds no row. Only a request whose own DELETE removed the row may use the
    values it read."""
    with transaction.atomic():
        store = CaptchaStore.objects.select_for_update().filter(hashkey=hashkey).first()
        if store is None:
            return None
        deleted, _ = CaptchaStore.objects.filter(pk=store.pk).delete()
        if deleted != 1:
            return None
        return store.response, store.expiration


def verify(session, key, answer, purpose: str = STAFF_SIGN_IN) -> str | None:
    """Return None when this session's challenge for `purpose` is solved,
    otherwise an error code (`MESSAGES`).

    The challenge is consumed by every call (also for a missing, malformed or
    wrong answer), so it can be tried once only, including under simultaneous
    submissions."""
    bound = _bound(session)
    expected = bound.pop(purpose, None)
    session[SESSION_KEY] = bound
    consumed = _consume(expected) if isinstance(expected, str) and expected else None
    key = key.strip() if isinstance(key, str) else ""
    answer = answer.strip().lower() if isinstance(answer, str) else ""
    if not key or not answer:
        return REQUIRED
    if not isinstance(expected, str) or not constant_time_compare(key, expected):
        return INVALID
    if consumed is None:  # never issued, purged after expiry, or consumed by another request
        return EXPIRED
    response, expiration = consumed
    if expiration <= timezone.now():
        return EXPIRED
    if not constant_time_compare(answer, response):
        return INCORRECT
    return None


def purge_expired(*, batch: int = 500) -> int:
    """Delete one bounded batch of expired challenges; returns the count.
    Run by the `accounts.purge_expired_staff_captchas` periodic task."""
    ids = list(
        CaptchaStore.objects.filter(expiration__lte=timezone.now())
        .order_by("expiration")
        .values_list("pk", flat=True)[:batch]
    )
    if not ids:
        return 0
    deleted, _ = CaptchaStore.objects.filter(pk__in=ids).delete()
    return deleted


# ---------------------------------------------------------------------------
# Issuance limit (anonymous requests create challenge rows and sessions)
# ---------------------------------------------------------------------------

_ISSUANCE_PREFIX = "staff-captcha:"


def issuance_allowed(network_identity: str, *, now: float | None = None) -> bool:
    """Count one image issuance (sign-in page or refresh) for this client
    network, through the SAME shared limiter as the participant human-check
    challenges (`apps.core.issuance_counter`: Redis in staging and production,
    a stricter bounded per-process fallback when it fails), in its own
    bucket. Called before the session is written, so a refused burst creates
    no challenge row and no session."""
    import time

    from apps.core.human_check import issuance_digest
    from apps.core.issuance_counter import get_issuance_limiter

    now = time.time() if now is None else now
    bucket = _ISSUANCE_PREFIX + issuance_digest(network_identity)
    return get_issuance_limiter().allow(bucket, now=now).allowed
