"""HTTP boundary of the Entry & Security space: device cookie, checkpoint set-up,
minimum-data results, external-security view, and the no-browsing rule."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUserAccountType
from apps.accounts.tests.sign_in import staff_sign_in
from apps.entry.models import (
    EntryEvent,
    EntryOperatorSession,
    RestrictionCategory,
    RestrictionSeverity,
)
from apps.entry.services.restrictions import create_restriction
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db

SYNTHETIC_NIN = "109870123456789012"
UNTRUSTED_INPUT = "not-a-credential"


def _sign_in(client, user, next_url=None):
    return staff_sign_in(client, user.email_normalized, factories.TEST_PASSWORD, next_url=next_url)


def _on_device(client, secret):
    client.cookies[settings.ENTRY_DEVICE_COOKIE_NAME] = secret


def _start(client, user, secret, zone):
    _sign_in(client, user)
    _on_device(client, secret)
    response = client.post(reverse("entry:home"), {"action": "start", "zone_id": str(zone.pk)})
    assert response.status_code == 302, response.content[:500]
    assert response["Location"] == reverse("entry:verify")


def _pending_operation_id(response) -> str:
    return response.context["pending_operation_id"]


@pytest.fixture
def signed_in_checkpoint(client, operator, device_and_secret, layout):
    _device, secret = device_and_secret
    _start(client, operator, secret, layout.main)
    return client


class TestDeviceGate:
    def test_checkpoint_requires_sign_in(self, client):
        response = client.get(reverse("entry:verify"))
        assert response.status_code == 302
        assert reverse("accounts:operational-sign-in") in response["Location"]

    def test_browser_without_device_credential_is_refused(self, client, operator):
        _sign_in(client, operator)
        response = client.get(reverse("entry:home"))
        assert response.status_code == 403
        assert b"not enrolled" in response.content

    def test_forged_device_cookie_is_refused(self, client, operator, device):
        _sign_in(client, operator)
        _on_device(client, "F" * 43)
        assert client.get(reverse("entry:home")).status_code == 403

    def test_verify_without_checkpoint_session_redirects_to_setup(
        self, client, operator, device_and_secret
    ):
        _sign_in(client, operator)
        _on_device(client, device_and_secret[1])
        response = client.get(reverse("entry:verify"))
        assert response.status_code == 302
        assert response["Location"] == reverse("entry:home")

    def test_setup_outside_scope_is_refused(self, client, operator, device_and_secret, layout):
        _sign_in(client, operator)
        _on_device(client, device_and_secret[1])
        response = client.post(
            reverse("entry:home"), {"action": "start", "zone_id": str(layout.vip.pk)}
        )
        assert response.status_code == 200
        assert not EntryOperatorSession.objects.exists()

    def test_activation_view_sets_a_strict_http_only_cookie(
        self, client, event, layout, device_admin
    ):
        from apps.entry.services.devices import register_device

        registration = register_device(
            event_edition=event,
            public_name="Desk",
            expires_at=timezone.now() + timedelta(days=2),
            venue=layout.venue,
            gate=layout.gate_a,
            zones=[layout.main],
            verification_methods=["QR"],
            actor=device_admin,
        )
        _sign_in(client, device_admin)
        response = client.post(
            reverse("entry:device-activate"), {"code": registration.activation_code}
        )
        assert response.status_code == 302
        cookie = response.cookies[settings.ENTRY_DEVICE_COOKIE_NAME]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Strict"
        assert cookie["path"] == "/entry/"
        assert registration.activation_code not in response.content.decode()

    def test_activation_view_requires_device_administration(self, client, operator):
        _sign_in(client, operator)
        assert client.get(reverse("entry:device-activate")).status_code == 403

    def test_sign_in_redirects_to_a_requested_entry_page_only(self, client, operator):
        response = _sign_in(client, operator, next_url="/entry/")
        assert response["Location"] == "/entry/"
        client.logout()
        response = _sign_in(client, operator, next_url="https://evil.example/entry/")
        assert response["Location"] == reverse("registrations:ops-intake-list")


class TestVerificationScreens:
    def test_verify_page_offers_only_permitted_methods(self, signed_in_checkpoint):
        response = signed_in_checkpoint.get(reverse("entry:verify"))
        assert response.status_code == 200
        assert response.context["qr_form"] is not None
        assert response.context["identity_form"] is not None
        assert response.context["reference_form"] is not None
        assert response.context["manual_form"] is None  # supervisors only
        assert "no-store" in response["Cache-Control"]
        assert b"Online" in response.content

    def test_qr_result_shows_minimum_data_and_never_the_token(
        self, signed_in_checkpoint, active_pass, person
    ):
        token = factories.token_for(active_pass)
        response = signed_in_checkpoint.post(reverse("entry:verify-qr"), {"token": token})
        body = response.content.decode()
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert person.display_name in body
        assert token not in body
        assert response.context["can_admit"] is True
        assert response.context["projection"]["reason_label"] == ""
        assert b"No reason" not in response.content
        participant = response.context["projection"]["participant"]
        assert set(participant) == {
            "display_name",
            "registration_reference",
            "has_photo",
            "badge_type",
            "identity_hint",
        }

    def test_admit_flow_records_one_event_even_when_double_submitted(
        self, signed_in_checkpoint, active_pass
    ):
        response = signed_in_checkpoint.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        operation_id = _pending_operation_id(response)
        for _ in range(2):
            decision = signed_in_checkpoint.post(
                reverse("entry:decision"), {"operation_id": operation_id, "decision": "ADMIT"}
            )
            assert decision.status_code == 302
        assert EntryEvent.objects.count() == 1
        follow = signed_in_checkpoint.get(reverse("entry:verify"))
        messages = [str(m) for m in follow.context["messages"]]
        assert any("already recorded" in m for m in messages)

    def test_forged_operation_id_matches_nothing(self, signed_in_checkpoint):
        response = signed_in_checkpoint.post(
            reverse("entry:decision"),
            {"operation_id": "A" * 32, "decision": "ADMIT"},
        )
        assert response.status_code == 302
        assert not EntryEvent.objects.exists()

    def test_wrong_event_result_exposes_no_participant_and_no_decision(
        self, signed_in_checkpoint, other_event, key, staff
    ):
        other_layout = factories.VenueLayout(other_event, code="OV")
        other_setup = factories.AccreditationSetup(other_event, other_layout, suffix="O")
        other_person = factories.make_person("Hidden Other Event Person")
        other_registration = factories.make_registration(event=other_event, person=other_person)
        factories.assign(registration=other_registration, setup=other_setup, actor=staff)
        other_pass = factories.issue_active_pass(registration=other_registration, actor=staff)
        response = signed_in_checkpoint.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(other_pass)}
        )
        assert b"Hidden Other Event Person" not in response.content
        assert response.context["pending_operation_id"] is None

    def test_identity_lookup_never_echoes_the_value(
        self, signed_in_checkpoint, person, active_pass
    ):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        response = signed_in_checkpoint.post(
            reverse("entry:lookup-identity"), {"method": "NIN", "value": SYNTHETIC_NIN}
        )
        assert response.status_code == 200
        assert SYNTHETIC_NIN.encode() not in response.content
        assert response.context["projection"]["participant"]["identity_hint"].endswith(
            SYNTHETIC_NIN[-4:]
        )

    def test_candidate_selection_uses_server_held_tokens(
        self, signed_in_checkpoint, event, person, registration, setup, staff, active_pass
    ):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        second = factories.make_registration(event=event, person=person)
        factories.assign(registration=second, setup=setup, actor=staff)
        response = signed_in_checkpoint.post(
            reverse("entry:lookup-identity"), {"method": "NIN", "value": SYNTHETIC_NIN}
        )
        assert response.templates[0].name == "entry/candidates.html"
        body = response.content.decode()
        assert str(registration.pk) not in body and str(second.pk) not in body
        token = response.context["selection_token"]
        forged = signed_in_checkpoint.post(
            reverse("entry:select-candidate"), {"selection": "B" * 24, "index": 0}
        )
        assert forged.status_code == 302
        chosen = signed_in_checkpoint.post(
            reverse("entry:select-candidate"), {"selection": token, "index": 0}
        )
        assert chosen.status_code == 200
        assert chosen.context["projection"]["participant"] is not None

    def test_manual_search_endpoint_is_refused_for_operators(self, signed_in_checkpoint):
        response = signed_in_checkpoint.post(reverse("entry:lookup-manual"), {"query": "Synth"})
        assert response.status_code == 403

    def test_monitor_is_refused_for_operators(self, signed_in_checkpoint):
        assert signed_in_checkpoint.get(reverse("entry:monitor")).status_code == 403

    def test_photo_requires_a_live_nonce(self, signed_in_checkpoint):
        assert signed_in_checkpoint.get(reverse("entry:photo", args=["x" * 24])).status_code == 404

    def test_ending_the_session_clears_checkpoint_state(self, signed_in_checkpoint, active_pass):
        signed_in_checkpoint.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        response = signed_in_checkpoint.post(reverse("entry:session-end"))
        assert response.status_code == 302
        assert not EntryOperatorSession.objects.filter(ended_at__isnull=True).exists()
        session = signed_in_checkpoint.session
        assert not any(key.startswith("entry_") for key in session.keys())

    def test_operational_sign_out_ends_the_operator_session(self, signed_in_checkpoint):
        signed_in_checkpoint.post(reverse("accounts:operational-sign-out"))
        session = EntryOperatorSession.objects.get()
        assert session.ended_at is not None
        assert session.end_reason == "SIGNED_OUT"


class TestSupervisorScreens:
    def test_supervisor_sees_monitor_and_manual_search(
        self, client, supervisor, device_and_secret, layout, registration, active_pass
    ):
        _start(client, supervisor, device_and_secret[1], layout.main)
        response = client.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        client.post(
            reverse("entry:decision"),
            {"operation_id": _pending_operation_id(response), "decision": "ADMIT"},
        )
        client.post(reverse("entry:verify-qr"), {"token": UNTRUSTED_INPUT})
        monitor = client.get(reverse("entry:monitor"))
        assert monitor.status_code == 200
        assert len(monitor.context["events"]) == 1
        assert len(monitor.context["denied_attempts"]) == 1
        search = client.post(reverse("entry:lookup-manual"), {"query": "Synthetic"})
        assert search.status_code == 200


class TestExternalSecurityView:
    @pytest.fixture
    def external(self, event, layout):
        return factories.make_user(
            "external@example.test",
            group_name="Entry Operators",
            event=event,
            gate=layout.gate_a,
            account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
            active_until=timezone.now() + timedelta(days=2),
        )

    def test_external_view_hides_badge_type_and_identity_hint(
        self, client, external, device_and_secret, layout, active_pass
    ):
        _start(client, external, device_and_secret[1], layout.main)
        response = client.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        participant = response.context["projection"]["participant"]
        assert set(participant) == {"display_name", "registration_reference", "has_photo"}
        assert b"Standard" not in response.content

    def test_external_view_hides_restriction_wording(
        self, client, event, external, device_and_secret, layout, person, active_pass
    ):
        manager = factories.make_user(
            "rm@example.test", group_name="Security Restriction Managers", event=event
        )
        create_restriction(
            actor=manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.DENY_ENTRY,
            category=RestrictionCategory.SECURITY_CONCERN,
            reason="Synthetic restricted reason",
        )
        _start(client, external, device_and_secret[1], layout.main)
        response = client.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        body = response.content.decode()
        projection = response.context["projection"]
        assert projection["result"] == "DENIED"
        assert projection["reason_code"] == ""
        assert projection["escalate_restricted"] is True
        assert "Security restriction" not in body
        assert "Security concern" not in body
        assert "Synthetic restricted reason" not in body

    def test_internal_operator_sees_procedure_but_not_category_without_permission(
        self, client, event, operator, device_and_secret, layout, person, active_pass
    ):
        manager = factories.make_user(
            "rm2@example.test", group_name="Security Restriction Managers", event=event
        )
        create_restriction(
            actor=manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.DENY_ENTRY,
            category=RestrictionCategory.SECURITY_CONCERN,
            reason="Synthetic restricted reason",
        )
        _start(client, operator, device_and_secret[1], layout.main)
        response = client.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        projection = response.context["projection"]
        assert projection["reason_code"] == "SECURITY_RESTRICTION"
        assert projection["restriction_categories"] == ()
        assert "Security concern" not in response.content.decode()

    def test_expired_external_account_cannot_sign_in(self, client, external):
        external.active_until = timezone.now() - timedelta(seconds=1)
        external.save()
        _sign_in(client, external)
        assert not client.session.get("_auth_user_id")


class TestNoGeneralParticipantBrowsing:
    @pytest.mark.parametrize(
        "url_name,kwargs",
        [
            ("registrations:ops-intake-list", {}),
            ("exports:workspace", {}),
        ],
    )
    def test_entry_users_cannot_open_back_office_screens(
        self, client, operator, supervisor, url_name, kwargs
    ):
        for user in (operator, supervisor):
            client.logout()
            _sign_in(client, user)
            response = client.get(reverse(url_name, kwargs=kwargs))
            assert response.status_code in (302, 403, 404)
            if response.status_code == 302:
                assert reverse("accounts:operational-sign-in") in response["Location"]

    def test_entry_users_cannot_open_a_registration_or_document(
        self, client, operator, registration
    ):
        _sign_in(client, operator)
        for url in (
            reverse("registrations:ops-intake-detail", args=[registration.pk]),
            reverse("badges:registration-credential", args=[registration.pk]),
            reverse("badges:registration-badge-issuance", args=[registration.pk]),
        ):
            response = client.get(url)
            assert response.status_code in (302, 403, 404), url

    def test_entry_groups_hold_no_participant_data_permission(self):
        from django.contrib.auth.models import Group

        for name in ("Entry Operators", "Entry Supervisors", "Entry Device Administrators"):
            codenames = set(
                Group.objects.get(name=name).permissions.values_list("codename", flat=True)
            )
            assert not codenames & {
                "view_registration",
                "view_registrationprofile",
                "view_document",
                "view_exportrequest",
            }, name


class TestDeviceAdministrationViews:
    def test_device_admin_registers_and_sees_the_code_once(
        self, client, event, layout, device_admin
    ):
        _sign_in(client, device_admin)
        url = reverse("entry:device-list", args=[event.pk])
        assert client.get(url).status_code == 200
        response = client.post(
            url,
            {
                "public_name": "Gate A tablet 9",
                "expires_at": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
                "gate_id": str(layout.gate_a.pk),
                "zone_ids": [str(layout.main.pk)],
                "verification_methods": ["QR", "REFERENCE"],
            },
        )
        assert response.status_code == 200
        assert response.templates[0].name == "entry/device_activation_code.html"
        assert "no-store" in response["Cache-Control"]
        device_page = client.get(url)
        assert response.context["code"] not in device_page.content.decode()

    def test_operators_cannot_administer_devices(self, client, event, operator, device):
        _sign_in(client, operator)
        # Signed in without the permission: the 403 page, never a sign-in
        # redirect (UI/UX Completion Gate F3); nothing is changed.
        assert client.get(reverse("entry:device-list", args=[event.pk])).status_code == 403
        response = client.post(
            reverse("entry:device-revoke", args=[device.public_id]),
            {"expected_version": device.version, "reason_code": "LOST_OR_STOLEN"},
        )
        assert response.status_code == 403
        device.refresh_from_db()
        assert device.status == "ENROLLED"

    def test_device_admin_revokes_through_the_view(self, client, device_admin, device):
        _sign_in(client, device_admin)
        response = client.post(
            reverse("entry:device-revoke", args=[device.public_id]),
            {"expected_version": device.version, "reason_code": "LOST_OR_STOLEN"},
        )
        assert response.status_code == 302
        device.refresh_from_db()
        assert device.status == "REVOKED"

    def test_stale_device_form_is_a_conflict(self, client, device_admin, device):
        _sign_in(client, device_admin)
        response = client.post(
            reverse("entry:device-suspend", args=[device.public_id]),
            {"expected_version": device.version + 7, "reason_code": "MAINTENANCE"},
        )
        assert response.status_code == 409

    def test_device_of_another_event_is_not_found(self, client, other_event, device):
        other_admin = factories.make_user(
            "oa@example.test", group_name="Entry Device Administrators", event=other_event
        )
        _sign_in(client, other_admin)
        assert (
            client.get(reverse("entry:device-detail", args=[device.public_id])).status_code == 404
        )
