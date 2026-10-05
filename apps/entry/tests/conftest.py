"""Shared fixtures for the online-entry suite. All data is synthetic."""

from __future__ import annotations

import pytest

from apps.core.crypto.signing import (
    InMemorySigningKeyProvider,
    set_signing_key_provider_for_testing,
)
from apps.entry.tests import factories


@pytest.fixture(autouse=True)
def _seed(request):
    # An autouse fixture runs before the database fixtures it depends on, so
    # it must activate database access explicitly.
    if "transactional_db" in request.fixturenames:
        request.getfixturevalue("transactional_db")
    elif "db" in request.fixturenames:
        request.getfixturevalue("db")
    else:
        return
    factories.seed_reference_values()


@pytest.fixture
def signing_provider():
    provider = InMemorySigningKeyProvider(key_ids=("v1", "v2"), current="v1")
    set_signing_key_provider_for_testing(provider)
    yield provider
    set_signing_key_provider_for_testing(None)


@pytest.fixture
def event(db):
    return factories.make_event("ENTTEST")


@pytest.fixture
def other_event(db):
    return factories.make_event("ENTOTHER")


@pytest.fixture
def layout(event):
    return factories.VenueLayout(event)


@pytest.fixture
def setup(event, layout):
    return factories.AccreditationSetup(event, layout)


@pytest.fixture
def staff(db):
    """A plain actor for configuration-time services that do not check scope."""
    return factories.make_user("staff@example.test")


@pytest.fixture
def device_admin(event):
    return factories.make_user(
        "device.admin@example.test", group_name="Entry Device Administrators", event=event
    )


@pytest.fixture
def operator(event, layout):
    return factories.make_user(
        "operator@example.test", group_name="Entry Operators", event=event, gate=layout.gate_a
    )


@pytest.fixture
def supervisor(event, layout):
    return factories.make_user(
        "supervisor@example.test", group_name="Entry Supervisors", event=event, gate=layout.gate_a
    )


@pytest.fixture
def person(db):
    return factories.make_person()


@pytest.fixture
def key(signing_provider, staff):
    return factories.publish_key(provider=signing_provider, actor=staff)


@pytest.fixture
def registration(event, person, setup, staff):
    registration = factories.make_registration(event=event, person=person)
    factories.assign(registration=registration, setup=setup, actor=staff)
    return registration


@pytest.fixture
def active_pass(registration, key, staff):
    return factories.issue_active_pass(registration=registration, actor=staff)


@pytest.fixture
def device_and_secret(event, layout, device_admin):
    return factories.enroll_device(event=event, layout=layout, admin=device_admin)


@pytest.fixture
def device(device_and_secret):
    return device_and_secret[0]


@pytest.fixture
def checkpoint(device, operator, layout):
    return factories.open_checkpoint(device=device, user=operator, zone=layout.main)


@pytest.fixture
def supervisor_checkpoint(device, supervisor, layout):
    return factories.open_checkpoint(device=device, user=supervisor, zone=layout.main)


# ---------------------------------------------------------------------------
# Offline preparation (Phase 4 Prompt 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def package_signer():
    from apps.core.crypto.package_signing import (
        InMemoryPackageSigningKeyProvider,
        set_package_signing_key_provider_for_testing,
    )

    provider = InMemoryPackageSigningKeyProvider(key_ids=("p1", "p2"), current="p1")
    set_package_signing_key_provider_for_testing(provider)
    yield provider
    set_package_signing_key_provider_for_testing(None)


@pytest.fixture
def private_storage_root(tmp_path):
    """An isolated private-storage root for one test -- never var/private/."""
    root = tmp_path / "private"
    root.mkdir()
    return root


@pytest.fixture
def offline_on(settings, private_storage_root):
    settings.ENTRY_OFFLINE_ENABLED = True
    settings.PRIVATE_STORAGE_ROOT = private_storage_root
    return settings


@pytest.fixture
def offline_device(offline_on, package_signer, event, layout, device, device_admin):
    """An enrolled device, offline-capable scope, event enabled, provisioned."""
    from apps.entry.tests import offline_factories

    offline_factories.enable_event(event, device_admin)
    offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    synthetic = offline_factories.SyntheticDevice(device=device)
    offline_factories.provision(synthetic, admin=device_admin)
    return synthetic


# ---------------------------------------------------------------------------
# Offline synchronization (Phase 4 Prompt 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_world(offline_device, active_pass, device_admin, operator, layout):
    """An OFFLINE_READY device with a current package (holding `active_pass`),
    an operator's live checkpoint session at gate A / MAIN, that operator's
    offline grant, and an empty local operation store."""
    from types import SimpleNamespace

    from apps.entry.tests import offline_factories

    event = offline_device.device.event_edition
    event.timezone = offline_factories.timezone_with_day_left(hours=9)
    event.save(update_fields=["timezone"])
    offline_device.device.refresh_from_db()
    package, _raw = offline_factories.download(offline_device)
    offline_factories.self_test(offline_device, admin=device_admin, package=package)
    checkpoint = factories.open_checkpoint(
        device=offline_device.device, user=operator, zone=layout.main
    )
    grant, _compact = offline_factories.issue_grant(offline_device, checkpoint=checkpoint)
    return SimpleNamespace(
        synthetic=offline_device,
        device=offline_device.device,
        package=package,
        checkpoint=checkpoint,
        grant=grant,
        store=offline_factories.SyntheticStore(offline_device),
        credential=active_pass,
        operator=operator,
    )
