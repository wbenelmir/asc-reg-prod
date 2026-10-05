"""Key lifecycle operations, idempotency binding, scoped lookup, terminal display.

Covers the remaining correction-pass items that are not about the credential
artefact itself.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import (
    DigitalEntryPassStatus,
    PassLifecycleOperation,
    PassLifecycleOperationType,
    PassReasonCode,
    VerificationKey,
    VerificationKeyReasonCode,
    VerificationKeyStatus,
)
from apps.badges.services import (
    FallbackLookupThrottled,
    OperationConflictError,
    VerificationKeyError,
    activate_pass,
    generate_pass,
    lookup_series_by_fallback_reference,
    new_operation_id,
    retire_verification_key,
    revoke_pass,
    revoke_verification_key,
    suspend_pass,
)
from apps.badges.tests.conftest import (
    grant_required_assignments,
    make_operational_user_with_membership,
    make_registration,
    sign_in_operational,
)

pytestmark = pytest.mark.django_db


def _issue(registration, actor):
    return generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential


def _activated(registration, actor):
    credential = _issue(registration, actor)
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return credential


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
# 5. Verification-key retirement and emergency revocation
# ---------------------------------------------------------------------------


def test_retiring_an_active_key_records_a_controlled_reason(active_key, key_custodian):
    retire_verification_key(
        key=active_key,
        actor=key_custodian,
        reason_code=VerificationKeyReasonCode.PLANNED_ROTATION,
    )
    active_key.refresh_from_db()
    assert active_key.status == VerificationKeyStatus.RETIRED

    entry = AuditEvent.objects.filter(action_code=action_codes.VERIFICATION_KEY_RETIRED).latest(
        "occurred_at"
    )
    assert entry.reason_code == VerificationKeyReasonCode.PLANNED_ROTATION
    assert "PRIVATE" not in str(entry.after_summary).upper()


def test_revoking_a_key_records_a_controlled_reason(active_key, key_custodian):
    revoke_verification_key(
        key=active_key,
        actor=key_custodian,
        reason_code=VerificationKeyReasonCode.CONFIRMED_COMPROMISE,
    )
    active_key.refresh_from_db()
    assert active_key.status == VerificationKeyStatus.REVOKED
    assert active_key.revocation_reason_code == VerificationKeyReasonCode.CONFIRMED_COMPROMISE


def test_an_unrecognized_reason_code_is_refused(active_key, key_custodian):
    with pytest.raises(VerificationKeyError, match="Unrecognized"):
        revoke_verification_key(key=active_key, actor=key_custodian, reason_code="MADE_UP_REASON")


def test_only_an_active_key_can_be_retired(signing_provider, key_custodian):
    from apps.badges.services import publish_verification_key

    pending = publish_verification_key(
        key_id="v2",
        public_key_pem=signing_provider.public_key_pem("v2").decode("ascii"),
        actor=key_custodian,
    )
    with pytest.raises(VerificationKeyError, match="Only an active"):
        retire_verification_key(
            key=pending,
            actor=key_custodian,
            reason_code=VerificationKeyReasonCode.PLANNED_ROTATION,
        )


def test_retirement_is_idempotent(active_key, key_custodian):
    operation_id = new_operation_id()
    retire_verification_key(
        key=active_key,
        actor=key_custodian,
        reason_code=VerificationKeyReasonCode.PLANNED_ROTATION,
        operation_id=operation_id,
    )
    retire_verification_key(
        key=active_key,
        actor=key_custodian,
        reason_code=VerificationKeyReasonCode.PLANNED_ROTATION,
        operation_id=operation_id,
    )
    assert (
        PassLifecycleOperation.objects.filter(
            operation_type=PassLifecycleOperationType.KEY_RETIRE
        ).count()
        == 1
    )


def test_revocation_is_idempotent(active_key, key_custodian):
    operation_id = new_operation_id()
    for _ in range(2):
        revoke_verification_key(
            key=active_key,
            actor=key_custodian,
            reason_code=VerificationKeyReasonCode.SUSPECTED_COMPROMISE,
            operation_id=operation_id,
        )
    assert (
        PassLifecycleOperation.objects.filter(
            operation_type=PassLifecycleOperationType.KEY_REVOKE
        ).count()
        == 1
    )


@pytest.mark.parametrize(
    "route", ["badges:verification-key-retire", "badges:verification-key-revoke"]
)
def test_key_lifecycle_routes_reject_get(client, active_key, key_custodian, route):
    sign_in_operational(client, key_custodian.email_normalized)
    assert client.get(reverse(route, kwargs={"pk": active_key.pk})).status_code == 405


@pytest.mark.parametrize(
    "route", ["badges:verification-key-retire", "badges:verification-key-revoke"]
)
def test_key_lifecycle_routes_require_the_key_permission(client, active_key, pass_admin, route):
    """A pass administrator holds no key permission, by design."""
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.post(
        reverse(route, kwargs={"pk": active_key.pk}),
        {"operation_id": new_operation_id(), "reason_code": "PLANNED_ROTATION"},
    )
    assert response.status_code in (302, 403)
    active_key.refresh_from_db()
    assert active_key.status == VerificationKeyStatus.ACTIVE


@pytest.mark.parametrize(
    "route", ["badges:verification-key-retire", "badges:verification-key-revoke"]
)
def test_key_lifecycle_routes_enforce_csrf(client, active_key, key_custodian, route):
    from django.test import Client

    enforcing = Client(enforce_csrf_checks=True)
    sign_in_operational(enforcing, key_custodian.email_normalized)
    response = enforcing.post(
        reverse(route, kwargs={"pk": active_key.pk}),
        {"operation_id": new_operation_id(), "reason_code": "PLANNED_ROTATION"},
    )
    assert response.status_code == 403
    active_key.refresh_from_db()
    assert active_key.status == VerificationKeyStatus.ACTIVE


def test_revocation_through_the_view_requires_a_reason(client, active_key, key_custodian):
    sign_in_operational(client, key_custodian.email_normalized)
    response = client.post(
        reverse("badges:verification-key-revoke", kwargs={"pk": active_key.pk}),
        {"operation_id": new_operation_id()},
    )
    assert response.status_code == 409
    active_key.refresh_from_db()
    assert active_key.status == VerificationKeyStatus.ACTIVE


def test_the_key_page_shows_retire_and_revoke_controls(client, active_key, key_custodian):
    sign_in_operational(client, key_custodian.email_normalized)
    body = client.get(reverse("badges:verification-keys")).content.decode()
    assert f"/verification-keys/{active_key.pk}/retire/" in body
    assert f"/verification-keys/{active_key.pk}/revoke/" in body


def test_each_key_control_carries_a_distinct_operation_id(client, active_key, key_custodian):
    import re

    sign_in_operational(client, key_custodian.email_normalized)
    body = client.get(reverse("badges:verification-keys")).content.decode()
    ids = re.findall(r'name="operation_id" value="([^"]+)"', body)
    assert len(ids) == len(set(ids)), "no two forms may share an operation identifier"


# ---------------------------------------------------------------------------
# 6. Operation identifiers bind to exactly one command
# ---------------------------------------------------------------------------


def test_reusing_an_operation_id_for_a_different_command_is_a_conflict(
    eligible_registration, pass_admin, active_key
):
    """The hole this closes: a replayed suspend must not report a revoke."""
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()

    suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    credential.refresh_from_db()

    with pytest.raises(OperationConflictError):
        revoke_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=operation_id,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED


def test_reusing_an_operation_id_with_a_different_actor_is_a_conflict(
    eligible_registration, pass_admin, active_key, event, organization
):
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()
    suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    other = make_operational_user_with_membership(
        email="second.admin@example.test",
        group_name="Pass Administrators",
        event_edition=event,
        organization=organization,
    )
    credential.refresh_from_db()
    with pytest.raises(OperationConflictError):
        suspend_pass(
            credential=credential,
            actor=other,
            operation_id=operation_id,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.SECURITY_CONCERN,
        )


def test_reusing_an_operation_id_against_a_different_target_is_a_conflict(
    eligible_registration,
    pass_admin,
    active_key,
    event,
    organization,
    role,
    badge_type,
    access_profile,
):
    from apps.people.models import Person, PersonStatus

    first = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()
    suspend_pass(
        credential=first,
        actor=pass_admin,
        operation_id=operation_id,
        expected_lock_version=first.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )

    other_person = Person.objects.create(status=PersonStatus.ACTIVE)
    other_registration = make_registration(
        event=event, organization=organization, person=other_person
    )
    grant_required_assignments(
        registration=other_registration,
        event=event,
        actor=pass_admin,
        role=role,
        badge_type=badge_type,
        access_profile=access_profile,
    )
    second = _activated(other_registration, pass_admin)

    with pytest.raises(OperationConflictError):
        suspend_pass(
            credential=second,
            actor=pass_admin,
            operation_id=operation_id,
            expected_lock_version=second.version,
            reason_code=PassReasonCode.SECURITY_CONCERN,
        )
    second.refresh_from_db()
    assert second.status == DigitalEntryPassStatus.ACTIVE


def test_an_identical_repeat_is_still_a_clean_replay(eligible_registration, pass_admin, active_key):
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()
    kwargs = {
        "credential": credential,
        "actor": pass_admin,
        "operation_id": operation_id,
        "expected_lock_version": credential.version,
        "reason_code": PassReasonCode.SECURITY_CONCERN,
    }
    first = suspend_pass(**kwargs)
    second = suspend_pass(**kwargs)

    assert first.replayed is False
    assert second.replayed is True
    assert second.credential.pk == first.credential.pk


# ---------------------------------------------------------------------------
# 7. Fallback lookup: scope filtering and throttling
# ---------------------------------------------------------------------------


def test_an_out_of_scope_match_is_indistinguishable_from_unknown(
    eligible_registration, pass_admin, active_key, other_event, organization
):
    credential = _issue(eligible_registration, pass_admin)
    reference = credential.series.fallback_reference

    stranger = make_operational_user_with_membership(
        email="other.scope@example.test",
        group_name="Pass Administrators",
        event_edition=other_event,
        organization=organization,
    )
    found = lookup_series_by_fallback_reference(raw_reference=reference, actor=stranger)
    unknown = lookup_series_by_fallback_reference(raw_reference="ASC-0000-0000-0", actor=stranger)
    assert found is None
    assert unknown is None


def test_a_cross_organization_match_is_hidden(
    eligible_registration, pass_admin, active_key, event, other_organization
):
    credential = _issue(eligible_registration, pass_admin)
    stranger = make_operational_user_with_membership(
        email="other.org.lookup@example.test",
        group_name="Pass Administrators",
        event_edition=event,
        organization=other_organization,
    )
    assert (
        lookup_series_by_fallback_reference(
            raw_reference=credential.series.fallback_reference, actor=stranger
        )
        is None
    )


def test_an_in_scope_match_is_returned(eligible_registration, pass_admin, active_key):
    credential = _issue(eligible_registration, pass_admin)
    found = lookup_series_by_fallback_reference(
        raw_reference=credential.series.fallback_reference, actor=pass_admin
    )
    assert found is not None
    assert found.pk == credential.series_id


def test_malformed_input_is_throttled_too(pass_admin, settings):
    """Rejecting bad input for free would leave an unthrottled oracle."""
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 3
    for _ in range(3):
        lookup_series_by_fallback_reference(raw_reference="!!!not-a-reference!!!", actor=pass_admin)
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(raw_reference="!!!still-bad!!!", actor=pass_admin)


def test_an_out_of_scope_lookup_is_audited_without_leaking_the_target(
    eligible_registration, pass_admin, active_key, other_event, organization
):
    credential = _issue(eligible_registration, pass_admin)
    stranger = make_operational_user_with_membership(
        email="audit.scope@example.test",
        group_name="Pass Administrators",
        event_edition=other_event,
        organization=organization,
    )
    lookup_series_by_fallback_reference(
        raw_reference=credential.series.fallback_reference, actor=stranger
    )
    entry = AuditEvent.objects.filter(action_code=action_codes.FALLBACK_REFERENCE_LOOKUP).latest(
        "occurred_at"
    )
    assert entry.result == "FAILURE"
    assert entry.target_uuid is None
    assert eligible_registration.public_reference not in str(entry.after_summary)


def test_the_lookup_view_never_reveals_an_out_of_scope_registration(
    client, eligible_registration, pass_admin, active_key, other_event, organization
):
    credential = _issue(eligible_registration, pass_admin)
    stranger = make_operational_user_with_membership(
        email="view.scope@example.test",
        group_name="Pass Administrators",
        event_edition=other_event,
        organization=organization,
    )
    sign_in_operational(client, stranger.email_normalized)
    response = client.post(
        reverse("badges:fallback-reference-lookup"),
        {"reference": credential.series.fallback_reference},
    )
    body = response.content.decode()
    assert eligible_registration.public_reference not in body
    assert "No entry pass matches that reference" in body


# ---------------------------------------------------------------------------
# 9. Terminal participant status
# ---------------------------------------------------------------------------


def test_a_revoked_pass_is_shown_as_revoked_not_as_missing(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    revoke_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    assert "no longer valid" in body
    assert "do not have an entry pass" not in body
    assert "data-pass-qr=" not in body


def test_an_expired_pass_is_shown_as_expired(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    credential.status = DigitalEntryPassStatus.EXPIRED
    credential.save(update_fields=["status"])

    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    assert "validity period has ended" in body
    assert "do not have an entry pass" not in body
    assert "data-pass-qr=" not in body


def test_a_replaced_pass_is_hidden_behind_its_replacement(
    client, eligible_registration, pass_admin, active_key, person
):
    from apps.badges.services import replace_pass

    credential = _activated(eligible_registration, pass_admin)
    replacement = replace_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_jti=credential.jti,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    ).credential

    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    # Exactly one card: the current replacement, not the replaced original.
    # Count the heading ids, not every mention -- each card references its
    # heading twice (the `id` and the section's `aria-labelledby`).
    assert body.count('id="pass-heading-') == 1
    assert "not yet active" in body
    assert replacement.status == DigitalEntryPassStatus.INACTIVE


def test_a_participant_with_no_pass_still_sees_the_empty_message(client, person):
    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()
    assert "do not have an entry pass" in body


def test_terminal_states_never_expose_a_qr_url(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    for status in (
        DigitalEntryPassStatus.REVOKED,
        DigitalEntryPassStatus.EXPIRED,
        DigitalEntryPassStatus.SUSPENDED,
        DigitalEntryPassStatus.INACTIVE,
    ):
        VerificationKey.objects.all()  # keep the key fixture meaningful
        credential.status = status
        credential.save(update_fields=["status"])
        _sign_in_participant(client, person)
        body = client.get(reverse("badges:participant-passes")).content.decode()
        assert "/qr.png" not in body, f"{status} must not expose a QR endpoint"


@pytest.mark.parametrize(
    ("status", "should_promise"),
    [
        (DigitalEntryPassStatus.INACTIVE, True),
        (DigitalEntryPassStatus.SUSPENDED, True),
        (DigitalEntryPassStatus.REVOKED, False),
        (DigitalEntryPassStatus.EXPIRED, False),
    ],
)
def test_only_a_recoverable_pass_promises_a_future_qr(
    client, eligible_registration, pass_admin, active_key, person, status, should_promise
):
    """A revoked or expired pass must not say a QR is still coming."""
    credential = _activated(eligible_registration, pass_admin)
    credential.status = status
    credential.save(update_fields=["status"])

    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    promise = "becomes available once your pass is active"
    assert (promise in body) is should_promise
