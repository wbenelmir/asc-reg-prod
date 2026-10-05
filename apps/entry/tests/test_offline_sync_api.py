"""The synchronization boundary over HTTP (Phase 4 Prompt 3): the documented
OpenAPI contract, CSRF, device authentication, the batch proof, and the
quarantine boundary. PostgreSQL, Django test client; synthetic data."""

from __future__ import annotations

import json

import pytest
from django.utils import timezone

from apps.entry.models import EntryEvent, SyncOperation
from apps.entry.tests import offline_factories
from apps.entry.tests.test_offline_api import OPENAPI, _assert_contract, _client, _post

pytestmark = pytest.mark.django_db


def _batch(world):
    op = world.store.next(
        offline_factories.base_operation(
            device=world.device,
            package=world.package,
            grant=world.grant,
            credential=world.credential,
            occurred_at=timezone.now(),
        )
    )
    return {"store": world.store.store_id, "operations": [world.store.envelope(op)]}


def test_the_sync_paths_are_documented(offline_world):
    assert "/offline/sync/" in OPENAPI["paths"]
    assert "/offline/sync/quarantine/" in OPENAPI["paths"]
    ack = OPENAPI["components"]["schemas"]["Acknowledgement"]
    assert set(ack["properties"]["status"]["enum"]) >= {"APPLIED", "PENDING", "NOT_PROCESSED"}


def test_a_signed_upload_over_http_is_applied_and_contract_shaped(offline_world, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(
        client,
        "/offline/sync/",
        _batch(offline_world),
        synthetic=offline_world.synthetic,
        purpose="sync",
    )
    assert response.status_code == 200
    _assert_contract("/offline/sync/", response)
    ack = response.json()["acknowledgements"][0]
    assert ack["status"] == "APPLIED" and ack["durable"] and ack["ack"].count(".") == 2
    assert set(ack) == set(OPENAPI["components"]["schemas"]["Acknowledgement"]["required"])
    assert response["X-ASC-Next-Nonce"]
    assert EntryEvent.objects.count() == 1


def test_an_upload_without_a_proof_is_refused_and_stores_nothing(offline_world, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(client, "/offline/sync/", _batch(offline_world))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROOF_MISSING"
    _assert_contract("/offline/sync/", response)
    assert not SyncOperation.objects.exists()


def test_an_upload_without_csrf_is_refused(offline_world, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(
        client,
        "/offline/sync/",
        _batch(offline_world),
        synthetic=offline_world.synthetic,
        purpose="sync",
        csrf=False,
    )
    assert response.status_code == 403
    assert not SyncOperation.objects.exists()


def test_an_unknown_device_gets_the_structured_401(offline_world):
    client = _client("y" * 43)
    response = _post(client, "/offline/sync/", _batch(offline_world))
    assert response.status_code == 401
    _assert_contract("/offline/sync/", response)


def test_a_proof_for_another_purpose_is_refused(offline_world, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(
        client,
        "/offline/sync/",
        _batch(offline_world),
        synthetic=offline_world.synthetic,
        purpose="package",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROOF_INVALID"


def test_the_quarantine_boundary_refuses_an_operational_device(offline_world, device_and_secret):
    client = _client(device_and_secret[1])
    payload = {**_batch(offline_world), "device": offline_world.device.public_id}
    response = client.post(
        "/entry/api/v1/offline/sync/quarantine/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "QUARANTINE_REFUSED"
    _assert_contract("/offline/sync/quarantine/", response)
    assert not SyncOperation.objects.exists()


def test_the_quarantine_boundary_accepts_a_revoked_devices_signed_evidence(
    offline_world, device_and_secret, device_admin
):
    from apps.entry.services.devices import revoke_device

    payload = {**_batch(offline_world), "device": offline_world.device.public_id}
    device = offline_world.device
    device.refresh_from_db()
    revoke_device(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    client = _client("z" * 43)  # the revoked device has no usable credential
    response = client.post(
        "/entry/api/v1/offline/sync/quarantine/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 200
    _assert_contract("/offline/sync/quarantine/", response)
    assert response.json()["acknowledgements"][0]["status"] == "QUARANTINED"
    assert not EntryEvent.objects.exists()


def test_unauthenticated_quarantine_requests_over_http_leave_bounded_evidence(
    offline_world, device_admin
):
    """Correction 4 (R-02): a burst naming a revoked device's public id, with
    no valid operation signature, stores nothing, spends no upload budget and
    leaves one audit event per refusal reason -- not one per request."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from apps.audit import action_codes
    from apps.audit.models import AuditEvent
    from apps.entry.services.devices import revoke_device

    device = offline_world.device
    device.refresh_from_db()
    revoke_device(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    genuine = _batch(offline_world)
    forged_op = offline_world.store.next(
        offline_factories.base_operation(
            device=offline_world.device,
            package=offline_world.package,
            grant=offline_world.grant,
            credential=offline_world.credential,
            occurred_at=timezone.now(),
        )
    )
    forged = offline_world.store.envelope(forged_op, key=ec.generate_private_key(ec.SECP256R1()))
    client = _client("z" * 43)  # no usable device credential
    token = client.cookies["csrftoken"].value
    bursts = {
        "MALFORMED_BATCH": {"device": device.public_id, "store": "x", "operations": []},
        "QUARANTINE_REFUSED": {
            "device": device.public_id,
            "store": offline_world.store.store_id,
            "operations": [forged],
        },
    }
    for _ in range(15):
        for code, payload in bursts.items():
            response = client.post(
                "/entry/api/v1/offline/sync/quarantine/",
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == code
            _assert_contract("/offline/sync/quarantine/", response)
    refusals = AuditEvent.objects.filter(
        target_uuid=device.pk, action_code=action_codes.OFFLINE_SYNC_REFUSED
    )
    assert sorted(refusals.values_list("reason_code", flat=True)) == [
        "INVALID_SIGNATURE",
        "MALFORMED_BATCH",
    ]
    assert not SyncOperation.objects.exists()
    response = client.post(
        "/entry/api/v1/offline/sync/quarantine/",
        data=json.dumps({**genuine, "device": device.public_id}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200
    assert response.json()["acknowledgements"][0]["status"] == "QUARANTINED"
