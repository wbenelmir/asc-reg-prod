"""UI/UX Completion Gate F8: the export form chooses the event and the
organization from the caller's export scope, never a typed database id.

* Only events and organizations of registrations in the caller's export
  scope are offered.
* A submitted event or organization outside that scope, unknown or
  malformed, is answered exactly like a denied scope (409) and creates
  nothing, so ids cannot be enumerated.
* Purposes are shown by translated label; the stored code is unchanged.
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.exports.models import ExportRequest

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db


@pytest.fixture
def coordinator(event, organization):
    make_registration(event=event, organization=organization)
    return make_operational_user_with_membership(
        email="f8-export@example.test",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )


def _client(user) -> Client:
    client = Client()
    sign_in_operational(client, user.email_normalized)
    return client


def _post(client, **overrides):
    data = {"purpose_code": "OPERATIONAL_REPORTING", "reason": "F8 scope check"}
    data.update(overrides)
    return client.post(reverse("exports:workspace"), data)


def test_form_offers_only_scoped_events_and_organizations(
    coordinator, event, other_event, organization, other_organization
):
    make_registration(event=other_event, organization=other_organization)
    content = _client(coordinator).get(reverse("exports:workspace")).content.decode()
    assert f'value="{event.pk}"' in content
    assert f'value="{organization.pk}"' in content
    assert f'value="{other_event.pk}"' not in content
    assert f'value="{other_organization.pk}"' not in content
    assert other_organization.official_name not in content
    assert 'type="text" name="organization_id"' not in content


def test_purposes_are_labelled_not_raw_codes(coordinator):
    from django.conf import settings

    client = _client(coordinator)
    content = client.get(reverse("exports:workspace")).content.decode()
    assert 'value="OPERATIONAL_REPORTING">OPERATIONAL_REPORTING<' not in content
    assert 'value="OPERATIONAL_REPORTING">Operational reporting<' in content
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    content = client.get(reverse("exports:workspace")).content.decode()
    assert 'value="OPERATIONAL_REPORTING">Rapports opérationnels<' in content


@pytest.mark.parametrize("field", ["event_edition_id", "organization_id"])
@pytest.mark.parametrize("value_kind", ["out_of_scope", "unknown", "malformed"])
def test_out_of_scope_or_forged_values_are_a_scope_failure(
    field, value_kind, coordinator, event, organization, other_event, other_organization
):
    make_registration(event=other_event, organization=other_organization)
    out_of_scope = other_event.pk if field == "event_edition_id" else other_organization.pk
    value = {
        "out_of_scope": str(out_of_scope),
        "unknown": str(uuid.uuid4()),
        "malformed": "'; DROP TABLE x; --",
    }[value_kind]
    data = {"event_edition_id": str(event.pk), "organization_id": str(organization.pk)}
    data[field] = value
    response = _post(_client(coordinator), **data)
    assert response.status_code == 409
    assert ExportRequest.objects.count() == 0


def test_missing_reason_is_a_validation_error_not_a_scope_failure(coordinator, event):
    response = _post(_client(coordinator), event_edition_id=str(event.pk), reason="")
    assert response.status_code == 200
    assert response.context["form"].errors["reason"]
    assert ExportRequest.objects.count() == 0


def test_every_organization_choice_still_exports_only_the_scope(coordinator, event):
    response = _post(_client(coordinator), event_edition_id=str(event.pk), organization_id="")
    assert response.status_code == 302
    assert ExportRequest.objects.filter(event_edition=event).count() == 1
