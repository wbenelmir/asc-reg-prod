"""Protected document streaming endpoint tests (Prompt 5 correction pass §3)."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import participant_auth, session_expiry
from apps.accounts.models import (
    OperationalUser,
    OperationalUserStatus,
    ScopedGroupMembership,
    ScopedGroupMembershipStatus,
)
from apps.accounts.tests.sign_in import staff_sign_in
from apps.audit.models import AuditEvent
from apps.documents.models import Document, DocumentStatus
from apps.documents.services import save_profile_photo
from apps.documents.tests.factories import make_test_photo
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.registrations.models import Registration, RegistrationSourceKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def event() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="DOCVIEW", name="Doc View Test", timezone="UTC", starts_at=now, ends_at=now
    )


@pytest.fixture
def other_event() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="DOCVIEW2", name="Doc View Test 2", timezone="UTC", starts_at=now, ends_at=now
    )


@pytest.fixture
def person() -> Person:
    return Person.objects.create(display_name="Owner")


@pytest.fixture
def other_person() -> Person:
    return Person.objects.create(display_name="Someone Else")


def _make_registration(event: EventEdition, person: Person, *, reference: str) -> Registration:
    return Registration.objects.create(
        public_reference=reference,
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key="open",
    )


@pytest.fixture
def registration(event: EventEdition, person: Person) -> Registration:
    return _make_registration(event, person, reference="DOCVIEW-R-000001")


@pytest.fixture
def active_document(registration: Registration, person: Person) -> Document:
    return save_profile_photo(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )


def _login_participant(client: Client, person: Person) -> None:
    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()


def _stream_url(document: Document) -> str:
    return reverse("documents:document-stream", kwargs={"document_id": document.pk})


def test_owning_participant_can_stream_their_own_active_document(
    client: Client, registration: Registration, active_document: Document, person: Person
) -> None:
    _login_participant(client, person)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"


def test_response_headers_are_safe(
    client: Client, active_document: Document, person: Person
) -> None:
    _login_participant(client, person)
    response = client.get(_stream_url(active_document))
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"
    assert response["Content-Disposition"].startswith("attachment;")
    assert "document." in response["Content-Disposition"]


def test_other_participant_cannot_access_someone_elses_document(
    client: Client, active_document: Document, other_person: Person
) -> None:
    _login_participant(client, other_person)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_anonymous_request_is_denied_generically(client: Client, active_document: Document) -> None:
    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_nonexistent_document_id_returns_generic_404(client: Client, person: Person) -> None:
    _login_participant(client, person)
    response = client.get(
        reverse("documents:document-stream", kwargs={"document_id": uuid.uuid4()})
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "status", [DocumentStatus.REPLACED, DocumentStatus.REJECTED, DocumentStatus.PURGED]
)
def test_non_active_documents_are_denied_for_the_owning_participant(
    client: Client, active_document: Document, person: Person, status: str
) -> None:
    Document.objects.filter(pk=active_document.pk).update(status=status)
    _login_participant(client, person)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_missing_storage_object_is_a_generic_404_not_a_500(
    client: Client, active_document: Document, person: Person
) -> None:
    from apps.documents.storage import get_private_storage

    get_private_storage().delete(active_document.stored_object.storage_key)
    _login_participant(client, person)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_successful_access_records_a_bounded_audit_event(
    client: Client, active_document: Document, person: Person
) -> None:
    _login_participant(client, person)
    before = AuditEvent.objects.count()
    client.get(_stream_url(active_document))
    events = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED")
    assert events.count() == before + 1
    event = events.latest("id")
    assert event.target_uuid == active_document.pk
    assert event.actor_person_id == person.id
    assert event.before_summary is None
    assert event.after_summary is None


def test_denied_access_never_records_a_success_audit_event(
    client: Client, active_document: Document, other_person: Person
) -> None:
    _login_participant(client, other_person)
    before = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").count()
    client.get(_stream_url(active_document))
    assert AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").count() == before


# ---------------------------------------------------------------------------
# Operational authorization: scope, wrong-scope, and permission/scope split.
# ---------------------------------------------------------------------------


def _make_operational_user(email: str) -> OperationalUser:
    user = OperationalUser.objects.create_user(
        email=email,
        password="__test_password__",  # noqa: S106
    )
    user.status = OperationalUserStatus.ACTIVE
    user.save(update_fields=["status"])
    return user


def _grant_view_registration_permission(group: Group) -> None:
    permission = Permission.objects.get(
        content_type__app_label="registrations", codename="view_registration"
    )
    group.permissions.add(permission)


def _sign_in_operational(client: Client, email: str) -> None:
    """Real sign-in POST, not `client.force_login()` -- `force_login()` bypasses
    the view entirely, so it never establishes the operational session-expiry
    timestamps `OperationalSessionExpiryMiddleware` fails closed without
    (Prompt 5 correction pass §4)."""
    response = staff_sign_in(client, email, "__test_password__")
    assert response.status_code == 302


def test_operational_user_with_scope_and_permission_can_stream(
    client: Client, event: EventEdition, active_document: Document, registration: Registration
) -> None:
    user = _make_operational_user("ops-authorized@example.com")
    group = Group.objects.create(name="doc-view-authorized")
    _grant_view_registration_permission(group)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event,
        status=ScopedGroupMembershipStatus.ACTIVE,
        granted_by=user,
    )
    _sign_in_operational(client, user.email_normalized)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 200


def test_operational_user_outside_scope_is_denied(
    client: Client, other_event: EventEdition, active_document: Document
) -> None:
    user = _make_operational_user("ops-wrong-scope@example.com")
    group = Group.objects.create(name="doc-view-wrong-scope")
    _grant_view_registration_permission(group)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=other_event,
        status=ScopedGroupMembershipStatus.ACTIVE,
        granted_by=user,
    )
    _sign_in_operational(client, user.email_normalized)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_operational_permission_from_one_group_never_combines_with_scope_from_another(
    client: Client, event: EventEdition, active_document: Document
) -> None:
    user = _make_operational_user("ops-split@example.com")
    permission_group = Group.objects.create(name="doc-view-permission-only")
    _grant_view_registration_permission(permission_group)
    user.groups.add(permission_group)

    scope_only_group = Group.objects.create(name="doc-view-scope-only")
    ScopedGroupMembership.objects.create(
        user=user,
        group=scope_only_group,
        event_edition=event,
        status=ScopedGroupMembershipStatus.ACTIVE,
        granted_by=user,
    )
    _sign_in_operational(client, user.email_normalized)
    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Prompt 5 correction pass §1: an expired or malformed participant session
# must never authorize document access, exactly like every other denial.
# ---------------------------------------------------------------------------


def test_expired_owning_participant_is_denied_generically(
    client: Client, active_document: Document, person: Person
) -> None:
    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = stale
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = stale
    session.save()

    with override_settings(PARTICIPANT_SESSION_INACTIVITY_SECONDS=60):
        response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_malformed_owning_participant_timestamps_are_denied_generically(
    client: Client, active_document: Document, person: Person
) -> None:
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = "garbage"
    session.save()

    response = client.get(_stream_url(active_document))
    assert response.status_code == 404


def test_expired_owning_participant_never_records_a_success_audit_event(
    client: Client, active_document: Document, person: Person
) -> None:
    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = stale
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = stale
    session.save()

    before = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").count()
    with override_settings(PARTICIPANT_SESSION_INACTIVITY_SECONDS=60):
        client.get(_stream_url(active_document))
    assert AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").count() == before


# ---------------------------------------------------------------------------
# Prompt 5 correction pass §3: authorization and audit-actor attribution
# are evaluated INDEPENDENTLY per audience -- a participant session key
# merely being present in the browser session must never cause an
# operationally authorized access to be misattributed to that participant,
# and vice versa.
# ---------------------------------------------------------------------------


def test_operational_access_is_correctly_attributed_despite_an_unrelated_participant_session(
    client: Client,
    event: EventEdition,
    active_document: Document,
    registration: Registration,
    other_person: Person,
) -> None:
    """An operational user, valid and authorized, shares the browser
    session with an unrelated (non-owning) participant session. Access
    must be granted through the OPERATIONAL path and attributed to the
    OPERATIONAL_USER -- never silently attributed to the participant
    merely because a participant session key happens to be present."""
    user = _make_operational_user("both-actor-ops@example.com")
    group = Group.objects.create(name="doc-view-both-actor")
    _grant_view_registration_permission(group)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event,
        status=ScopedGroupMembershipStatus.ACTIVE,
        granted_by=user,
    )
    _sign_in_operational(client, user.email_normalized)
    # An unrelated participant (not the document owner) also has a session
    # key present in the SAME browser session.
    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(other_person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()

    response = client.get(_stream_url(active_document))
    assert response.status_code == 200

    event_row = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").latest("id")
    assert event_row.actor_type == "OPERATIONAL_USER"
    assert event_row.actor_user_id == user.id
    assert event_row.actor_person_id is None


def test_participant_access_is_correctly_attributed_despite_an_unauthorized_operational_user(
    client: Client, active_document: Document, person: Person
) -> None:
    """The owning participant is valid; an operational user is ALSO signed
    in on the same browser session but is not authorized for this
    Registration. Access must be granted through the PARTICIPANT path and
    attributed to PARTICIPANT."""
    user = _make_operational_user("both-actor-ops-2@example.com")
    _sign_in_operational(client, user.email_normalized)  # no scope/permission granted
    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()

    response = client.get(_stream_url(active_document))
    assert response.status_code == 200

    event_row = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").latest("id")
    assert event_row.actor_type == "PARTICIPANT"
    assert event_row.actor_person_id == person.id
    assert event_row.actor_user_id is None


def test_participant_authorization_is_resolved_once_and_carried_unchanged_into_the_audit(
    client: Client, active_document: Document, person: Person, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the participant audit identity race: the view must resolve the
    authorized participant person id ONCE during authorization and thread
    it into the audit record unchanged -- never re-evaluate participant
    session validity a second time after `storage.open()`. A session
    expiring mid-request must never turn an already-authorized participant
    audit event into `actor_person_id=None`.

    Proved by making a hypothetical SECOND call to
    `get_valid_participant_person_id` return a different (expired) result:
    the recorded audit is asserted unaffected, and the call count is
    asserted to be exactly one, so a regression that re-checks validity
    after authorization is caught even before it could corrupt an audit
    row.
    """
    _login_participant(client, person)

    real = participant_auth.get_valid_participant_person_id
    calls: list[None] = []

    def _once_then_expired(request):
        calls.append(None)
        if len(calls) == 1:
            return real(request)
        # A second call would represent the session expiring mid-request --
        # this result must never reach the audit record.
        return None

    monkeypatch.setattr(participant_auth, "get_valid_participant_person_id", _once_then_expired)

    response = client.get(_stream_url(active_document))
    assert response.status_code == 200
    assert len(calls) == 1

    event_row = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").latest("id")
    assert event_row.actor_type == "PARTICIPANT"
    assert event_row.actor_person_id == person.id
    assert event_row.actor_user_id is None


def test_operational_actor_is_preferred_when_both_are_valid_and_authorized(
    client: Client,
    event: EventEdition,
    active_document: Document,
    registration: Registration,
    person: Person,
) -> None:
    """When the SAME browser session is both the owning participant AND a
    validly authorized operational user, the operational actor is
    preferred (Prompt 5 correction pass §3: "Prefer an authorized
    operational actor when the operational identity is valid and its
    scoped permission grants access")."""
    user = _make_operational_user("both-actor-ops-3@example.com")
    group = Group.objects.create(name="doc-view-both-valid")
    _grant_view_registration_permission(group)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event,
        status=ScopedGroupMembershipStatus.ACTIVE,
        granted_by=user,
    )
    _sign_in_operational(client, user.email_normalized)
    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()

    response = client.get(_stream_url(active_document))
    assert response.status_code == 200

    event_row = AuditEvent.objects.filter(action_code="DOCUMENT_STREAMED").latest("id")
    assert event_row.actor_type == "OPERATIONAL_USER"
    assert event_row.actor_user_id == user.id
