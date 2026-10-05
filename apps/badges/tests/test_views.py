"""View-level authorization, participant ownership, and localization."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.badges.models import DigitalEntryPass, DigitalEntryPassStatus, PassReasonCode
from apps.badges.services import (
    activate_pass,
    generate_pass,
    new_operation_id,
)
from apps.badges.tests.conftest import (
    make_operational_user_with_membership,
    make_registration,
    sign_in_operational,
)
from apps.badges.tests.test_signing_and_verification import _fake_private_pem

pytestmark = pytest.mark.django_db


def _credential(registration, actor):
    return generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential


def _activated(registration, actor):
    credential = _credential(registration, actor)
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return credential


def _sign_in_participant(client, person):
    """Establish a valid participant session, matching the real key set."""
    from django.utils import timezone

    from apps.accounts import participant_auth, session_expiry

    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()


# ---------------------------------------------------------------------------
# Operational authorization
# ---------------------------------------------------------------------------


def test_the_credential_page_requires_authentication(client, eligible_registration):
    response = client.get(
        reverse("badges:registration-credential", kwargs={"pk": eligible_registration.pk})
    )
    assert response.status_code in (302, 403)


def test_an_authorized_administrator_sees_the_credential_page(
    client, eligible_registration, pass_admin, active_key
):
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.get(
        reverse("badges:registration-credential", kwargs={"pk": eligible_registration.pk})
    )
    assert response.status_code == 200


def test_a_viewer_never_sees_a_lifecycle_control(
    client, eligible_registration, pass_admin, active_key, event, organization
):
    """Hiding a control is presentation; the endpoint still refuses below."""
    _credential(eligible_registration, pass_admin)
    viewer = make_operational_user_with_membership(
        email="pass.viewer@example.test",
        group_name="Pass Viewers",
        event_edition=event,
        organization=organization,
    )
    sign_in_operational(client, viewer.email_normalized)
    response = client.get(
        reverse("badges:registration-credential", kwargs={"pk": eligible_registration.pk})
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert "credential/generate/" not in body
    assert "/activate/" not in body
    assert "/revoke/" not in body


def test_a_viewer_cannot_post_a_lifecycle_command(
    client, eligible_registration, pass_admin, active_key, event, organization
):
    credential = _credential(eligible_registration, pass_admin)
    viewer = make_operational_user_with_membership(
        email="pass.viewer2@example.test",
        group_name="Pass Viewers",
        event_edition=event,
        organization=organization,
    )
    sign_in_operational(client, viewer.email_normalized)
    response = client.post(
        reverse("badges:credential-activate", kwargs={"pk": credential.pk}),
        {"operation_id": new_operation_id(), "expected_lock_version": credential.version},
    )
    assert response.status_code in (302, 403, 404)
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE


def test_wrong_event_scope_is_denied(
    client, eligible_registration, pass_admin, active_key, other_event, organization
):
    credential = _credential(eligible_registration, pass_admin)
    stranger = make_operational_user_with_membership(
        email="other.event@example.test",
        group_name="Pass Administrators",
        event_edition=other_event,
        organization=organization,
    )
    sign_in_operational(client, stranger.email_normalized)
    response = client.post(
        reverse("badges:credential-activate", kwargs={"pk": credential.pk}),
        {"operation_id": new_operation_id(), "expected_lock_version": credential.version},
    )
    assert response.status_code in (302, 403, 404)
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE


def test_wrong_organization_scope_is_denied(
    client, eligible_registration, pass_admin, active_key, event, other_organization
):
    credential = _credential(eligible_registration, pass_admin)
    stranger = make_operational_user_with_membership(
        email="other.org@example.test",
        group_name="Pass Administrators",
        event_edition=event,
        organization=other_organization,
    )
    sign_in_operational(client, stranger.email_normalized)
    response = client.post(
        reverse("badges:credential-activate", kwargs={"pk": credential.pk}),
        {"operation_id": new_operation_id(), "expected_lock_version": credential.version},
    )
    assert response.status_code in (302, 403, 404)
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE


def test_a_key_custodian_holds_no_credential_permission(
    client, eligible_registration, pass_admin, active_key, key_custodian
):
    """Key custody is deliberately isolated from participant credentials."""
    credential = _credential(eligible_registration, pass_admin)
    sign_in_operational(client, key_custodian.email_normalized)
    response = client.get(
        reverse("badges:registration-credential", kwargs={"pk": eligible_registration.pk})
    )
    assert response.status_code in (302, 403, 404)

    response = client.post(
        reverse("badges:credential-revoke", kwargs={"pk": credential.pk}),
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": credential.version,
            "reason_code": PassReasonCode.SECURITY_CONCERN,
        },
    )
    assert response.status_code in (302, 403, 404)
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE


def test_a_pass_administrator_cannot_manage_verification_keys(client, pass_admin, active_key):
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.get(reverse("badges:verification-keys"))
    assert response.status_code in (302, 403)


# ---------------------------------------------------------------------------
# POST-only and CSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        "badges:credential-activate",
        "badges:credential-suspend",
        "badges:credential-resume",
        "badges:credential-revoke",
        "badges:credential-replace",
    ],
)
def test_lifecycle_routes_reject_get(client, eligible_registration, pass_admin, active_key, route):
    credential = _credential(eligible_registration, pass_admin)
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.get(reverse(route, kwargs={"pk": credential.pk}))
    assert response.status_code == 405


def test_a_post_without_a_csrf_token_is_refused(eligible_registration, pass_admin, active_key):
    """CSRF is enforced for real, not merely configured."""
    from django.test import Client

    credential = _credential(eligible_registration, pass_admin)
    enforcing_client = Client(enforce_csrf_checks=True)
    sign_in_operational(enforcing_client, pass_admin.email_normalized)
    response = enforcing_client.post(
        reverse("badges:credential-activate", kwargs={"pk": credential.pk}),
        {"operation_id": new_operation_id(), "expected_lock_version": credential.version},
    )
    assert response.status_code == 403
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE


# ---------------------------------------------------------------------------
# Successful lifecycle through the UI
# ---------------------------------------------------------------------------


def test_the_generate_control_creates_a_credential(
    client, eligible_registration, pass_admin, active_key
):
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.post(
        reverse("badges:credential-generate", kwargs={"pk": eligible_registration.pk}),
        {"operation_id": new_operation_id()},
    )
    assert response.status_code == 302
    credential = DigitalEntryPass.objects.get(registration=eligible_registration)
    assert credential.status == DigitalEntryPassStatus.INACTIVE


def test_a_stale_lock_version_returns_a_visible_conflict(
    client, eligible_registration, pass_admin, active_key
):
    credential = _credential(eligible_registration, pass_admin)
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.post(
        reverse("badges:credential-activate", kwargs={"pk": credential.pk}),
        {"operation_id": new_operation_id(), "expected_lock_version": credential.version + 9},
    )
    assert response.status_code == 409
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE


def test_an_ineligible_registration_gets_a_localized_explanation(
    client, event, organization, person, pass_admin, active_key
):
    registration = make_registration(event=event, organization=organization, person=person)
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.post(
        reverse("badges:credential-generate", kwargs={"pk": registration.pk}),
        {"operation_id": new_operation_id()},
        follow=True,
    )
    assert response.status_code == 200
    # Phase 3 Prompt 8 (P8-01): the eligibility reason is a translated label,
    # never the raw reason code.
    assert b"has no current Participant Role assignment" in response.content
    assert b"NO_CURRENT_ROLE" not in response.content
    assert not DigitalEntryPass.objects.filter(registration=registration).exists()


# ---------------------------------------------------------------------------
# Participant surface
# ---------------------------------------------------------------------------


def test_a_participant_sees_only_their_own_pass(
    client, eligible_registration, pass_admin, active_key, event, organization, person
):
    from apps.people.models import Person, PersonStatus

    _activated(eligible_registration, pass_admin)
    stranger = Person.objects.create(status=PersonStatus.ACTIVE)
    _sign_in_participant(client, stranger)

    response = client.get(reverse("badges:participant-passes"))
    assert response.status_code == 200
    assert b"do not have an entry pass" in response.content


def test_a_participant_sees_their_own_active_pass_and_qr(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)

    response = client.get(reverse("badges:participant-passes"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "data-pass-qr=" in body
    assert credential.series.fallback_reference[:4] in body


@pytest.mark.parametrize(
    "status",
    [
        DigitalEntryPassStatus.INACTIVE,
        DigitalEntryPassStatus.SUSPENDED,
    ],
)
def test_a_non_active_pass_never_exposes_a_qr_to_the_participant(
    client, eligible_registration, pass_admin, active_key, person, status
):
    credential = _credential(eligible_registration, pass_admin)
    credential.status = status
    credential.save(update_fields=["status"])
    _sign_in_participant(client, person)

    response = client.get(reverse("badges:participant-passes"))
    assert response.status_code == 200
    assert b"data-pass-qr=" not in response.content


def test_the_participant_surface_never_leaks_internal_detail(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    # Internal record identifiers, the per-version credential identifier,
    # the reviewing actor, and the credential-history view are all absent.
    # (`signing_key_id` is deliberately not substring-tested: it is a
    # two-character value like "v1" that occurs harmlessly inside unrelated
    # inline script identifiers, so a substring check would assert nothing.)
    assert str(credential.pk) not in body
    assert str(credential.series_id) not in body
    assert str(credential.registration_id) not in body
    assert credential.jti not in body
    assert credential.nonce not in body
    assert credential.payload_hash not in body
    assert pass_admin.email_normalized not in body
    assert "Credential version" not in body
    assert "Credential history" not in body


def test_a_participant_cannot_print_another_participants_pass(
    client, eligible_registration, pass_admin, active_key
):
    from apps.people.models import Person, PersonStatus

    credential = _activated(eligible_registration, pass_admin)
    stranger = Person.objects.create(status=PersonStatus.ACTIVE)
    _sign_in_participant(client, stranger)
    response = client.get(
        reverse(
            "badges:participant-pass-print",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code == 404


def test_the_print_view_renders_for_the_owner(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    response = client.get(
        reverse(
            "badges:participant-pass-print",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code == 200
    assert b"pass_print.css" in response.content


def test_participant_credential_surfaces_are_not_cached(
    client, eligible_registration, pass_admin, active_key, person
):
    _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    response = client.get(reverse("badges:participant-passes"))
    assert "no-store" in response["Cache-Control"]


def test_no_credential_value_appears_in_a_url(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    print_url = reverse(
        "badges:participant-pass-print",
        kwargs={"public_id": credential.series.public_id},
    )
    assert credential.jti not in print_url
    assert credential.series.fallback_reference not in print_url
    assert credential.nonce not in print_url


# ---------------------------------------------------------------------------
# Localization and RTL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_participant_pass_renders_in_every_supported_language(
    client, eligible_registration, pass_admin, active_key, person, language
):
    _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    response = client.get(
        reverse("badges:participant-passes"), headers={"accept-language": language}
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert f'lang="{language}"' in body


def test_arabic_renders_right_to_left_with_an_isolated_reference(
    client, eligible_registration, pass_admin, active_key, person
):
    _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    response = client.get(reverse("badges:participant-passes"), headers={"accept-language": "ar"})
    body = response.content.decode()
    assert 'dir="rtl"' in body
    # The reference and the credential value stay LTR inside RTL layout.
    assert "<bdi>" in body


def test_the_print_view_renders_in_arabic(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    response = client.get(
        reverse(
            "badges:participant-pass-print",
            kwargs={"public_id": credential.series.public_id},
        ),
        headers={"accept-language": "ar"},
    )
    assert response.status_code == 200
    assert 'dir="rtl"' in response.content.decode()


# ---------------------------------------------------------------------------
# Verification key surface
# ---------------------------------------------------------------------------


def test_a_custodian_can_view_the_key_surface(client, key_custodian, active_key):
    sign_in_operational(client, key_custodian.email_normalized)
    response = client.get(reverse("badges:verification-keys"))
    assert response.status_code == 200
    assert b"public verification keys" in response.content.lower()


def test_the_key_surface_never_renders_private_material(client, key_custodian, active_key):
    sign_in_operational(client, key_custodian.email_normalized)
    body = client.get(reverse("badges:verification-keys")).content.decode()
    assert "PRIVATE KEY" not in body.upper()


def test_publishing_a_private_pem_is_refused_at_the_view(client, key_custodian):
    sign_in_operational(client, key_custodian.email_normalized)
    response = client.post(
        reverse("badges:verification-keys"),
        {
            "key_id": "v4",
            "public_key_pem": _fake_private_pem(),
        },
    )
    assert response.status_code == 200
    assert b"public key material only" in response.content


def test_the_published_key_set_contains_public_material_only(client, pass_admin, active_key):
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.get(reverse("badges:verification-key-set"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["keys"][0]["alg"] == "ES256"
    assert "PUBLIC KEY" in payload["keys"][0]["public_key_pem"]
    assert "PRIVATE" not in response.content.decode().upper()
