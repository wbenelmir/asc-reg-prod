"""Phase 3 Prompt 8 (P8-06): pass activation, resumption and QR exposure follow
the Registration Context's status.

Participant withdrawal and operational cancellation do not revoke a Digital
Entry Pass -- its history is preserved and online verification keeps
denying it. Before this correction, though, an INACTIVE or SUSPENDED pass
of such a context could still be activated or resumed, and an ACTIVE one
still showed the participant a QR that looked usable. All data is synthetic.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import DigitalEntryPass, DigitalEntryPassStatus, PassLifecycleOperation
from apps.badges.services import (
    PassNotEligibleError,
    PassStateError,
    activate_pass,
    generate_pass,
    issue_pass_token,
    new_operation_id,
    pass_exposes_usable_qr,
    reconstruct_pass_token,
    resume_pass,
    suspend_pass,
)
from apps.core.models import OutboxEvent
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.registrations.selectors import active_approved_context_q, is_active_approved_context
from apps.reviews.services import cancel_registration_operationally, withdraw_registration

pytestmark = pytest.mark.django_db


def _withdraw(registration):
    registration.refresh_from_db()
    withdraw_registration(
        registration=registration,
        person=registration.person,
        expected_version=registration.version,
    )


def _cancel(registration, actor):
    registration.refresh_from_db()
    cancel_registration_operationally(
        registration=registration,
        expected_version=registration.version,
        reason="SYNTHETIC_OPERATIONAL_CANCELLATION",
        actor=actor,
    )


END_CONTEXT = {
    "withdrawal": lambda registration, actor: _withdraw(registration),
    "cancellation": _cancel,
}


def _generate(registration, actor):
    return generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential


def _activate(credential, actor):
    return activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    ).credential


def _evidence(credential):
    return {
        "operations": PassLifecycleOperation.objects.filter(resulting_pass=credential).count(),
        "outbox": OutboxEvent.objects.filter(aggregate_id=str(credential.pk)).count(),
        "audits": AuditEvent.objects.filter(target_uuid=credential.pk).count(),
    }


def _sign_in_participant(client, person):
    from django.utils import timezone

    from apps.accounts import participant_auth, session_expiry

    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()


# ---------------------------------------------------------------------------
# Shared predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("public_status", RegistrationPublicStatus.values)
@pytest.mark.parametrize("withdrawn", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("current", [False, True])
def test_the_in_memory_and_queryset_definitions_agree(
    eligible_registration, public_status, withdrawn, cancelled, current
):
    from django.utils import timezone

    Registration.objects.filter(pk=eligible_registration.pk).update(
        public_status=public_status,
        withdrawn_at=timezone.now() if withdrawn else None,
        cancelled_at=timezone.now() if cancelled else None,
        is_current_context=current,
    )
    registration = Registration.objects.get(pk=eligible_registration.pk)
    in_queryset = Registration.objects.filter(active_approved_context_q(), pk=registration.pk)
    assert is_active_approved_context(registration) is in_queryset.exists()


# ---------------------------------------------------------------------------
# Activation and resumption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("end_context", sorted(END_CONTEXT))
def test_activation_is_refused_after_the_context_ends(
    end_context, eligible_registration, pass_admin, active_key
):
    credential = _generate(eligible_registration, pass_admin)
    END_CONTEXT[end_context](eligible_registration, pass_admin)
    before = _evidence(credential)

    with pytest.raises(PassNotEligibleError):
        _activate(credential, pass_admin)

    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE
    assert credential.version == 1
    assert credential.activated_at is None
    assert _evidence(credential) == before
    assert not OutboxEvent.objects.filter(event_type="badges.pass.activated").exists()
    assert not AuditEvent.objects.filter(action_code=action_codes.CREDENTIAL_ACTIVATED).exists()


@pytest.mark.parametrize("end_context", sorted(END_CONTEXT))
def test_resumption_is_refused_after_the_context_ends(
    end_context, eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    credential = suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code="SECURITY_CONCERN",
    ).credential
    END_CONTEXT[end_context](eligible_registration, pass_admin)
    before = _evidence(credential)
    version = credential.version

    with pytest.raises(PassNotEligibleError):
        resume_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_lock_version=credential.version,
        )

    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED
    assert credential.version == version
    assert credential.resumed_at is None
    assert _evidence(credential) == before
    assert not OutboxEvent.objects.filter(event_type="badges.pass.resumed").exists()


def test_the_check_reads_the_locked_row_not_the_callers_stale_object(
    eligible_registration, pass_admin, active_key
):
    """The view hands the service a credential whose `registration` was
    loaded while the context was still approved; the refusal must still
    happen."""
    credential = DigitalEntryPass.objects.select_related("registration").get(
        pk=_generate(eligible_registration, pass_admin).pk
    )
    assert is_active_approved_context(credential.registration)
    _withdraw(Registration.objects.get(pk=eligible_registration.pk))
    # The in-memory relation still reads APPROVED.
    assert credential.registration.public_status == RegistrationPublicStatus.APPROVED
    with pytest.raises(PassNotEligibleError):
        _activate(credential, pass_admin)


def test_an_existing_credential_is_neither_revoked_nor_replaced_by_withdrawal(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    _withdraw(eligible_registration)
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE
    assert DigitalEntryPass.objects.filter(registration=eligible_registration).count() == 1


def test_an_approved_context_is_still_activated_normally(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    assert credential.status == DigitalEntryPassStatus.ACTIVE
    assert pass_exposes_usable_qr(credential) is True
    assert issue_pass_token(credential).count(".") == 2


# ---------------------------------------------------------------------------
# QR exposure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("end_context", sorted(END_CONTEXT))
def test_no_usable_qr_and_a_direct_token_request_is_refused(
    end_context, eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    END_CONTEXT[end_context](eligible_registration, pass_admin)
    credential = DigitalEntryPass.objects.select_related("registration").get(pk=credential.pk)

    assert pass_exposes_usable_qr(credential) is False
    with pytest.raises(PassStateError):
        issue_pass_token(credential)


def test_issue_pass_token_rereads_the_registration(eligible_registration, pass_admin, active_key):
    credential = DigitalEntryPass.objects.select_related("registration").get(
        pk=_activate(_generate(eligible_registration, pass_admin), pass_admin).pk
    )
    _withdraw(Registration.objects.get(pk=eligible_registration.pk))
    # The caller's related object is stale and still says APPROVED...
    assert credential.registration.public_status == RegistrationPublicStatus.APPROVED
    # ...but the token is still refused.
    with pytest.raises(PassStateError):
        issue_pass_token(credential)


@pytest.mark.parametrize("end_context", sorted(END_CONTEXT))
def test_the_participant_surface_shows_no_usable_qr_and_the_endpoint_is_404(
    end_context, client, eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    series_public_id = credential.series.public_id
    qr_url = reverse("badges:participant-pass-qr", kwargs={"public_id": series_public_id})
    _sign_in_participant(client, eligible_registration.person)
    assert client.get(qr_url).status_code == 200

    END_CONTEXT[end_context](eligible_registration, pass_admin)

    page = client.get(reverse("badges:participant-passes"))
    assert page.status_code == 200
    [card] = page.context["passes"]
    assert card["has_usable_qr"] is False
    assert card["qr_image_url"] == ""
    assert card["may_become_active"] is False
    body = page.content.decode("utf-8")
    assert qr_url not in body
    assert "Your entry pass is active." not in body
    assert "Your entry pass is no longer valid." in body

    print_view = client.get(
        reverse("badges:participant-pass-print", kwargs={"public_id": series_public_id})
    )
    assert print_view.status_code == 200
    assert print_view.context["has_usable_qr"] is False
    assert qr_url not in print_view.content.decode("utf-8")

    assert client.get(qr_url).status_code == 404


def test_an_inactive_pass_of_a_withdrawn_context_promises_no_activation(
    client, eligible_registration, pass_admin, active_key
):
    _generate(eligible_registration, pass_admin)
    _withdraw(eligible_registration)
    _sign_in_participant(client, eligible_registration.person)
    [card] = client.get(reverse("badges:participant-passes")).context["passes"]
    assert card["has_usable_qr"] is False
    assert card["may_become_active"] is False


def test_the_withdrawn_context_credential_history_still_reconstructs(
    eligible_registration, pass_admin, active_key
):
    """History is preserved: the stored artefact still reconstructs (for the
    operator's record and for the gate's denial), it is simply no longer
    handed to the participant."""
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    _withdraw(eligible_registration)
    credential.refresh_from_db()
    assert reconstruct_pass_token(credential).count(".") == 2
