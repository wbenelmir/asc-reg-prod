"""A setup link being replaced while it is being used (R05).

Issuing a new link locks the account, then revokes the account's open links;
completing a link must lock in the same order (account, then link), otherwise
the two can deadlock. Each test forces one interleaving on real PostgreSQL
connections: the first party is paused INSIDE its transaction while holding
its lock, and is released only once PostgreSQL reports the second party as
waiting for a lock (`pg_stat_activity`), so the order is deterministic rather
than a matter of sleep timing. Synthetic data; mail goes to the test outbox.
"""

from __future__ import annotations

import threading
import time

import pytest
from django.contrib.auth.models import Group
from django.db import connection, connections

from apps.accounts import administration as admin
from apps.accounts.models import (
    CredentialSetupToken,
    OperationalUser,
    OperationalUserStatus,
    ScopedGroupMembership,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

CHOSEN = "lock-order-Chosen-password-5"
WAIT = 15


def _wait_for_lock_waiter(timeout=WAIT) -> bool:
    deadline = time.monotonic() + timeout
    with connections["default"].cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.02)
    return False


def _thread(target, results, name):
    def run():
        try:
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            results[name] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


@pytest.fixture
def world():
    actor = OperationalUser.objects.create_user(
        email="lock-order-admin@example.test", password=None, status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=actor, group=Group.objects.get(name="Account Administrators"), granted_by=actor
    )
    account = OperationalUser.objects.create_user(
        email="lock-order-account@example.test", password=None, status=OperationalUserStatus.INVITED
    )
    old = admin.issue_credential_setup(actor=actor, target=account, purpose="INVITATION")
    return actor, account, old


def _assert_no_unexpected_errors(results):
    for name, value in results.items():
        if isinstance(value, BaseException):
            assert isinstance(value, admin.AccountAdministrationError), (name, repr(value))


def test_a_replacement_holding_the_account_wins_and_the_old_link_is_refused(world, monkeypatch):
    """Replacement locks the account first; the completion of the old link
    arrives meanwhile. No deadlock: the completion waits, then sees the link
    revoked and is refused; the new link stays open; no password is set."""
    actor, account, old = world
    holding, release = threading.Event(), threading.Event()
    original = admin._new_token

    def paused_new_token(user, **kwargs):
        if threading.current_thread().name == "replace":
            holding.set()
            assert release.wait(WAIT)
        return original(user, **kwargs)

    monkeypatch.setattr(admin, "_new_token", paused_new_token)
    results: dict = {}
    replace = _thread(
        lambda: admin.issue_credential_setup(actor=actor, target=account, purpose="INVITATION"),
        results,
        "replace",
    )
    assert holding.wait(WAIT)
    complete = _thread(
        lambda: admin.complete_credential_setup(token_id=old.pk, password=CHOSEN),
        results,
        "complete",
    )
    try:
        assert _wait_for_lock_waiter(), "the completion never waited for the account lock"
    finally:
        release.set()
    for thread in (replace, complete):
        thread.join(60)
        assert not thread.is_alive()
    _assert_no_unexpected_errors(results)
    assert isinstance(results["complete"], admin.AccountAdministrationError)
    assert results["complete"].code == "LINK_INVALID"
    new = results["replace"]
    account.refresh_from_db()
    assert not account.has_usable_password()
    old.refresh_from_db()
    assert old.revoked_at is not None and old.used_at is None
    assert admin.open_token_by_id(new.pk) is not None


def test_a_completion_holding_the_account_wins_and_the_replacement_follows(world, monkeypatch):
    """The completion holds the account and its link; a replacement arrives
    meanwhile. No deadlock: the password is set once, the old link is used
    (not revoked), and the replacement then issues a new open link."""
    actor, account, old = world
    holding, release = threading.Event(), threading.Event()
    original = admin.validate_password

    def paused_validation(password, user=None, *args, **kwargs):
        if threading.current_thread().name == "complete":
            holding.set()
            assert release.wait(WAIT)
        return original(password, user, *args, **kwargs)

    monkeypatch.setattr(admin, "validate_password", paused_validation)
    results: dict = {}
    complete = _thread(
        lambda: admin.complete_credential_setup(token_id=old.pk, password=CHOSEN),
        results,
        "complete",
    )
    assert holding.wait(WAIT)
    replace = _thread(
        lambda: admin.issue_credential_setup(actor=actor, target=account, purpose="RESET"),
        results,
        "replace",
    )
    try:
        assert _wait_for_lock_waiter(), "the replacement never waited for the account lock"
    finally:
        release.set()
    for thread in (complete, replace):
        thread.join(60)
        assert not thread.is_alive()
    _assert_no_unexpected_errors(results)
    account.refresh_from_db()
    assert account.check_password(CHOSEN)
    old.refresh_from_db()
    assert old.used_at is not None and old.revoked_at is None
    new = results["replace"]
    assert isinstance(new, CredentialSetupToken)
    assert admin.open_token_by_id(new.pk) is not None


def test_two_replacements_and_one_completion_leave_one_consistent_state(world):
    """Several parties at once (barrier): never an unhandled database error,
    at most one password set, at most one open link afterwards."""
    actor, account, old = world
    barrier = threading.Barrier(3)
    results: dict = {}

    def after_barrier(call):
        def run():
            barrier.wait(WAIT)
            return call()

        return run

    threads = [
        _thread(
            after_barrier(
                lambda: admin.complete_credential_setup(token_id=old.pk, password=CHOSEN)
            ),
            results,
            "complete",
        ),
        _thread(
            after_barrier(
                lambda: admin.issue_credential_setup(actor=actor, target=account, purpose="RESET")
            ),
            results,
            "replace-1",
        ),
        _thread(
            after_barrier(
                lambda: admin.issue_credential_setup(actor=actor, target=account, purpose="RESET")
            ),
            results,
            "replace-2",
        ),
    ]
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive()
    _assert_no_unexpected_errors(results)
    account.refresh_from_db()
    old.refresh_from_db()
    if isinstance(results["complete"], BaseException):
        assert not account.has_usable_password() and old.used_at is None
    else:
        assert account.check_password(CHOSEN) and old.used_at is not None
    assert (
        CredentialSetupToken.objects.filter(
            user=account, used_at__isnull=True, revoked_at__isnull=True
        ).count()
        == 1
    )
