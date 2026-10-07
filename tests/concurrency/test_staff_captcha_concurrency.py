"""Staff sign-in CAPTCHA: simultaneous verification of one challenge consumes it
exactly once (real PostgreSQL transactions, `apps.accounts.captcha_guard`).

Each caller runs in its own thread with its own database connection and a
request-local copy of the same stored session, as simultaneous requests of one
browser would. A per-thread execute wrapper holds every caller just after its
read of the challenge table until all callers have read it (bounded wait,
never a barrier while holding a lock): with a read-then-delete implementation
all callers would see the row and succeed; with atomic consumption the later
callers are blocked by the row lock inside their read, the first caller's
bounded wait expires, it consumes and commits, and the others then find no
row. Answers come from the isolated test database and are never printed.
"""

from __future__ import annotations

import threading

import pytest
from captcha.models import CaptchaStore
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from apps.accounts import captcha_guard
from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accounts.tests.sign_in import captcha_answer

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

RENDEZVOUS_SECONDS = 2.0
EMAIL = "captcha-race@example.test"
GOOD = "captcha-race-correct-horse"
WRONG = "not-the-right-one"


def contend(calls):
    """Run the callables simultaneously, one thread and one connection each."""
    after_read = threading.Barrier(len(calls))
    start = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def hold_after_challenge_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.lstrip().upper().startswith("SELECT") and "captcha_captchastore" in sql:
            try:
                after_read.wait(timeout=RENDEZVOUS_SECONDS)
            except threading.BrokenBarrierError:
                pass  # the others are blocked by the row lock (or already done)
        return result

    def run(index, call):
        try:
            start.wait(timeout=10)
            with connection.execute_wrapper(hold_after_challenge_read):
                results[index] = call()
        except Exception as exc:  # surfaced by the assertions
            results[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a caller is stuck"
    assert not any(isinstance(result, Exception) for result in results), results
    return results


def stored_session_with_challenge():
    session = SessionStore()
    challenge = captcha_guard.issue(session)
    session.save()
    return session.session_key, challenge.key


def verify_with_session_copy(session_key, key, answer):
    def call():
        return captcha_guard.verify(SessionStore(session_key=session_key), key, answer)

    return call


def challenge_rows(key) -> int:
    """Read through a separate connection: only committed state counts."""
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM captcha_captchastore WHERE hashkey = %s", [key])
            return cursor.fetchone()[0]
    finally:
        other.close()


@pytest.mark.parametrize("callers", [2, 4])
def test_simultaneous_correct_answers_consume_the_challenge_once(callers):
    session_key, key = stored_session_with_challenge()
    answer = CaptchaStore.objects.get(hashkey=key).response
    results = contend([verify_with_session_copy(session_key, key, answer) for _ in range(callers)])
    assert results.count(None) == 1, results  # exactly one success
    assert sorted(r for r in results if r is not None) == [captcha_guard.EXPIRED] * (callers - 1)
    assert challenge_rows(key) == 0


def test_competing_wrong_and_correct_answers_consume_at_most_once():
    session_key, key = stored_session_with_challenge()
    answer = CaptchaStore.objects.get(hashkey=key).response
    results = contend(
        [
            verify_with_session_copy(session_key, key, "wrong"),
            verify_with_session_copy(session_key, key, answer),
        ]
    )
    consumers = [r for r in results if r in (None, captcha_guard.INCORRECT)]
    assert len(consumers) == 1 and results.count(captcha_guard.EXPIRED) == 1, results
    if results[0] == captcha_guard.INCORRECT:
        assert results[1] == captcha_guard.EXPIRED  # the wrong answer won: no success at all
    assert challenge_rows(key) == 0


@pytest.mark.parametrize("answer", ["", "wrong"])
def test_missing_or_wrong_answer_still_commits_the_consumption(answer):
    session_key, key = stored_session_with_challenge()
    result = verify_with_session_copy(session_key, key, answer)()
    assert result in (captcha_guard.REQUIRED, captcha_guard.INCORRECT)
    assert challenge_rows(key) == 0


def _staff():
    return OperationalUser.objects.create_user(
        email=EMAIL, password=GOOD, status=OperationalUserStatus.ACTIVE
    )


def test_a_failed_sign_in_never_rolls_back_the_consumption():
    """A correct image with a wrong password leaves the challenge consumed
    (committed before the password check), so the same answer cannot be
    replayed with another password guess."""
    _staff()
    client = Client()
    client.get(reverse("accounts:operational-sign-in"))
    answer = captcha_answer(client)
    response = client.post(
        reverse("accounts:operational-sign-in"),
        {"email": EMAIL, "password": WRONG, **answer},
    )
    assert response.status_code == 200 and SESSION_KEY not in client.session
    assert challenge_rows(answer["captcha_key"]) == 0
    replay = client.post(
        reverse("accounts:operational-sign-in"), {"email": EMAIL, "password": GOOD, **answer}
    )
    assert replay.status_code == 200 and SESSION_KEY not in client.session


def test_simultaneous_sign_in_posts_with_one_challenge_sign_in_once():
    _staff()
    first = Client()
    first.get(reverse("accounts:operational-sign-in"))
    data = {"email": EMAIL, "password": GOOD, **captcha_answer(first)}
    second = Client()
    second.cookies = first.cookies  # the same browser session, twice
    responses = contend(
        [
            lambda: first.post(reverse("accounts:operational-sign-in"), data),
            lambda: second.post(reverse("accounts:operational-sign-in"), data),
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 302]
    refused = next(r for r in responses if r.status_code == 200).content.decode()
    assert str(captcha_guard.MESSAGES[captcha_guard.EXPIRED]) in refused
