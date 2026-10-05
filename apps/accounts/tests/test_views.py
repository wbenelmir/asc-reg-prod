"""Logout safety tests (Prompt 4 final closure pass §10): POST-only, CSRF-protected."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts import participant_auth
from apps.accounts.models import OperationalUser, OperationalUserStatus

pytestmark = pytest.mark.django_db


def test_participant_logout_rejects_get(client: Client) -> None:
    response = client.get(reverse("accounts:participant-sign-out"))
    assert response.status_code == 405


def test_participant_logout_accepts_post_and_clears_the_session(client: Client) -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("logout-test@example.com")
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session.save()

    response = client.post(reverse("accounts:participant-sign-out"))
    assert response.status_code == 302
    assert participant_auth.PARTICIPANT_SESSION_KEY not in client.session


def test_participant_logout_enforces_csrf_for_a_client_without_a_valid_token() -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    enforcing_client = Client(enforce_csrf_checks=True)
    person = resolve_or_create_participant_for_email("csrf-logout@example.com")
    session = enforcing_client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session.save()

    response = enforcing_client.post(reverse("accounts:participant-sign-out"))
    assert response.status_code == 403


def test_operational_logout_rejects_get(client: Client) -> None:
    OperationalUser.objects.create_user(
        email="ops-logout@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-logout@example.com", "password": "__test_password__"},
    )
    response = client.get(reverse("accounts:operational-sign-out"))
    assert response.status_code == 405


def test_operational_logout_accepts_post(client: Client) -> None:
    OperationalUser.objects.create_user(
        email="ops-logout-2@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-logout-2@example.com", "password": "__test_password__"},
    )
    response = client.post(reverse("accounts:operational-sign-out"))
    assert response.status_code == 302
    assert response.url == reverse("accounts:operational-sign-in")
