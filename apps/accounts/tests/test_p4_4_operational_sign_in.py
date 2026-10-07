"""P4-4 finding P44-F03: operational sign-in attempt limits and audit.

Before P4-4 the staff password sign-in accepted unlimited attempts and wrote
no audit event. These tests fail on that code: nothing was audited and the
eleventh wrong password was still checked. Synthetic data only.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import SESSION_KEY
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import operational_sign_in
from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accounts.tests.sign_in import captcha_answer, staff_sign_in
from apps.audit import action_codes
from apps.audit.models import AuditEvent

SIGN_IN = reverse("accounts:operational-sign-in")
EMAIL = "p44-staff@example.test"
GOOD = "p44-correct-horse-battery"
WRONG = "p44-not-the-right-one"
FAILED = action_codes.OPERATIONAL_SIGN_IN_FAILED
SUCCEEDED = action_codes.OPERATIONAL_SIGN_IN_SUCCEEDED
THROTTLED = action_codes.OPERATIONAL_SIGN_IN_THROTTLED


@pytest.fixture
def staff(db):
    return OperationalUser.objects.create_user(
        email=EMAIL, password=GOOD, status=OperationalUserStatus.ACTIVE
    )


def _post(email, password, *, address="198.51.100.10"):
    return staff_sign_in(Client(REMOTE_ADDR=address), email, password)


def _codes():
    return list(AuditEvent.objects.order_by("id").values_list("action_code", flat=True))


def _all_audit_text() -> str:
    return " ".join(
        str(value)
        for row in AuditEvent.objects.values()
        for value in row.values()
        if value is not None
    ).lower()


@pytest.mark.django_db
def test_success_and_failure_are_audited_without_the_email_or_password(staff):
    assert _post(EMAIL, WRONG).status_code == 200
    response = _post(EMAIL, GOOD)
    assert response.status_code == 302

    assert _codes() == [FAILED, SUCCEEDED]
    failed, succeeded = AuditEvent.objects.order_by("id")
    assert failed.result == "FAILURE" and failed.actor_user_id is None
    assert succeeded.result == "SUCCESS" and succeeded.actor_user_id == staff.pk
    target = operational_sign_in.email_target_uuids(EMAIL)
    assert failed.target_uuid in target and succeeded.target_uuid == failed.target_uuid
    assert failed.network_fingerprint and "198.51.100.10" not in failed.network_fingerprint

    text = _all_audit_text()
    for secret in (EMAIL, "p44-staff", GOOD, WRONG, "198.51.100.10"):
        assert secret.lower() not in text


@pytest.mark.django_db
def test_the_email_budget_refuses_without_checking_the_password(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 3
    for _ in range(3):
        assert _post(EMAIL, WRONG).status_code == 200

    clients = [Client(REMOTE_ADDR="198.51.100.10") for _ in range(4)]
    with patch("apps.accounts.operational_sign_in.authenticate") as checked:
        refused = [staff_sign_in(client, EMAIL, GOOD) for client in clients]
    checked.assert_not_called()
    assert {r.status_code for r in refused} == {429}
    # No authenticated session for a refused attempt. (The page itself keeps an
    # anonymous, short-lived session for its security image.)
    assert all(SESSION_KEY not in client.session for client in clients)
    assert b"Too many sign-in attempts" in refused[0].content
    # One throttle event per email and window, however long the burst.
    assert _codes() == [FAILED] * 3 + [THROTTLED]
    throttled = AuditEvent.objects.get(action_code=THROTTLED)
    assert throttled.result == "DENIED"
    assert throttled.reason_code == operational_sign_in.REASON_EMAIL_BUDGET


@pytest.mark.django_db
def test_an_unknown_email_is_throttled_exactly_like_a_known_one(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 2
    known = [_post(EMAIL, WRONG).status_code for _ in range(4)]
    unknown = [_post("p44-nobody@example.test", WRONG).status_code for _ in range(4)]
    assert known == unknown == [200, 200, 429, 429]
    assert _post(EMAIL, WRONG).content.count(b"Too many sign-in attempts") == 1
    assert _post("p44-nobody@example.test", WRONG).content.count(b"Too many sign-in attempts") == 1


@pytest.mark.django_db
def test_the_email_budget_counts_since_the_last_success(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 3
    for _ in range(2):
        _post(EMAIL, WRONG)
    assert _post(EMAIL, GOOD).status_code == 302
    for _ in range(2):
        assert _post(EMAIL, WRONG).status_code == 200
    assert _post(EMAIL, GOOD).status_code == 302


@pytest.mark.django_db
def test_the_email_budget_ignores_other_case_and_spacing(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 2
    _post(EMAIL.upper(), WRONG)
    _post(f"  {EMAIL}", WRONG)
    assert _post(EMAIL, GOOD).status_code == 429


@pytest.mark.django_db
def test_attempts_older_than_the_window_no_longer_count(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 2
    for _ in range(2):
        _post(EMAIL, WRONG)
    assert _post(EMAIL, GOOD).status_code == 429
    later = timezone.now() + timedelta(seconds=settings.OPERATIONAL_SIGN_IN_WINDOW_SECONDS + 1)
    with patch("apps.accounts.operational_sign_in.timezone.now", return_value=later):
        assert _post(EMAIL, GOOD).status_code == 302


@pytest.mark.django_db
def test_the_network_budget_applies_across_emails(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK = 3
    for index in range(3):
        assert _post(f"p44-spray-{index}@example.test", WRONG).status_code == 200
    with patch("apps.accounts.operational_sign_in.authenticate") as checked:
        assert _post(EMAIL, GOOD).status_code == 429
        assert _post("p44-spray-9@example.test", WRONG).status_code == 429
    checked.assert_not_called()
    # Another network is unaffected; one throttle event for the refused network.
    assert _post(EMAIL, GOOD, address="203.0.113.20").status_code == 302
    throttled = AuditEvent.objects.filter(action_code=THROTTLED)
    assert throttled.count() == 1
    assert throttled.get().reason_code == operational_sign_in.REASON_NETWORK_BUDGET


@pytest.mark.django_db
def test_a_misconfigured_zero_limit_does_not_lock_everyone_out(staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 0
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK = 0
    assert _post(EMAIL, GOOD).status_code == 302


@pytest.mark.django_db
def test_an_inactive_account_fails_like_a_wrong_password(staff):
    staff.status = OperationalUserStatus.SUSPENDED
    staff.save(update_fields=["status"])
    response = _post(EMAIL, GOOD)
    assert response.status_code == 200
    assert b"Incorrect email or password." in response.content
    assert _codes() == [FAILED]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("fr", "Trop de tentatives de connexion."),
        ("ar", "محاولات تسجيل دخول كثيرة جدًا."),
    ],
)
def test_the_refusal_is_translated(staff, settings, language, text):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 1
    _post(EMAIL, WRONG)
    client = Client(REMOTE_ADDR="198.51.100.10")
    client.get(SIGN_IN)
    response = client.post(
        SIGN_IN,
        {"email": EMAIL, "password": WRONG, **captcha_answer(client)},
        HTTP_ACCEPT_LANGUAGE=language,
    )
    assert response.status_code == 429
    assert text in response.content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.concurrency
def test_the_email_budget_is_exact_under_concurrency(settings):
    OperationalUser.objects.create_user(
        email=EMAIL, password=GOOD, status=OperationalUserStatus.ACTIVE
    )
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 3
    parallel = 8
    barrier = threading.Barrier(parallel)

    def attempt(index):
        try:
            client = Client(REMOTE_ADDR=f"198.51.100.{20 + index}")
            client.get(SIGN_IN)  # issues this client's security image
            data = {"email": EMAIL, "password": WRONG, **captcha_answer(client)}
            barrier.wait(timeout=15)
            return client.post(SIGN_IN, data).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        statuses = [
            f.result(timeout=60) for f in [pool.submit(attempt, i) for i in range(parallel)]
        ]

    assert sorted(statuses) == [200] * 3 + [429] * 5
    assert AuditEvent.objects.filter(action_code=FAILED).count() == 3
    assert AuditEvent.objects.filter(action_code=THROTTLED).count() == 1
