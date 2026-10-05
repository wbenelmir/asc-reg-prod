"""P4-4: challenge-endpoint abuse limits, anonymous-session lifetime and cleanup,
and the cache policy of public, token and legal pages. Synthetic data only."""

from __future__ import annotations

import datetime

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.core import human_check
from apps.core.models import HumanChallengeUse
from apps.core.tasks import clear_expired_sessions_task, purge_human_challenge_uses_task
from apps.core.testing import otp_request_data

pytestmark = pytest.mark.django_db

CHALLENGE = "accounts:human-check-challenge"
LIMIT = 3


@pytest.fixture(autouse=True)
def _fresh_cache():
    # P4-4-C3: the counter no longer lives in this cache; the root conftest
    # also resets the process-wide issuance counter around every test.
    cache.clear()
    yield
    cache.clear()


def _session_row(client: Client) -> Session | None:
    key = client.cookies.get(settings.SESSION_COOKIE_NAME)
    return Session.objects.filter(session_key=key.value).first() if key else None


# ---------------------------------------------------------------------------
# Challenge endpoint throttle
# ---------------------------------------------------------------------------


@override_settings(HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=LIMIT)
def test_a_burst_of_cookieless_challenge_requests_is_refused_without_sessions() -> None:
    sessions_before = Session.objects.count()
    statuses = [Client().get(reverse(CHALLENGE)).status_code for _ in range(LIMIT + 4)]
    assert statuses == [200] * LIMIT + [429] * 4
    # Only the allowed requests created a session; refusals created none.
    assert Session.objects.count() == sessions_before + LIMIT


@override_settings(HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=LIMIT)
def test_the_refusal_is_generic_private_and_sets_no_cookie() -> None:
    for _ in range(LIMIT):
        Client().get(reverse(CHALLENGE))
    client = Client()
    refused = client.get(reverse(CHALLENGE))
    assert refused.status_code == 429
    assert refused.json() == {"error": "try_again_later"}
    assert refused["Retry-After"] == str(settings.HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS)
    assert refused["Cache-Control"] == "private, no-store"
    assert settings.SESSION_COOKIE_NAME not in refused.cookies


@override_settings(HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=LIMIT)
def test_a_normal_visitor_is_not_affected_and_can_request_a_code() -> None:
    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        response = client.post(
            reverse("accounts:otp-request"),
            otp_request_data("p44-normal@example.com", client=client),
        )
    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:otp-verify")


@override_settings(HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=LIMIT)
def test_the_limit_is_per_network_and_per_window() -> None:
    now = 1_900_000_000.0
    for _ in range(LIMIT):
        assert human_check.challenge_issuance_allowed("198.51.100.7", now=now)
    assert not human_check.challenge_issuance_allowed("198.51.100.7", now=now)
    # Another network is unaffected.
    assert human_check.challenge_issuance_allowed("198.51.100.8", now=now)
    # The next window starts afresh.
    later = now + settings.HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS
    assert human_check.challenge_issuance_allowed("198.51.100.7", now=later)


@override_settings(HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=LIMIT)
def test_one_ipv6_slash_64_is_one_client() -> None:
    now = 1_900_000_000.0
    for suffix in range(LIMIT):
        assert human_check.challenge_issuance_allowed(f"2001:db8:1:2::{suffix + 1}", now=now)
    assert not human_check.challenge_issuance_allowed("2001:db8:1:2:ffff::9", now=now)
    assert human_check.challenge_issuance_allowed("2001:db8:1:3::1", now=now)


def test_the_cache_key_never_contains_the_address() -> None:
    # P4-4-C3: the counter's keys are keyed digests, in its own store.
    from apps.core.issuance_counter import get_issuance_limiter

    human_check.challenge_issuance_allowed("192.0.2.44", now=1_900_000_000.0)
    keys = get_issuance_limiter().local.keys()
    assert keys == [human_check.issuance_digest("192.0.2.44")]
    assert not any("192.0.2.44" in key for key in keys)


@override_settings(
    HUMAN_CHECK_COUNTER_STORE="redis",
    REDIS_URL=None,
    HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=LIMIT,
    HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW=2,
)
def test_a_counter_failure_applies_the_stricter_fallback_and_logs_no_address(caplog) -> None:
    """P4-4-C3 (CACHE-01) replaces the P4-4 fail-open behaviour: a failing
    shared counter no longer allows every request. Here the shared store is
    selected without a URL, so every shared count fails."""
    now = 1_900_000_000.0
    with caplog.at_level("ERROR", logger="asc2026.ops.alerts"):
        results = [human_check.challenge_issuance_allowed("192.0.2.55", now=now) for _ in range(4)]
    assert results == [True, True, False, False]
    assert "192.0.2.55" not in caplog.text
    assert "HUMAN_CHECK_COUNTER_DEGRADED" in {getattr(r, "alert", "") for r in caplog.records}


