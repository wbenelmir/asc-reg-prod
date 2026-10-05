"""Device API contract, CSRF, headers, error envelope and grants
(Phase 4 Prompt 2, binding decision P2-A). PostgreSQL, Django test client.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.utils import timezone

from apps.entry import session_state
from apps.entry.models import OfflineOperatorGrant
from apps.entry.offline_contract import TYP_OPERATOR_GRANT
from apps.entry.services.offline_crypto import jws_verify
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db

OPENAPI = json.loads(
    (Path(django_settings.BASE_DIR) / "docs/api/entry_device_api_v1.openapi.json").read_text(
        encoding="utf-8"
    )
)
BASE = "/entry/api/v1"


def _client(device_secret, user=None) -> Client:
    client = Client(enforce_csrf_checks=True)
    client.cookies[django_settings.ENTRY_DEVICE_COOKIE_NAME] = device_secret
    if user is not None:
        from apps.accounts import session_expiry

        client.force_login(user)
        session = client.session
        now = timezone.now().isoformat()
        session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
        session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
        session.save()
    client.get("/entry/offline/")  # sets the CSRF cookie, like the shell does
    return client


def _post(client, path, payload, *, synthetic=None, purpose="", csrf=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {}
    if csrf:
        headers["HTTP_X_CSRFTOKEN"] = client.cookies[django_settings.CSRF_COOKIE_NAME].value
    if synthetic is not None:
        _b, nonce, signature = synthetic.signed(purpose, {}, nonce=None)
        from apps.entry.services.offline_devices import issue_nonce, proof_message

        nonce = issue_nonce(synthetic.device)
        signature = offline_factories.raw_sign(
            synthetic.sign_key, proof_message(purpose=purpose, nonce=nonce, body=body)
        )
        headers["HTTP_X_ASC_DEVICE_NONCE"] = nonce
        headers["HTTP_X_ASC_DEVICE_SIGNATURE"] = signature
    return client.post(BASE + path, data=body, content_type="application/json", **headers)


def _required(schema_ref):
    name = schema_ref.rsplit("/", 1)[-1]
    return set(OPENAPI["components"]["schemas"][name]["required"])


def _assert_contract(path, response):
    operation = OPENAPI["paths"][path]["post"]
    status = str(response.status_code)
    assert status in operation["responses"], (path, status)
    documented = operation["responses"][status]
    if "$ref" in documented:
        documented = OPENAPI["components"]["responses"][documented["$ref"].rsplit("/", 1)[-1]]
    content = documented.get("content")
    if content:
        schema_ref = content["application/json"]["schema"]["$ref"]
        assert _required(schema_ref) <= set(response.json())
    assert response["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# Boundary: CSRF, authentication, default deny, error envelope
# ---------------------------------------------------------------------------


def test_every_documented_path_is_routed_and_post_only(offline_device, device_and_secret):
    client = _client(device_and_secret[1])
    for path in OPENAPI["paths"]:
        assert client.get(BASE + path).status_code in (403, 405)


def test_csrf_is_enforced_even_without_a_signed_in_user(offline_device, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(client, "/offline/heartbeat/", {}, csrf=False)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_unknown_device_gets_the_structured_401(offline_device):
    client = _client("x" * 43)
    response = _post(client, "/offline/heartbeat/", {})
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "DEVICE_NOT_AUTHENTICATED"
    assert set(error) == {"code", "message", "correlation_id"}
    assert "Traceback" not in response.content.decode()
    _assert_contract("/offline/heartbeat/", response)


def test_heartbeat_contract_and_next_nonce(offline_device, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(client, "/offline/heartbeat/", {"report": None})
    assert response.status_code == 200
    _assert_contract("/offline/heartbeat/", response)
    assert response["X-ASC-Next-Nonce"]
    assert response.json()["nonce"] != response["X-ASC-Next-Nonce"]


def test_heartbeat_is_passive_for_the_operator_session(offline_device, device_and_secret):
    assert "/entry/api/v1/offline/heartbeat/" in django_settings.OPERATIONAL_PASSIVE_PATHS


def test_malformed_json_is_refused_safely(offline_device, device_and_secret):
    client = _client(device_and_secret[1])
    response = client.post(
        BASE + "/offline/heartbeat/",
        data=b"{not json",
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies[django_settings.CSRF_COOKIE_NAME].value,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


def test_the_drf_default_denies_undeclared_views():
    from apps.entry.api.permissions import DenyAll

    assert DenyAll().has_permission(None, None) is False
    assert django_settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == [
        "apps.entry.api.permissions.DenyAll"
    ]


# ---------------------------------------------------------------------------
# Package and delta over HTTP
# ---------------------------------------------------------------------------


def _package_when_ready(client, offline_device):
    """POST the package request; the first answer is 202 BUILDING (the
    request never builds). Run the build as the worker would, then ask
    again for the stored bytes."""
    building = _post(
        client,
        "/offline/package/",
        {"have_version": None},
        synthetic=offline_device,
        purpose="package",
    )
    assert building.status_code == 202
    _assert_contract("/offline/package/", building)
    assert building.json()["status"] == "BUILDING"
    assert int(building["Retry-After"]) == building.json()["retry_after_seconds"] > 0
    offline_factories.run_pending_builds()
    return _post(
        client,
        "/offline/package/",
        {"have_version": None},
        synthetic=offline_device,
        purpose="package",
    )


def test_package_over_http_is_byte_identical_and_contract_shaped(
    offline_device, device_and_secret, active_pass
):
    client = _client(device_and_secret[1])
    first = _package_when_ready(client, offline_device)
    second = _post(
        client,
        "/offline/package/",
        {"have_version": None},
        synthetic=offline_device,
        purpose="package",
    )
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    _assert_contract("/offline/package/", first)
    version = int(first["X-ASC-Package-Version"])
    third = _post(
        client,
        "/offline/package/",
        {"have_version": version},
        synthetic=offline_device,
        purpose="package",
    )
    assert third.status_code == 204


def test_package_without_a_proof_is_refused(offline_device, device_and_secret, active_pass):
    client = _client(device_and_secret[1])
    response = _post(client, "/offline/package/", {"have_version": None})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROOF_MISSING"
    _assert_contract("/offline/package/", response)


def test_delta_over_http(offline_device, device_and_secret, active_pass):
    client = _client(device_and_secret[1])
    package = _package_when_ready(client, offline_device)
    manifest, _body = offline_device.open_response(package.content, typ="ASC-OPKG", kind="OPKG")
    delta = _post(
        client,
        "/offline/delta/",
        {"package_id": manifest["package_id"]},
        synthetic=offline_device,
        purpose="delta",
    )
    assert delta.status_code == 200
    _assert_contract("/offline/delta/", delta)


def test_disabled_offline_is_a_structured_409(offline_device, device_and_secret, settings):
    settings.ENTRY_OFFLINE_ENABLED = False
    client = _client(device_and_secret[1])
    response = _post(
        client,
        "/offline/package/",
        {"have_version": None},
        synthetic=offline_device,
        purpose="package",
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OFFLINE_DISABLED"
    _assert_contract("/offline/package/", response)


def test_provision_requires_a_signed_in_user(offline_device, device_and_secret):
    client = _client(device_and_secret[1])
    response = _post(client, "/offline/provision/", {"signing_key": "a", "unwrap_key": "b"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_SIGNED_IN"
    _assert_contract("/offline/provision/", response)


def test_provision_over_http(
    offline_on, package_signer, event, layout, device_and_secret, device_admin
):
    device, secret = device_and_secret
    offline_factories.enable_event(event, device_admin)
    offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    synthetic = offline_factories.SyntheticDevice(device=device)
    client = _client(secret, user=device_admin)
    payload = {"signing_key": synthetic.sign_spki, "unwrap_key": synthetic.unwrap_spki}
    response = _post(
        client, "/offline/provision/", payload, synthetic=synthetic, purpose="provision"
    )
    assert response.status_code == 201, response.content
    _assert_contract("/offline/provision/", response)
    assert {a["kid"] for a in response.json()["trust_anchors"]} == {"p1", "p2"}


def test_self_test_over_http(offline_device, device_and_secret, device_admin, active_pass):
    client = _client(device_and_secret[1], user=device_admin)
    package = _package_when_ready(client, offline_device)
    manifest, _ = offline_device.open_response(package.content, typ="ASC-OPKG", kind="OPKG")
    payload = {
        "package_id": manifest["package_id"],
        "checks": offline_factories.all_checks_passed(),
    }
    response = _post(
        client, "/offline/self-test/", payload, synthetic=offline_device, purpose="self-test"
    )
    assert response.status_code == 200
    _assert_contract("/offline/self-test/", response)
    assert response.json() == {"ready": True}


# ---------------------------------------------------------------------------
# Operator grants (online only; bounded by P2-B)
# ---------------------------------------------------------------------------


def _ready_with_checkpoint(offline_device, device_admin, user, layout):
    package, _ = offline_factories.download(offline_device)
    offline_factories.self_test(offline_device, admin=device_admin, package=package)
    return factories.open_checkpoint(device=offline_device.device, user=user, zone=layout.main)


def _grant_client(secret, user, checkpoint):
    client = _client(secret, user=user)
    session = client.session
    session[session_state.DEVICE_SESSION_KEY] = str(checkpoint.device_session.pk)
    session[session_state.OPERATOR_SESSION_KEY] = str(checkpoint.operator_session.pk)
    session.save()
    return client


def test_grant_is_bounded_by_session_and_contact_and_verifiable(
    offline_device, device_and_secret, device_admin, supervisor, layout, active_pass
):
    checkpoint = _ready_with_checkpoint(offline_device, device_admin, supervisor, layout)
    client = _grant_client(device_and_secret[1], supervisor, checkpoint)
    response = _post(client, "/offline/grant/", {}, synthetic=offline_device, purpose="grant")
    assert response.status_code == 201, response.content
    _assert_contract("/offline/grant/", response)
    payload = jws_verify(
        response.json()["grant"],
        trusted_keys_der=offline_device.trusted_keys_der(),
        typ=TYP_OPERATOR_GRANT,
    )
    grant = OfflineOperatorGrant.objects.get(public_id=payload["grant_id"])
    assert grant.expires_at <= checkpoint.operator_session.expires_at
    assert grant.expires_at <= grant.issued_at + timedelta(hours=2)
    assert "OVERRIDE_OFFLINE" in payload["permissions"]  # supervisors hold override_entry


def test_sensitive_grant_is_at_most_one_hour(
    offline_on,
    package_signer,
    event,
    layout,
    device_and_secret,
    device_admin,
    supervisor,
    key,
    active_pass,
):
    device, secret = device_and_secret
    offline_factories.enable_event(event, device_admin)
    offline_factories.make_offline_capable(
        device, admin=device_admin, layout=layout, sensitivity="SENSITIVE"
    )
    synthetic = offline_factories.SyntheticDevice(device=device)
    offline_factories.provision(synthetic, admin=device_admin)
    checkpoint = _ready_with_checkpoint(synthetic, device_admin, supervisor, layout)
    client = _grant_client(secret, supervisor, checkpoint)
    response = _post(client, "/offline/grant/", {}, synthetic=synthetic, purpose="grant")
    grant = OfflineOperatorGrant.objects.get(public_id=response.json()["grant_id"])
    assert grant.expires_at <= grant.issued_at + timedelta(hours=1)


def test_grant_requires_an_offline_ready_device(
    offline_device, device_and_secret, supervisor, layout, active_pass
):
    checkpoint = factories.open_checkpoint(
        device=offline_device.device, user=supervisor, zone=layout.main
    )
    client = _grant_client(device_and_secret[1], supervisor, checkpoint)
    response = _post(client, "/offline/grant/", {}, synthetic=offline_device, purpose="grant")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEVICE_NOT_READY"


def test_grant_is_revoked_when_the_operator_session_ends(
    offline_device, device_and_secret, device_admin, supervisor, layout, active_pass
):
    from apps.entry.services.sessions import end_operator_session

    checkpoint = _ready_with_checkpoint(offline_device, device_admin, supervisor, layout)
    client = _grant_client(device_and_secret[1], supervisor, checkpoint)
    response = _post(client, "/offline/grant/", {}, synthetic=offline_device, purpose="grant")
    grant_id = response.json()["grant_id"]
    end_operator_session(operator_session=checkpoint.operator_session, reason="SIGNED_OUT")
    grant = OfflineOperatorGrant.objects.get(public_id=grant_id)
    assert grant.revoked_at is not None
    beat = _post(client, "/offline/heartbeat/", {})
    directives = {d["type"]: d for d in beat.json()["directives"]}
    assert grant_id in directives["REVOKE_GRANTS"]["grants"]


def test_grant_rows_are_immutable_except_revocation(
    offline_device, device_and_secret, device_admin, supervisor, layout, active_pass
):
    from django.db import transaction
    from django.db.utils import DatabaseError

    checkpoint = _ready_with_checkpoint(offline_device, device_admin, supervisor, layout)
    client = _grant_client(device_and_secret[1], supervisor, checkpoint)
    grant_id = _post(
        client, "/offline/grant/", {}, synthetic=offline_device, purpose="grant"
    ).json()["grant_id"]
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflineOperatorGrant.objects.filter(public_id=grant_id).update(
            expires_at=timezone.now() + timedelta(days=1)
        )
