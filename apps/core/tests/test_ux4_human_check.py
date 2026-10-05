"""Self-hosted ALTCHA human check (UX-4: M01, D-13, S-16; UX-C2: owner decision
UX-D01 option M).

Real challenges from the official `altcha` library, real PBKDF2 derivation
and the real replay store; nothing is mocked. The concurrent replay race is
in `tests/concurrency/test_ux4_channel_close_race.py`.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time

import pytest
from django.core import mail
from django.core.checks import run_checks
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.models import AuthenticationChallenge
from apps.core import human_check
from apps.core.models import HumanChallengeUse
from apps.core.testing import otp_request_data, solved_human_check

pytestmark = pytest.mark.django_db

ACTION = human_check.ACTION_OTP_REQUEST


@pytest.fixture
def session() -> dict:
    """The issuing browser session (any mapping works for the service)."""
    return {}


def _decode(payload: str) -> dict:
    return json.loads(base64.b64decode(payload))


def _encode(data: dict) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


def _solved(session, *, action: str = ACTION, now: float | None = None) -> str:
    return human_check.solve(human_check.issue_challenge(action, session=session, now=now))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_a_solved_challenge_is_accepted_once_and_stored_without_personal_data(session) -> None:
    payload = solved_human_check(session=session)
    assert human_check.verify_and_consume(payload, action=ACTION, session=session) == "OK"
    use = HumanChallengeUse.objects.get()
    assert re.fullmatch(r"[0-9a-f]{64}", use.signature_digest)
    assert use.action == ACTION
    assert set(f.name for f in HumanChallengeUse._meta.fields) == {
        "id",
        "signature_digest",
        "action",
        "expires_at",
        "created_at",
    }


def test_the_challenge_is_an_altcha_v2_challenge_bound_to_action_and_session(session) -> None:
    challenge = human_check.issue_challenge(ACTION, session=session)
    parameters = challenge["parameters"]
    assert parameters["algorithm"] == "PBKDF2/SHA-256"
    assert isinstance(parameters["expiresAt"], int)
    assert parameters["data"]["a"] == ACTION and parameters["data"]["v"] == 2
    assert re.fullmatch(r"[0-9a-f]{64}", parameters["data"]["b"])
    assert session[human_check.SESSION_NONCE_KEY] not in json.dumps(challenge)  # only an HMAC
    assert re.fullmatch(r"[0-9a-f]{64}", challenge["signature"])


def test_a_replayed_solution_is_refused(session) -> None:
    payload = solved_human_check(session=session)
    human_check.verify_and_consume(payload, action=ACTION, session=session)
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(payload, action=ACTION, session=session)
    assert caught.value.reason == "REPLAYED"
    assert HumanChallengeUse.objects.count() == 1


def test_a_solution_from_another_session_is_refused_and_not_consumed(session) -> None:
    payload = solved_human_check(session=session)
    other_session = {}
    human_check.issue_challenge(ACTION, session=other_session)  # the other session has a nonce
    for foreign in ({}, other_session):
        with pytest.raises(human_check.HumanCheckFailed) as caught:
            human_check.verify_and_consume(payload, action=ACTION, session=foreign)
        assert caught.value.reason == "WRONG_SESSION"
    assert not HumanChallengeUse.objects.exists()
    # Still valid, once, in the session that received it.
    assert human_check.verify_and_consume(payload, action=ACTION, session=session) == "OK"


def test_a_solution_for_another_action_is_refused(session) -> None:
    payload = _solved(session, action="final_submit")
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(payload, action=ACTION, session=session)
    assert caught.value.reason == "WRONG_ACTION"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cost", 1),  # an easier derivation
        ("keyPrefix", ""),  # any key would match
        ("expiresAt", 9_999_999_999),  # a far-future expiry
        ("data", {"v": 2, "a": ACTION, "b": "0" * 64}),  # a chosen binding
        ("nonce", "00" * 16),
        ("salt", "00" * 16),
    ],
)
def test_any_tampering_breaks_the_signature(session, field, value) -> None:
    data = _decode(_solved(session))
    data["challenge"]["parameters"][field] = value
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(_encode(data), action=ACTION, session=session)
    assert caught.value.reason == "BAD_SIGNATURE"
    assert not HumanChallengeUse.objects.exists()


def test_a_forged_signature_is_refused(session) -> None:
    data = _decode(_solved(session))
    data["challenge"]["signature"] = "f" * 64
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(_encode(data), action=ACTION, session=session)
    assert caught.value.reason == "BAD_SIGNATURE"


def test_an_expired_challenge_is_refused(session) -> None:
    payload = _solved(session, now=time.time() - 10_000)
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(payload, action=ACTION, session=session)
    assert caught.value.reason == "EXPIRED"


def test_the_expiry_boundary_is_inclusive(session) -> None:
    issued_at = time.time()
    payload = _solved(session, now=issued_at)
    expires = _decode(payload)["challenge"]["parameters"]["expiresAt"]
    assert (
        human_check.verify_and_consume(payload, action=ACTION, session=session, now=expires) == "OK"
    )
    other = _solved(session, now=issued_at)
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(other, action=ACTION, session=session, now=expires + 1)
    assert caught.value.reason == "EXPIRED"


def test_a_wrong_solution_is_refused(session) -> None:
    data = _decode(_solved(session))
    data["solution"]["counter"] += 1
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(_encode(data), action=ACTION, session=session)
    assert caught.value.reason == "WRONG_SOLUTION"
    data = _decode(_solved(session))
    data["solution"]["derivedKey"] = "00" * 32
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(_encode(data), action=ACTION, session=session)
    assert caught.value.reason == "WRONG_SOLUTION"


def _malformed(session) -> list:
    good = _decode(_solved(session))

    def variant(path, value):
        data = json.loads(json.dumps(good))
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return _encode(data)

    return [
        "not base64 at all!",
        _encode([1, 2, 3]),
        _encode({"challenge": {}}),
        variant(("solution", "counter"), True),
        variant(("solution", "counter"), "7"),
        variant(("solution", "counter"), -1),
        variant(("solution", "counter"), 2**32),
        variant(("solution", "derivedKey"), 7),
        variant(("challenge", "parameters", "expiresAt"), "soon"),
        variant(("challenge", "parameters", "data"), "x"),
        variant(("challenge", "parameters", "algorithm"), "ARGON2ID"),
        "A" * (human_check.MAX_PAYLOAD_LENGTH + 1),
    ]


def test_malformed_payloads_are_refused(session) -> None:
    for payload in _malformed(session):
        with pytest.raises(human_check.HumanCheckFailed) as caught:
            human_check.verify_and_consume(payload, action=ACTION, session=session)
        assert caught.value.reason == "MALFORMED", payload[:40]
    assert not HumanChallengeUse.objects.exists()


def test_a_missing_payload_is_refused(session) -> None:
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume("", action=ACTION, session=session)
    assert caught.value.reason == "MISSING"


def test_every_challenge_is_different_and_one_nonce_serves_the_session(session) -> None:
    first = human_check.issue_challenge(ACTION, session=session)
    nonce = session[human_check.SESSION_NONCE_KEY]
    second = human_check.issue_challenge(ACTION, session=session)
    assert first["signature"] != second["signature"]
    assert first["parameters"]["salt"] != second["parameters"]["salt"]
    assert session[human_check.SESSION_NONCE_KEY] == nonce
    assert first["parameters"]["data"]["b"] == second["parameters"]["data"]["b"]
    other = {}
    third = human_check.issue_challenge(ACTION, session=other)
    assert third["parameters"]["data"]["b"] != first["parameters"]["data"]["b"]


def test_the_default_cost_and_algorithm_are_the_documented_ones(settings) -> None:
    from config.settings import base

    assert base.HUMAN_CHECK_ALGORITHM == "PBKDF2/SHA-256"
    assert base.HUMAN_CHECK_COST == 5_000
    assert base.HUMAN_CHECK_TTL_SECONDS == 900
    assert base.HUMAN_CHECK_ENABLED is True
    assert settings.HUMAN_CHECK_ENABLED is True  # still enforced in tests


def test_expired_rows_are_purged_and_live_rows_kept() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(tz=UTC)
    HumanChallengeUse.objects.create(
        signature_digest="a" * 64, action=ACTION, expires_at=now - timedelta(minutes=1)
    )
    HumanChallengeUse.objects.create(
        signature_digest="b" * 64, action=ACTION, expires_at=now + timedelta(minutes=10)
    )
    assert human_check.purge_expired_uses() == 1
    assert list(HumanChallengeUse.objects.values_list("signature_digest", flat=True)) == ["b" * 64]
    assert human_check.purge_expired_uses() == 0


def test_payloads_nonces_and_signatures_never_reach_the_logs(session, caplog) -> None:
    payload = solved_human_check(session=session)
    challenge = _decode(payload)["challenge"]
    caplog.set_level(logging.DEBUG)
    human_check.verify_and_consume(payload, action=ACTION, session=session)
    with pytest.raises(human_check.HumanCheckFailed):
        human_check.verify_and_consume(payload, action=ACTION, session=session)
    for record in caplog.records:
        message = record.getMessage()
        assert payload not in message and challenge["signature"] not in message
        assert challenge["parameters"]["salt"] not in message
        assert session[human_check.SESSION_NONCE_KEY] not in message


# ---------------------------------------------------------------------------
# Recovery switch and system checks
# ---------------------------------------------------------------------------


@override_settings(HUMAN_CHECK_ENABLED=False)
def test_the_recovery_switch_is_explicit_logged_and_reported(caplog) -> None:
    caplog.set_level(logging.WARNING)
    assert human_check.verify_and_consume("", action=ACTION, session={}) == "DISABLED"
    assert any("recovery switch" in record.getMessage() for record in caplog.records)
    assert "core.W001" in {message.id for message in run_checks()}


def test_no_warning_while_enabled() -> None:
    assert "core.W001" not in {message.id for message in run_checks()}


@override_settings(HUMAN_CHECK_COST=10)
def test_a_trivial_cost_is_a_configuration_error() -> None:
    assert "core.E001" in {message.id for message in run_checks()}


@override_settings(HUMAN_CHECK_ALGORITHM="SHA-256")
def test_an_algorithm_without_a_vendored_worker_is_a_configuration_error() -> None:
    assert "core.E003" in {message.id for message in run_checks()}


# ---------------------------------------------------------------------------
# The OTP request form and the challenge endpoint
# ---------------------------------------------------------------------------


def test_the_start_page_carries_the_local_widget_and_no_remote_origin() -> None:
    page = Client().get(reverse("accounts:otp-request")).content.decode()
    assert "<altcha-widget" in page
    assert f'challenge="{reverse("accounts:human-check-challenge")}"' in page
    assert 'name="human_check"' in page
    assert "/static/vendor/altcha/altcha.min.js" in page
    assert "/static/vendor/altcha/pbkdf2.js" in page
    assert "/static/js/asc-altcha.js" in page
    assert '"humanInteractionSignature": false' in page
    assert "<noscript>" in page and "assisted registration" in page
    assert "mailto:info-africanstartupconference@startup.dz" in page
    for remote in ("cdn.", "unpkg", "jsdelivr", "altcha.org", "sentinel"):
        assert remote not in page.lower()
    # The form's own hidden field is not rendered: only the widget posts it.
    assert page.count('name="human_check"') == 1


def test_the_challenge_endpoint_is_private_and_session_bound() -> None:
    client = Client()
    response = client.get(reverse("accounts:human-check-challenge"))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    challenge = response.json()
    binding = challenge["parameters"]["data"]["b"]
    again = client.get(reverse("accounts:human-check-challenge")).json()
    assert again["parameters"]["data"]["b"] == binding
    assert again["signature"] != challenge["signature"]
    other = Client().get(reverse("accounts:human-check-challenge")).json()
    assert other["parameters"]["data"]["b"] != binding
    assert client.post(reverse("accounts:human-check-challenge")).status_code == 405


def test_a_post_without_the_check_issues_no_code() -> None:
    mail.outbox.clear()
    response = Client().post(reverse("accounts:otp-request"), {"email": "ux4-nocheck@example.com"})
    assert response.status_code == 400
    assert b"The automatic security check did not complete" in response.content
    assert not AuthenticationChallenge.objects.exists()
    assert mail.outbox == []


def test_a_post_with_a_solved_check_issues_the_code() -> None:
    mail.outbox.clear()
    client = Client()
    response = client.post(
        reverse("accounts:otp-request"), otp_request_data("ux4-solved@example.com", client=client)
    )
    assert response.status_code == 302
    assert AuthenticationChallenge.objects.count() == 1


def test_a_solution_posted_from_another_browser_session_issues_no_code() -> None:
    issuing = Client()
    data = otp_request_data("ux4-cross@example.com", client=issuing)
    response = Client().post(reverse("accounts:otp-request"), data)
    assert response.status_code == 400
    assert not AuthenticationChallenge.objects.exists()
    assert not HumanChallengeUse.objects.exists()


def test_a_replayed_form_post_issues_no_second_code() -> None:
    client = Client()
    data = otp_request_data("ux4-replay@example.com", client=client)
    assert client.post(reverse("accounts:otp-request"), data).status_code == 302
    response = client.post(reverse("accounts:otp-request"), data)  # same session, same solution
    assert response.status_code == 400
    assert AuthenticationChallenge.objects.count() == 1
    assert HumanChallengeUse.objects.count() == 1


def test_a_refused_check_keeps_the_typed_email() -> None:
    response = Client().post(reverse("accounts:otp-request"), {"email": "ux4-kept@example.com"})
    assert response.status_code == 400
    assert b'value="ux4-kept@example.com"' in response.content


def test_an_invalid_email_does_not_consume_the_solution() -> None:
    client = Client()
    data = otp_request_data("not-an-email", client=client)
    response = client.post(reverse("accounts:otp-request"), data)
    assert response.status_code == 400
    assert not HumanChallengeUse.objects.exists()


def test_the_refusal_does_not_depend_on_whether_the_account_exists() -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    resolve_or_create_participant_for_email("ux4-known@example.com")
    known = Client().post(reverse("accounts:otp-request"), {"email": "ux4-known@example.com"})
    unknown = Client().post(reverse("accounts:otp-request"), {"email": "ux4-unknown@example.com"})
    assert known.status_code == unknown.status_code == 400

    def body(response):
        return re.sub(
            r'(name="csrfmiddlewaretoken" value|value)="[^"]*"',
            "",
            response.content.decode(),
        )

    assert body(known) == body(unknown)


@override_settings(HUMAN_CHECK_ENABLED=False)
def test_with_the_recovery_switch_the_form_works_and_renders_no_challenge() -> None:
    client = Client()
    page = client.get(reverse("accounts:otp-request")).content.decode()
    assert "<altcha-widget" not in page and "vendor/altcha" not in page
    assert client.get(reverse("accounts:human-check-challenge")).status_code == 404
    response = client.post(reverse("accounts:otp-request"), {"email": "ux4-switch@example.com"})
    assert response.status_code == 302


def test_the_otp_throttle_still_applies_after_the_check(settings) -> None:
    settings.OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW = 1
    settings.OTP_RESEND_COOLDOWN_SECONDS = 0
    for _attempt in range(3):
        client = Client()
        client.post(
            reverse("accounts:otp-request"),
            otp_request_data("ux4-throttle@example.com", client=client),
        )
    assert AuthenticationChallenge.objects.count() == 1
