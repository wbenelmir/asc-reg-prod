"""Real multi-connection PostgreSQL concurrency for Offline Package builds
and critical deltas (Phase 4 Prompt 2, independent re-review corrections B and C).

Every test runs threads on their OWN PostgreSQL connections (the pattern of
`test_entry_concurrency.py`) with real commits (`transaction=True`):

B. concurrent package requests converge on ONE build and ONE reserved
   version; concurrent or redelivered build workers produce exactly one
   package; a REAL commit failure (a deferred foreign key checked at COMMIT)
   leaves no stored ciphertext behind; a build overtaken by a re-scope
   while it waits for the device lock is never committed;
C. a delta request that waits on the device row lock behind a concurrent
   block, re-scope or re-provisioning re-validates after the lock and is
   refused; and a change committed by a transaction that was still open
   when the package snapshot was taken -- with a timestamp OLDER than the
   package cutoff -- still reaches the next delta (commit order, not
   timestamps).

All data is synthetic.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.core.crypto.package_signing import (
    InMemoryPackageSigningKeyProvider,
    set_package_signing_key_provider_for_testing,
)
from apps.core.crypto.signing import (
    InMemorySigningKeyProvider,
    set_signing_key_provider_for_testing,
)
from apps.entry.models import (
    EntryDevice,
    OfflinePackage,
    OfflinePackageBuild,
    OfflinePackageBuildStatus,
)
from apps.entry.offline_contract import TYP_DELTA_MANIFEST
from apps.entry.services import offline_packages
from apps.entry.services.offline_devices import OfflineUnavailable
from apps.entry.tests import factories, offline_factories

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_in_threads(callables, *, timeout=60):
    barrier = threading.Barrier(len(callables))
    results: list = [None] * len(callables)
    errors: list[BaseException] = []

    def worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(callables))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    return results, errors


def _wait_for_lock_waiter(timeout=10.0) -> bool:
    """True once some other backend waits on a row lock. Usually called
    inside an open transaction, where PostgreSQL caches `pg_stat_activity`:
    the snapshot is cleared before every read."""
    deadline = time.monotonic() + timeout
    with connections["default"].cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


def _stored_files(root):
    return sorted(path.name for path in root.rglob("*") if path.is_file())


@pytest.fixture
def world(settings, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    settings.ENTRY_OFFLINE_ENABLED = True
    settings.PRIVATE_STORAGE_ROOT = root
    qr = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(qr)
    signer = InMemoryPackageSigningKeyProvider(key_ids=("p1",), current="p1")
    set_package_signing_key_provider_for_testing(signer)
    factories.seed_reference_values()
    event = factories.make_event("OFFCONC")
    layout = factories.VenueLayout(event)
    setup = factories.AccreditationSetup(event, layout)
    staff = factories.make_user("offline.staff@example.test")
    factories.publish_key(provider=qr, actor=staff)
    registration = factories.make_registration(event=event, person=factories.make_person())
    factories.assign(registration=registration, setup=setup, actor=staff)
    credential = factories.issue_active_pass(registration=registration, actor=staff)
    admin = factories.make_user(
        "offline.admin@example.test", group_name="Entry Device Administrators", event=event
    )
    device, _secret = factories.enroll_device(event=event, layout=layout, admin=admin)
    offline_factories.enable_event(event, admin)
    offline_factories.make_offline_capable(device, admin=admin, layout=layout)
    synthetic = offline_factories.SyntheticDevice(device=device)
    offline_factories.provision(synthetic, admin=admin)
    yield {
        "event": event,
        "layout": layout,
        "admin": admin,
        "staff": staff,
        "synthetic": synthetic,
        "credential": credential,
        "registration": registration,
        "root": root,
    }
    set_package_signing_key_provider_for_testing(None)
    set_signing_key_provider_for_testing(None)


def _no_dispatch(monkeypatch):
    """Keep the Celery dispatch out of the way so the test decides who builds."""
    monkeypatch.setattr(offline_packages, "_dispatch_build", lambda build_id: None)


def _delta(synthetic, package_public_id):
    body, nonce, signature = synthetic.signed("delta", {"package_id": package_public_id})
    return offline_packages.issue_delta(
        device=EntryDevice.objects.get(pk=synthetic.device.pk),
        package_public_id=package_public_id,
        nonce=nonce,
        signature=signature,
        body=body,
    )


# ---------------------------------------------------------------------------
# B. Build lifecycle
# ---------------------------------------------------------------------------


def test_concurrent_package_requests_converge_on_one_build_and_version(world, monkeypatch):
    _no_dispatch(monkeypatch)
    synthetic = world["synthetic"]
    results, errors = _run_in_threads(
        [lambda: offline_factories.download_result(synthetic) for _ in range(6)]
    )
    assert not errors, errors
    assert {result.status for result in results} == {"BUILDING"}
    assert len({result.build.pk for result in results}) == 1
    assert OfflinePackageBuild.objects.count() == 1
    assert OfflinePackageBuild.objects.get().package_version == 1
    assert (
        AuditEvent.objects.filter(action_code=action_codes.OFFLINE_PACKAGE_BUILD_REQUESTED).count()
        == 1
    )


def test_concurrent_and_redelivered_workers_build_exactly_one_version(world, monkeypatch):
    _no_dispatch(monkeypatch)
    build = offline_factories.download_result(world["synthetic"]).build
    results, errors = _run_in_threads(
        [lambda: offline_packages.run_package_build(build.pk) for _ in range(4)]
    )
    assert not errors, errors
    packages = {result.pk for result in results if result is not None}
    assert len(packages) == 1
    assert OfflinePackage.objects.count() == 1
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.READY
    assert build.attempts == 1
    assert OfflinePackage.objects.get().package_version == build.package_version
    # A later redelivery of the same task is a no-op returning the same package.
    assert offline_packages.run_package_build(build.pk).pk in packages
    assert OfflinePackage.objects.count() == 1


def test_a_real_commit_failure_leaves_no_stored_ciphertext(world, monkeypatch):
    """The commit itself fails: a DEFERRABLE foreign key is only checked at
    COMMIT, after every statement of the transaction succeeded."""
    _no_dispatch(monkeypatch)
    synthetic = world["synthetic"]
    build = offline_factories.download_result(synthetic).build
    before = _stored_files(world["root"])
    real_audit = offline_packages.audit

    def audit_then_break_the_commit(**kwargs):
        result = real_audit(**kwargs)
        if kwargs.get("action_code") == action_codes.OFFLINE_PACKAGE_BUILT:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE entry_device SET offline_prepared_by_id = gen_random_uuid() "
                    "WHERE id = %s",
                    [synthetic.device.pk],
                )
        return result

    monkeypatch.setattr(offline_packages, "audit", audit_then_break_the_commit)
    with pytest.raises(IntegrityError):
        offline_packages.run_package_build(build.pk)
    assert not OfflinePackage.objects.exists()
    assert _stored_files(world["root"]) == before
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.PENDING
    assert build.pending_object_key == ""
    monkeypatch.setattr(offline_packages, "audit", real_audit)
    package = offline_packages.run_package_build(build.pk)
    assert package.package_version == build.package_version


def test_a_build_overtaken_by_a_rescope_while_waiting_for_the_lock_is_never_committed(
    world, monkeypatch
):
    from apps.entry.services.devices import change_device_scope

    _no_dispatch(monkeypatch)
    synthetic = world["synthetic"]
    layout = world["layout"]
    build = offline_factories.download_result(synthetic).build
    before = _stored_files(world["root"])
    prepared = threading.Event()
    locked = threading.Event()
    real_prepare = offline_packages._prepare_package_bytes

    def prepare_then_wait(**kwargs):
        result = real_prepare(**kwargs)
        prepared.set()
        assert locked.wait(timeout=20)
        return result

    monkeypatch.setattr(offline_packages, "_prepare_package_bytes", prepare_then_wait)

    def rescope():
        assert prepared.wait(timeout=20)
        with transaction.atomic():
            device = EntryDevice.objects.select_for_update().get(pk=synthetic.device.pk)
            locked.set()
            assert _wait_for_lock_waiter()  # the builder now waits for the device lock
            change_device_scope(
                device=device,
                venue=layout.venue,
                gate=layout.gate_a,
                zones=[layout.main],
                verification_methods=factories.ALL_METHODS,
                actor=world["admin"],
                expected_version=device.version,
                offline_capable=True,
            )

    results, errors = _run_in_threads(
        [lambda: offline_packages.run_package_build(build.pk), rescope]
    )
    assert not errors, errors
    assert results[0] is None
    assert not OfflinePackage.objects.exists()
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.CANCELLED
    assert build.pending_object_key == ""
    assert _stored_files(world["root"]) == before


# ---------------------------------------------------------------------------
# C. Delta re-validation after the row lock, and commit order
# ---------------------------------------------------------------------------


def _delta_after_concurrent(world, change):
    """Run `change(device)` in a transaction holding the device row lock
    until the delta request is waiting on it; return the delta outcome."""
    synthetic = world["synthetic"]
    package = offline_packages.build_package(device=synthetic.device)
    locked = threading.Event()

    def lifecycle():
        with transaction.atomic():
            device = EntryDevice.objects.select_for_update().get(pk=synthetic.device.pk)
            locked.set()
            waited = _wait_for_lock_waiter()
            change(device)
        return waited

    def delta():
        assert locked.wait(timeout=20)
        try:
            _delta(synthetic, package.public_id)
        except OfflineUnavailable as exc:
            return exc.code
        return "ISSUED"

    results, errors = _run_in_threads([lifecycle, delta])
    assert not errors, errors
    assert results[0] is True, f"the delta never waited on the device lock: {results[1]}"
    return results[1]


def test_a_delta_waiting_behind_a_block_is_refused(world):
    from apps.entry.services.offline_devices import block_offline_use

    def block(device):
        block_offline_use(
            device=device,
            actor=world["admin"],
            expected_version=device.version,
            reason_code="SECURITY_CONCERN",
        )

    assert _delta_after_concurrent(world, block) == "DEVICE_OFFLINE_BLOCKED"


def test_a_delta_waiting_behind_a_rescope_is_refused(world):
    from apps.entry.services.devices import change_device_scope

    layout = world["layout"]

    def rescope(device):
        change_device_scope(
            device=device,
            venue=layout.venue,
            gate=layout.gate_a,
            zones=[layout.main],
            verification_methods=factories.ALL_METHODS,
            actor=world["admin"],
            expected_version=device.version,
            offline_capable=True,
        )

    assert _delta_after_concurrent(world, rescope) == "PACKAGE_NOT_CURRENT"


def test_a_delta_waiting_behind_a_reprovisioning_is_refused(world):
    replacement = offline_factories.SyntheticDevice(device=world["synthetic"].device)

    def reprovision(device):
        offline_factories.provision(replacement, admin=world["admin"])

    assert _delta_after_concurrent(world, reprovision) == "PACKAGE_NOT_CURRENT"


def test_a_change_committed_after_the_snapshot_reaches_the_delta_despite_an_older_timestamp(
    world,
):
    """T1 changes a restriction-relevant row and stays open; the package is
    built (T1 is in progress in its snapshot); T1 commits AFTER the build
    with a timestamp older than the package cutoff. An `updated_at` filter
    would miss it; the journal + snapshot watermark does not."""
    from apps.entry.models import RestrictionCategory, RestrictionSeverity, SecurityRestriction

    synthetic = world["synthetic"]
    registration = world["registration"]
    changed = threading.Event()
    built = threading.Event()
    backdated = timezone.now() - timedelta(hours=1)

    def open_writer():
        with transaction.atomic():
            SecurityRestriction.objects.create(
                person=registration.person,
                event_edition=world["event"],
                severity=RestrictionSeverity.DENY_ENTRY,
                category=RestrictionCategory.SECURITY_CONCERN,
                starts_at=backdated,
                reason_encrypted="synthetic late-commit restriction",
                created_by=world["staff"],
            )
            SecurityRestriction.objects.filter(registration=None).update(
                created_at=backdated, updated_at=backdated
            )
            changed.set()
            assert built.wait(timeout=30)
        return "COMMITTED"

    def builder():
        assert changed.wait(timeout=20)
        package = offline_packages.build_package(device=synthetic.device)
        built.set()
        return package

    results, errors = _run_in_threads([open_writer, builder])
    assert not errors, errors
    package = results[1]
    assert package.data_cutoff_at > backdated
    _row, raw = _delta(synthetic, package.public_id)
    _manifest, body = synthetic.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert body["restrictions"], "the late-committed restriction was missed"
    assert body["restrictions"][0]["jti"] == world["credential"].jti
    assert body["restrictions"][0]["restrictions"][0]["severity"] == "DENY_ENTRY"