# ---------------------------------------------------------------------------
# Anonymous challenge sessions: short-lived, restored at sign-in, cleaned up
# ---------------------------------------------------------------------------


def test_a_session_created_for_a_challenge_is_short_lived() -> None:
    client = Client()
    client.get(reverse(CHALLENGE))
    row = _session_row(client)
    assert row is not None
    lifetime = (row.expire_date - timezone.now()).total_seconds()
    expected = settings.HUMAN_CHECK_ANONYMOUS_SESSION_SECONDS
    assert expected - 30 <= lifetime <= expected + 5
    assert expected < settings.SESSION_COOKIE_AGE


def test_an_existing_session_keeps_its_ordinary_lifetime() -> None:
    client = Client()
    session = client.session
    session["pending_claim_reference"] = "synthetic"
    session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    client.get(reverse(CHALLENGE))
    row = _session_row(client)
    lifetime = (row.expire_date - timezone.now()).total_seconds()
    assert lifetime > settings.HUMAN_CHECK_ANONYMOUS_SESSION_SECONDS + 60


@override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator")
def test_participant_sign_in_restores_the_ordinary_lifetime() -> None:
    client = Client()
    client.post(
        reverse("accounts:otp-request"), otp_request_data("p44-life@example.com", client=client)
    )
    response = client.post(
        reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
    )
    assert response.status_code == 302
    row = _session_row(client)
    lifetime = (row.expire_date - timezone.now()).total_seconds()
    assert settings.SESSION_COOKIE_AGE - 60 <= lifetime <= settings.SESSION_COOKIE_AGE + 5


def test_the_session_sweep_deletes_only_expired_rows() -> None:
    now = timezone.now()
    Session.objects.create(
        session_key="p44expired" + "0" * 22,
        session_data="x",
        expire_date=now - datetime.timedelta(1),
    )
    Session.objects.create(
        session_key="p44live" + "0" * 25, session_data="x", expire_date=now + datetime.timedelta(1)
    )
    assert clear_expired_sessions_task() >= 1
    assert not Session.objects.filter(session_key__startswith="p44expired").exists()
    assert Session.objects.filter(session_key__startswith="p44live").exists()
    assert clear_expired_sessions_task() == 0  # idempotent


def test_the_replay_store_sweep_deletes_only_expired_rows() -> None:
    now = timezone.now()
    HumanChallengeUse.objects.create(
        signature_digest="a" * 64, action="otp_request", expires_at=now - datetime.timedelta(1)
    )
    HumanChallengeUse.objects.create(
        signature_digest="b" * 64, action="otp_request", expires_at=now + datetime.timedelta(1)
    )
    assert purge_human_challenge_uses_task() == 1
    assert list(HumanChallengeUse.objects.values_list("signature_digest", flat=True)) == ["b" * 64]


def test_every_scheduled_task_is_a_registered_task() -> None:
    from config.celery import app

    app.loader.import_default_modules()
    for entry in settings.CELERY_BEAT_SCHEDULE.values():
        assert entry["task"] in app.tasks, entry["task"]
        assert entry["schedule"] >= 60


# ---------------------------------------------------------------------------
# Cache policy: public start page, token URLs and the legal page
# ---------------------------------------------------------------------------


def _vary(response) -> set[str]:
    return {part.strip().lower() for part in response.headers.get("Vary", "").split(",")}


def test_the_indexable_start_page_is_never_shared_cacheable() -> None:
    client = Client()
    page = client.get(reverse("accounts:otp-request"))
    assert page.status_code == 200
    assert page["Cache-Control"] == "private, no-store"
    assert "X-Robots-Tag" not in page.headers  # still the one indexable page
    assert {"cookie", "accept-language"} <= _vary(page)


def test_a_bound_error_that_repeats_the_email_is_never_shared_cacheable() -> None:
    client = Client()
    response = client.post(
        reverse("accounts:otp-request"), {"email": "p44-bound@example.com", "human_check": "x"}
    )
    assert response.status_code == 400
    assert "p44-bound@example.com" in response.content.decode()
    assert response["Cache-Control"] == "private, no-store"
    assert {"cookie", "accept-language"} <= _vary(response)


@pytest.mark.parametrize("path", ["/invite/p44-synthetic-token/", "/claim/p44-synthetic-token/"])
def test_token_urls_are_private_noindex_and_do_not_leak_through_referrers(path) -> None:
    response = Client().get(path)
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Robots-Tag"] == "noindex, nofollow"
    assert response["Referrer-Policy"] == "same-origin"


def test_the_legal_page_with_its_csrf_form_is_private_and_varies_by_language() -> None:
    response = Client().get(reverse("privacy:legal-information"))
    assert response.status_code == 200
    assert "csrfmiddlewaretoken" in response.content.decode()  # the language form
    assert response["Cache-Control"] == "private, no-store"
    assert "accept-language" in _vary(response)
