"""Real PostgreSQL connections for sync replay, reconnect storms and revocation.

The intake/application boundary is called directly here; nonce/CSRF integration
is covered by test_offline_sync_api and the existing device-proof concurrency
suite. No process-local replacement for a database lock is used.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.entry.models import EntryDevice, EntryEvent, SyncOperation
from apps.entry.services import offline_sync
from apps.entry.tests.test_offline_sync import _admit
from tests.concurrency.test_offline_concurrency import _wait_for_lock_waiter

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


@pytest.mark.parametrize("clients", [2, 8])
def test_concurrent_duplicate_intake_and_application(offline_world, clients):
    op = _admit(offline_world)
    envelope = offline_world.store.envelope(op)
    barrier = threading.Barrier(clients)

    def upload():
        try:
            device = EntryDevice.objects.get(pk=offline_world.device.pk)
            barrier.wait(timeout=15)
            return offline_sync._handle(
                device,
                offline_world.store.store_id,
                envelope,
                quarantined=False,
                now=timezone.now(),
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(upload) for _ in range(clients)]
        results = [future.result(timeout=45) for future in futures]
    assert all(result.durable and result.status == "APPLIED" for result in results)
    assert SyncOperation.objects.count() == 1
    assert EntryEvent.objects.count() == 1
    row = SyncOperation.objects.get()
    assert row.duplicate_submissions == clients - 1


def test_sync_waiting_for_device_lock_observes_committed_suspension(offline_world):
    envelope = offline_world.store.envelope(_admit(offline_world))

    def upload():
        try:
            stale_device = EntryDevice.objects.get(pk=offline_world.device.pk)
            return offline_sync._handle(
                stale_device,
                offline_world.store.store_id,
                envelope,
                quarantined=False,
                now=timezone.now(),
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            device = EntryDevice.objects.select_for_update().get(pk=offline_world.device.pk)
            future = pool.submit(upload)
            assert _wait_for_lock_waiter(), "The second PostgreSQL connection must wait"
            device.status = "SUSPENDED"
            device.save(update_fields=["status"])
        ack = future.result(timeout=30)
    assert ack.durable and ack.status == "QUARANTINED"
    assert not EntryEvent.objects.exists()


def test_processing_waiting_for_device_lock_observes_revocation(offline_world):
    envelope = offline_world.store.envelope(_admit(offline_world))
    intake = offline_sync._intake(
        offline_world.device,
        offline_world.store.store_id,
        envelope,
        quarantined=False,
        now=timezone.now(),
    )

    def process():
        try:
            return offline_sync.process_operation(intake.row_id)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            device = EntryDevice.objects.select_for_update().get(pk=offline_world.device.pk)
            future = pool.submit(process)
            assert _wait_for_lock_waiter()
            device.status = "REVOKED"
            device.save(update_fields=["status"])
        ack = future.result(timeout=30)
    assert ack.durable and ack.status == "QUARANTINED"
    assert not EntryEvent.objects.exists()
