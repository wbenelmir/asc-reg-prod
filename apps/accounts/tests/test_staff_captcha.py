"""Staff sign-in image CAPTCHA (`apps.accounts.captcha_guard`). Synthetic data,
PostgreSQL. Answers are read from the isolated test database
(`apps.accounts.tests.sign_in.captcha_answer`); a browser only sees the image.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from captcha.models import CaptchaStore
from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import captcha_guard
from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accounts.tests.sign_in import captcha_answer, staff_sign_in
from apps.audit import action_codes
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db

SIGN_IN = reverse("accounts:operational-sign-in")
REFRESH = reverse("accounts:staff-captcha-refresh")
EMAIL = "captcha-staff@example.test"
GOOD = "captcha-correct-horse-battery"


@pytest.fixture
def staff(db):
    return OperationalUser.objects.create_user(
        email=EMAIL, password=GOOD, status=OperationalUserStatus.ACTIVE
    )


def signed_in(client) -> bool:
    return SESSION_KEY in client.session


def post(client, data):
    return client.post(SIGN_IN, {"email": EMAIL, "password": GOOD} | data)


def image_url(key):
    return reverse("accounts:staff-captcha-image", kwargs={"key": key})


def test_the_page_shows_an_image_never_the_answer(client, staff):
    page = client.get(SIGN_IN).content.decode()
    key = client.session[captcha_guard.SESSION_KEY][captcha_guard.STAFF_SIGN_IN]
    store = CaptchaStore.objects.get(hashkey=key)
    assert f'name="captcha_key" value="{key}"' in page and image_url(key) in page
    assert store.challenge not in page
    assert store.response not in page.lower().replace(key, "")
    assert 'for="id_captcha_answer"' in page and "New image" in page
    assert 'autocomplete="off"' in page
    # The session holds the key only, never the answer.
    assert client.session[captcha_guard.SESSION_KEY] == {captcha_guard.STAFF_SIGN_IN: key}
    image = client.get(image_url(key))
    assert image.status_code == 200 and image["Content-Type"] == "image/png"
    assert "no-store" in image["Cache-Control"]
    assert store.challenge.encode() not in image.content
    assert client.get(image_url("0" * 40)).status_code == 410
    assert "no-store" in client.get(SIGN_IN)["Cache-Control"]


def test_the_answer_is_generated_from_the_unambiguous_alphabet(settings):
    settings.STAFF_CAPTCHA_LENGTH = 6
    text, answer = captcha_guard.challenge()
    assert len(text) == 6 and set(text) <= set(captcha_guard.ALPHABET)
    assert answer == text.lower()


def test_the_image_is_only_served_to_its_own_session(staff):
    owner, other = Client(), Client()
    owner.get(SIGN_IN)
    key = owner.session[captcha_guard.SESSION_KEY][captcha_guard.STAFF_SIGN_IN]
    other.get(SIGN_IN)
    assert other.get(image_url(key)).status_code == 410
    assert owner.get(image_url(key)).status_code == 200


def test_a_correct_answer_signs_in_and_consumes_the_challenge(client, staff):
    response = staff_sign_in(client, EMAIL, GOOD)
    assert response.status_code == 302 and signed_in(client)
    assert not CaptchaStore.objects.exists()


@pytest.mark.parametrize(
    "tamper, code",
    [
        (lambda answer: {"captcha_key": answer["captcha_key"], "captcha_answer": ""}, "REQUIRED"),
        (lambda answer: {}, "REQUIRED"),
        (lambda answer: answer | {"captcha_answer": "zzzzz"}, "INCORRECT"),
        (lambda answer: answer | {"captcha_answer": ["x"] * 3}, "INCORRECT"),
        (lambda answer: answer | {"captcha_key": "f" * 40}, "INVALID"),
        (lambda answer: answer | {"captcha_key": "<script>"}, "INVALID"),
    ],
)
def test_empty_wrong_malformed_or_foreign_answers_refuse_and_consume(client, staff, tamper, code):
    client.get(SIGN_IN)
    answer = captcha_answer(client)
    response = post(client, tamper(answer))
    content = response.content.decode()
    assert response.status_code == 200 and not signed_in(client)
    assert str(captcha_guard.MESSAGES[getattr(captcha_guard, code)]) in content
    assert 'aria-invalid="true"' in content and 'id="error-summary"' in content
    assert f'value="{EMAIL}"' in content  # the typed email is kept
    # The image was consumed: the page carries a new one and the old answer fails.
    assert (
        client.session[captcha_guard.SESSION_KEY][captcha_guard.STAFF_SIGN_IN]
        != (answer["captcha_key"])
    )
    assert not CaptchaStore.objects.filter(hashkey=answer["captcha_key"]).exists()
    assert post(client, answer).status_code == 200 and not signed_in(client)
    assert staff_sign_in(client, EMAIL, GOOD).status_code == 302


def test_an_expired_challenge_is_refused(client, staff):
    client.get(SIGN_IN)
    answer = captcha_answer(client)
    CaptchaStore.objects.filter(hashkey=answer["captcha_key"]).update(
        expiration=timezone.now() - timedelta(seconds=1)
    )
    response = post(client, answer)
    assert str(captcha_guard.MESSAGES[captcha_guard.EXPIRED]) in response.content.decode()
    assert not signed_in(client)


def test_a_solved_challenge_cannot_be_replayed(client, staff):
    client.get(SIGN_IN)
    answer = captcha_answer(client)
    assert post(client, answer).status_code == 302
    client.post(reverse("accounts:operational-sign-out"))
    response = post(client, answer)
    assert response.status_code == 200 and not signed_in(client)


def test_a_challenge_belongs_to_its_session(staff):
    first, second = Client(), Client()
    first.get(SIGN_IN)
    answer = captcha_answer(first)
    second.get(SIGN_IN)
    response = post(second, answer)
    assert str(captcha_guard.MESSAGES[captcha_guard.INVALID]) in response.content.decode()
    assert not signed_in(second)
    assert post(Client(), answer).status_code == 200


def test_a_challenge_of_another_purpose_is_refused(rf):
    from django.contrib.sessions.backends.db import SessionStore

    session = SessionStore()
    challenge = captcha_guard.issue(session, "another-purpose")
    answer = CaptchaStore.objects.get(hashkey=challenge.key).response
    assert captcha_guard.verify(session, challenge.key, answer) == captcha_guard.INVALID


def test_captcha_failures_never_reach_the_password_or_its_limits(client, staff, settings):
    settings.OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = 3
    for _ in range(5):
        client.get(SIGN_IN)
        key = client.session[captcha_guard.SESSION_KEY][captcha_guard.STAFF_SIGN_IN]
        post(client, {"captcha_key": key, "captcha_answer": "wrong"})
    assert not AuditEvent.objects.filter(action_code__startswith="ACC_OPERATIONAL_SIGN_IN").exists()
    assert staff_sign_in(client, EMAIL, GOOD).status_code == 302
    client.post(reverse("accounts:operational-sign-out"))
    for _ in range(3):  # a solved image with a wrong password still counts
        staff_sign_in(client, EMAIL, "wrong-password")
    assert (
        AuditEvent.objects.filter(action_code=action_codes.OPERATIONAL_SIGN_IN_FAILED).count() == 3
    )
    response = staff_sign_in(client, EMAIL, GOOD)
    assert response.status_code == 429 and not signed_in(client)


def test_refresh_replaces_the_challenge_and_returns_only_key_and_url(client, staff):
    client.get(SIGN_IN)
    old = captcha_answer(client)
    refreshed = client.post(REFRESH, {"form": captcha_guard.STAFF_SIGN_IN})
    assert refreshed.status_code == 200 and "no-store" in refreshed["Cache-Control"]
    data = refreshed.json()
    assert set(data) == {"key", "image_url"} and data["key"] != old["captcha_key"]
    assert client.session[captcha_guard.SESSION_KEY][captcha_guard.STAFF_SIGN_IN] == data["key"]
    assert not CaptchaStore.objects.filter(hashkey=old["captcha_key"]).exists()
    assert CaptchaStore.objects.get(hashkey=data["key"]).response not in refreshed.content.decode()
    assert post(client, old).status_code == 200 and not signed_in(client)
    assert client.post(REFRESH, {"form": "other"}).status_code == 400
    assert client.get(REFRESH).status_code == 405


def test_refresh_requires_csrf(staff):
    browser = Client(enforce_csrf_checks=True)
    browser.get(SIGN_IN)
    assert browser.post(REFRESH, {"form": captcha_guard.STAFF_SIGN_IN}).status_code == 403


def test_the_sign_in_form_requires_csrf(staff):
    browser = Client(enforce_csrf_checks=True)
    browser.get(SIGN_IN)
    response = browser.post(SIGN_IN, {"email": EMAIL, "password": GOOD, **captcha_answer(browser)})
    assert response.status_code == 403 and not signed_in(browser)


def test_image_issuance_is_bounded_per_network(staff, settings):
    settings.HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW = 3
    browser = Client(REMOTE_ADDR="203.0.113.50")
    for _ in range(3):
        assert browser.get(SIGN_IN).status_code == 200
    rows = CaptchaStore.objects.count()
    refused = browser.get(SIGN_IN)
    assert refused.status_code == 429 and "data-captcha-unavailable" in refused.content.decode()
    assert browser.post(REFRESH, {"form": captcha_guard.STAFF_SIGN_IN}).status_code == 429
    assert CaptchaStore.objects.count() == rows
    # Another network is unaffected.
    assert Client(REMOTE_ADDR="203.0.113.51").get(SIGN_IN).status_code == 200


def test_expired_rows_are_purged_in_bounded_batches(client, staff):
    from apps.accounts.tasks import purge_expired_staff_captchas_task

    for _ in range(3):
        CaptchaStore.objects.create(
            challenge="ABCDE", response="abcde", expiration=timezone.now() - timedelta(minutes=1)
        )
    client.get(SIGN_IN)
    assert CaptchaStore.objects.count() == 1  # issuing removed the expired rows too
    CaptchaStore.objects.create(
        challenge="ABCDE", response="abcde", expiration=timezone.now() - timedelta(minutes=1)
    )
    assert purge_expired_staff_captchas_task() == 1
    assert CaptchaStore.objects.count() == 1


def test_participant_otp_and_altcha_are_unchanged(client):
    page = client.get(reverse("accounts:otp-request")).content.decode()
    assert "altcha-widget" in page
    assert "captcha_key" not in page and "data-staff-captcha" not in page
    assert captcha_guard.SESSION_KEY not in client.session


def test_the_package_urls_and_test_mode_are_not_enabled(settings):
    from django.urls import NoReverseMatch

    from apps.accounts.checks import staff_captcha_configuration

    assert settings.CAPTCHA_TEST_MODE is False
    assert staff_captcha_configuration() == []
    with pytest.raises(NoReverseMatch):
        reverse("captcha-refresh")
    settings.CAPTCHA_TEST_MODE = True
    assert [e.id for e in staff_captcha_configuration()] == ["accounts.E002"]
