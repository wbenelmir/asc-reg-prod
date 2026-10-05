"""P4-4, gap G-04: device-proof nonces under real concurrency, through HTTP.

The Prompt 3 concurrency tests call the intake below the HTTP layer. Here each
thread has its own Django test client and its own PostgreSQL connection and
posts to `/entry/api/v1/offline/sync/`, so CSRF, device authentication, the
signed proof, the one-time nonce (a unique `OfflineRequestNonce` row) and the
idempotent operation store are all exercised together. Synthetic data only.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.entry.models import EntryEvent, OfflineRequestNonce, SyncOperation
from apps.entry.services.offline_devices import issue_nonce, proof_message
from apps.entry.tests import offline_factories
from apps.entry.tests.test_offline_api import BASE, _client

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

SYNC = BASE + "/offline/sync/"


def _body(world) -> bytes:
    op = world.store.next(
        offline_factories.base_operation(
            device=world.device,
            package=world.package,
            grant=world.grant,
            credential=world.credential,
            occurred_at=timezone.now(),
        )
    )
    payload = {"store": world.store.store_id, "operations": [world.store.envelope(op)]}
    return json.dumps(payload).encode("utf-8")


def _signed_headers(world, body: bytes, nonce: str) -> dict:
    signature = offline_factories.raw_sign(
        world.synthetic.sign_key, proof_message(purpose="sync", nonce=nonce, body=body)
    )
    return {"HTTP_X_ASC_DEVICE_NONCE": nonce, "HTTP_X_ASC_DEVICE_SIGNATURE": signature}


def _race(clients, body: bytes, headers_list: list[dict]):
    barrier = threading.Barrier(len(clients))

    def post(index):
        try:
            client = clients[index]
            headers = {
                "HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value,
                **headers_list[index],
            }
            barrier.wait(timeout=15)
            return client.post(SYNC, data=body, content_type="application/json", **headers)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = [pool.submit(post, index) for index in range(len(clients))]
        return [future.result(timeout=60) for future in futures]


@pytest.mark.parametrize("parallel", [2, 8])
def test_one_signed_request_replayed_concurrently_is_accepted_exactly_once(
    offline_world, device_and_secret, parallel
):
    body = _body(offline_world)
    nonce = issue_nonce(offline_world.device)
    headers = _signed_headers(offline_world, body, nonce)
    clients = [_client(device_and_secret[1]) for _ in range(parallel)]
    responses = _race(clients, body, [headers] * parallel)

    accepted = [r for r in responses if r.status_code == 200]
    refused = [r for r in responses if r.status_code != 200]
    assert len(accepted) == 1
    assert accepted[0].json()["acknowledgements"][0]["status"] == "APPLIED"
    assert {r.status_code for r in refused} <= {400, 409}
    assert all(r.json()["error"]["code"] == "NONCE_REPLAYED" for r in refused)
    assert OfflineRequestNonce.objects.filter(device=offline_world.device).count() >= 1
    assert SyncOperation.objects.count() == 1
    assert EntryEvent.objects.count() == 1


def test_one_operation_sent_concurrently_with_fresh_nonces_is_applied_once(
    offline_world, device_and_secret
):
    parallel = 4
    body = _body(offline_world)
    headers_list = [
        _signed_headers(offline_world, body, issue_nonce(offline_world.device))
        for _ in range(parallel)
    ]
    clients = [_client(device_and_secret[1]) for _ in range(parallel)]
    responses = _race(clients, body, headers_list)

    assert [r.status_code for r in responses] == [200] * parallel
    statuses = [r.json()["acknowledgements"][0] for r in responses]
    assert all(ack["durable"] and ack["status"] == "APPLIED" for ack in statuses)
    assert SyncOperation.objects.count() == 1
    assert EntryEvent.objects.count() == 1
    assert SyncOperation.objects.get().duplicate_submissions == parallel - 1
