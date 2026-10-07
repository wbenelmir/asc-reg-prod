"""Staff account administration under real multi-connection PostgreSQL races
(`apps.accounts.administration`).

1. Two unrestricted account administrators suspend each other at the same
   moment: one succeeds, the other is refused, and an administrator remains.
2. One setup link submitted twice at once sets exactly one password.

Threads on their own connections, released by a barrier. Synthetic data.
"""

from __future__ import annotations

import threading

import pytest
from django.contrib.auth.models import Group
from django.db import connection

from apps.accounts import administration as admin
from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run(calls):
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def worker(index, call):
        try:
            barrier.wait(timeout=10)
            results[index] = call()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return results


def _administrator(email):
    user = OperationalUser.objects.create_user(
        email=email, password=None, status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=user, group=Group.objects.get(name="Account Administrators"), granted_by=user
    )
    return user


def test_two_administrators_suspending_each_other_leave_one_administrator():
    first = _administrator("race-admin-1@example.test")
    second = _administrator("race-admin-2@example.test")
    results = _run(
        [
            lambda: admin.change_status(
                actor=first, target=second, command="suspend", reason="Race one"
            ),
            lambda: admin.change_status(
                actor=second, target=first, command="suspend", reason="Race two"
            ),
        ]
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1, results
    assert isinstance(errors[0], admin.AccountAdministrationError)
    assert errors[0].code in ("LAST_ADMINISTRATOR", "NOT_PERMITTED")
    assert len(admin.unrestricted_administrator_ids()) == 1
    statuses = sorted(
        OperationalUser.objects.filter(pk__in=[first.pk, second.pk]).values_list(
            "status", flat=True
        )
    )
    assert statuses == [OperationalUserStatus.ACTIVE, OperationalUserStatus.SUSPENDED]


def test_one_setup_link_submitted_twice_sets_one_password():
    actor = _administrator("race-link-admin@example.test")
    account = OperationalUser.objects.create_user(
        email="race-link@example.test", password=None, status=OperationalUserStatus.INVITED
    )
    token = admin.issue_credential_setup(actor=actor, target=account, purpose="INVITATION")
    passwords = ["first-Choice-password-11", "second-Choice-password-22"]
    results = _run(
        [
            lambda: admin.complete_credential_setup(token_id=token.pk, password=passwords[0]),
            lambda: admin.complete_credential_setup(token_id=token.pk, password=passwords[1]),
        ]
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1 and errors[0].code == "LINK_INVALID", results
    account.refresh_from_db()
    assert sum(account.check_password(p) for p in passwords) == 1
