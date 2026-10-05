"""Phase 3 Prompt 8 (P8-01): pass, key and stock failures are localized and safe.

Before this correction the views rendered `str(exc)` -- the service's own
developer-facing English -- so a French or Arabic operator read English,
and internal wording (a signing provider, a raw eligibility code) reached
the page. The views now map the exception's class and stable code to a
translated message (`apps.badges.presentation`). Each test drives a real
failure through the real view in French and Arabic, and asserts that the
raw service text is absent and the translated message present.
Synthetic data only.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.utils import translation
from django.utils.html import escape

from apps.badges import presentation
from apps.badges.services import (
    InsufficientStockError,
    PassConfigurationError,
    PassNotEligibleError,
    RegistrationNotEligibleError,
    generate_pass,
    new_operation_id,
)
from apps.badges.tests.conftest import make_registration, sign_in_operational
from apps.core.service_errors import GENERIC_SERVICE_ERROR, resolve_service_error_message

pytestmark = pytest.mark.django_db

LANGUAGES = ["fr", "ar"]


def _in(language, message) -> str:
    with translation.override(language):
        return str(message)


def _assert_localized(response, language, *, raw: str, expected) -> None:
    body = response.content.decode("utf-8")
    localized = _in(language, expected)
    # The expected text really is translated -- not an English fallback.
    assert localized != _in("en", expected)
    assert escape(localized) in body or localized in body
    assert raw not in body
    assert escape(raw) not in body


def _post(client, language, url, data):
    from django.urls import reverse

    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    return client.post(reverse(url[0], kwargs=url[1]), data, follow=True)


# ---------------------------------------------------------------------------
# Physical stock issuance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_insufficient_stock_at_issuance_is_localized(
    language, client, badge_type, stock_issuer, stock_location, current_badge_assignment
):
    sign_in_operational(client, stock_issuer.email_normalized)
    response = _post(
        client,
        language,
        ("badges:badge-issue", {"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "location_id": str(stock_location.pk),
        },
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="There is no stock of this Badge Type available at this location.",
        expected=presentation._MESSAGES[(InsufficientStockError, None)],
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_issuance_for_a_withdrawn_context_is_localized(
    language, client, badge_type, stock_issuer, stock_location, current_badge_assignment
):
    from apps.reviews.services import withdraw_registration

    registration = current_badge_assignment.registration
    withdraw_registration(
        registration=registration,
        person=registration.person,
        expected_version=registration.version,
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    response = _post(
        client,
        language,
        ("badges:badge-issue", {"pk": registration.pk}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "location_id": str(stock_location.pk),
        },
    )
    # Withdrawal leaves the assignment CURRENT, so the request reaches the
    # service, which refuses it (P8-02) -- in the viewer's language.
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="no physical badge may be issued for it",
        expected=presentation._MESSAGES[(RegistrationNotEligibleError, None)],
    )


# ---------------------------------------------------------------------------
# Digital Entry Pass lifecycle and configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_ineligible_generation_shows_a_translated_reason_label(
    language, client, event, organization, person, pass_admin, active_key
):
    registration = make_registration(event=event, organization=organization, person=person)
    sign_in_operational(client, pass_admin.email_normalized)
    response = _post(
        client,
        language,
        ("badges:credential-generate", {"pk": registration.pk}),
        {"operation_id": new_operation_id()},
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="Registration is not eligible for a Digital Entry Pass",
        expected=presentation.ELIGIBILITY_REASON_LABELS["NO_CURRENT_ROLE"],
    )
    assert "NO_CURRENT_ROLE" not in response.content.decode("utf-8")


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_signing_configuration_failure_is_localized(
    language, client, eligible_registration, pass_admin, signing_provider
):
    """No ACTIVE verification key is published: a configuration failure."""
    sign_in_operational(client, pass_admin.email_normalized)
    response = _post(
        client,
        language,
        ("badges:credential-generate", {"pk": eligible_registration.pk}),
        {"operation_id": new_operation_id()},
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="No ACTIVE verification key is published.",
        expected=presentation._MESSAGES[(PassConfigurationError, None)],
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_activating_a_withdrawn_contexts_pass_is_localized(
    language, client, eligible_registration, pass_admin, active_key
):
    from apps.reviews.services import withdraw_registration

    credential = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=new_operation_id()
    ).credential
    eligible_registration.refresh_from_db()
    withdraw_registration(
        registration=eligible_registration,
        person=eligible_registration.person,
        expected_version=eligible_registration.version,
    )
    sign_in_operational(client, pass_admin.email_normalized)
    response = _post(
        client,
        language,
        ("badges:credential-activate", {"pk": credential.pk}),
        {"operation_id": new_operation_id(), "expected_lock_version": credential.version},
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="Registration is not eligible for a Digital Entry Pass",
        expected=presentation.ELIGIBILITY_REASON_LABELS["NOT_APPROVED"],
    )
    assert "NOT_APPROVED" not in response.content.decode("utf-8")


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_refused_verification_key_is_localized(language, client, key_custodian):
    sign_in_operational(client, key_custodian.email_normalized)
    response = _post(
        client,
        language,
        ("badges:verification-keys", {}),
        {"key_id": "v7", "public_key_pem": "not a public key"},
    )
    assert response.status_code == 200
    from apps.badges.services import VerificationKeyError

    _assert_localized(
        response,
        language,
        raw="A verification key must be a PEM-encoded EC public key on the NIST",
        expected=presentation._MESSAGES[(VerificationKeyError, "INVALID_PUBLIC_KEY")],
    )


# ---------------------------------------------------------------------------
# The mapping itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_mapped_message_is_translated(language):
    messages = [*presentation._MESSAGES.values(), *presentation.ELIGIBILITY_REASON_LABELS.values()]
    messages.append(GENERIC_SERVICE_ERROR)
    for message in messages:
        assert _in(language, message) != _in("en", message), _in("en", message)


def test_the_exception_text_is_never_consulted():
    class Unmapped(Exception):
        pass

    leaking = PassNotEligibleError("SYNTHETIC-INTERNAL-DETAIL")
    assert "SYNTHETIC-INTERNAL-DETAIL" not in presentation.service_error_message(leaking)
    unknown = Unmapped("SYNTHETIC-INTERNAL-DETAIL")
    assert presentation.service_error_message(unknown) == str(GENERIC_SERVICE_ERROR)
    assert resolve_service_error_message(unknown, {}) == str(GENERIC_SERVICE_ERROR)


def test_an_unknown_eligibility_code_falls_back_to_the_generic_label():
    message = presentation.service_error_message(PassNotEligibleError("SOMETHING_NEW"))
    assert message == str(presentation._NOT_ELIGIBLE_FALLBACK)
    assert presentation.eligibility_reason_label("SOMETHING_NEW") == message
