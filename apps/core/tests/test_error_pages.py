"""Localized error pages (UI/UX Completion Gate F4): 400, 403, 404, 500 and
CSRF failure.

* Every page uses the public shell with the official logo, one h1, and the
  active language and direction (English, French, Arabic/RTL).
* 400 and 500 are rendered by Django without a request context; the 500
  page is proven static-safe by rendering it with database access blocked.
* The CSRF page keeps Django's protection (the request is still refused
  with 403) and never shows the technical reason.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.test import Client, override_settings
from django.urls import include, path

from apps.accounts.tests.sign_in import staff_sign_in


def _raise_server_error(request):
    raise RuntimeError("synthetic failure for the 500 page test")


def _raise_bad_request(request):
    raise SuspiciousOperation("synthetic bad request for the 400 page test")


urlpatterns = [
    path("__test__/boom/", _raise_server_error),
    path("__test__/bad/", _raise_bad_request),
    path("", include("config.urls")),
]

LANGUAGES = (("en", "ltr"), ("fr", "ltr"), ("ar", "rtl"))

EXPECTED_TITLES = {
    "404": {"en": "Page not found", "fr": "Page introuvable", "ar": "الصفحة غير موجودة"},
    "500": {"en": "Something went wrong", "fr": "Une erreur est survenue", "ar": "حدث خطأ ما"},
    "400": {
        "en": "The request could not be understood",
        "fr": "La requête n’a pas pu être comprise",
        "ar": "تعذّر فهم الطلب",
    },
    "csrf": {
        "en": "Your form could not be sent",
        "fr": "Votre formulaire n’a pas pu être envoyé",
        "ar": "تعذّر إرسال النموذج",
    },
}


def _client(language: str, **kwargs) -> Client:
    client = Client(**kwargs)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    return client


def _assert_shell(content: str, language: str, direction: str) -> None:
    assert f'<html lang="{language}" dir="{direction}">' in content
    assert content.count("<h1") == 1
    assert "img/brand/asc-logo.svg" in content
    assert 'class="asc-ui asc-public asc-error-page"' in content
    assert "asc-ui.css" in content


@pytest.mark.django_db
@pytest.mark.parametrize(("language", "direction"), LANGUAGES)
def test_404_page_is_localized_and_uses_the_public_shell(language, direction) -> None:
    response = _client(language).get("/this-page-does-not-exist/")
    assert response.status_code == 404
    content = response.content.decode()
    _assert_shell(content, language, direction)
    assert EXPECTED_TITLES["404"][language] in content
    # The language switcher is offered (a request context exists).
    assert 'id="language-switcher-select"' in content


@override_settings(ROOT_URLCONF=__name__)
@pytest.mark.parametrize(("language", "direction"), LANGUAGES)
def test_500_page_is_static_safe_and_localized(language, direction) -> None:
    """No `django_db` mark: any database access while handling the error or
    rendering the page would raise and fail this test."""
    client = _client(language, raise_request_exception=False)
    response = client.get("/__test__/boom/")
    assert response.status_code == 500
    content = response.content.decode()
    _assert_shell(content, language, direction)
    assert EXPECTED_TITLES["500"][language] in content
    # No diagnostic detail, and no control that would need a session.
    assert "synthetic failure" not in content
    assert "RuntimeError" not in content
    assert "Traceback" not in content
    assert 'id="language-switcher-select"' not in content
    assert 'name="csrfmiddlewaretoken"' not in content


@override_settings(ROOT_URLCONF=__name__)
@pytest.mark.parametrize(("language", "direction"), LANGUAGES)
def test_400_page_is_static_safe_and_localized(language, direction) -> None:
    response = _client(language).get("/__test__/bad/")
    assert response.status_code == 400
    content = response.content.decode()
    _assert_shell(content, language, direction)
    assert EXPECTED_TITLES["400"][language] in content
    assert "synthetic bad request" not in content


@pytest.mark.django_db
@pytest.mark.parametrize(("language", "direction"), LANGUAGES)
def test_csrf_failure_keeps_protection_and_hides_the_reason(language, direction) -> None:
    client = _client(language, enforce_csrf_checks=True)
    response = staff_sign_in(client, "csrf-probe@example.test", "__not-used__")
    # Django's CSRF middleware still refuses the request.
    assert response.status_code == 403
    assert "csrf_failure.html" in [t.name for t in response.templates]
    content = response.content.decode()
    _assert_shell(content, language, direction)
    assert EXPECTED_TITLES["csrf"][language] in content
    for technical in ("CSRF cookie not set", "CSRF token missing", "Referer", "REASON"):
        assert technical not in content


@pytest.mark.django_db
def test_csrf_failure_does_not_sign_anyone_in() -> None:
    from apps.accounts.models import OperationalUser, OperationalUserStatus

    OperationalUser.objects.create_user(
        email="csrf-user@example.test",
        password="__csrf-synthetic__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    client = Client(enforce_csrf_checks=True)
    response = staff_sign_in(client, "csrf-user@example.test", "__csrf-synthetic__")
    assert response.status_code == 403
    assert "_auth_user_id" not in client.session


def test_csrf_failure_view_is_configured() -> None:
    assert settings.CSRF_FAILURE_VIEW == "apps.core.views.csrf_failure"
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE
