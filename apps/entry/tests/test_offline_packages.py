"""Offline Package build, minimization, retrieval, delta and cleanup
(Phase 4 Prompt 2, binding decisions P2-B/P2-C/P2-D/P2-E). PostgreSQL.

All data is synthetic. The synthetic device decrypts what the server built
for it, which is the only way the plaintext is ever observed -- and every
assertion about the plaintext is made on that decrypted copy.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry.models import (
    OfflineCriticalDelta,
    OfflinePackage,
    OfflinePackageBuild,
    OfflinePackageBuildStatus,
    OfflinePackageStatus,
    RestrictionCategory,
    RestrictionSeverity,
)
from apps.entry.offline_contract import (
    PACKAGE_ENTRY,
    TYP_DELTA_MANIFEST,
    TYP_PACKAGE_MANIFEST,
    event_day_end,
)
from apps.entry.services.offline_devices import OfflineRequestRejected, OfflineUnavailable
from apps.entry.services.offline_packages import (
    build_package,
    cleanup_offline_packages,
    download_package,
    issue_delta,
)
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db

SYNTHETIC_NIN = "109990000000000077"
SYNTHETIC_EMAIL = "sentinel.participant@example.test"
SYNTHETIC_REASON = "SENTINEL-RESTRICTION-REASON-TEXT"


def _open(offline_device, raw):
    return offline_device.open_response(raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG")


def _delta(offline_device, package):
    body, nonce, signature = offline_device.signed("delta", {"package_id": package.public_id})
    return issue_delta(
        device=offline_device.device,
        package_public_id=package.public_id,
        nonce=nonce,
        signature=signature,
        body=body,
    )


# ---------------------------------------------------------------------------
# Availability and authorization
# ---------------------------------------------------------------------------


def test_build_refused_when_offline_is_disabled_globally(offline_device, settings):
    settings.ENTRY_OFFLINE_ENABLED = False
    with pytest.raises(OfflineUnavailable) as exc:
        build_package(device=offline_device.device)
    assert exc.value.code == "OFFLINE_DISABLED"


def test_build_refused_when_the_event_is_not_enabled(offline_device, event):
    from apps.entry.models import OfflineEventSetting

    OfflineEventSetting.objects.filter(event_edition=event).update(enabled=False)
    with pytest.raises(OfflineUnavailable) as exc:
        build_package(device=offline_device.device)
    assert exc.value.code == "OFFLINE_DISABLED"


def test_build_refused_for_a_scope_that_is_not_offline_capable(
    offline_on, package_signer, event, layout, device, device_admin
):
    offline_factories.enable_event(event, device_admin)
    with pytest.raises(OfflineUnavailable) as exc:
        build_package(device=device)
    assert exc.value.code == "SCOPE_NOT_OFFLINE"


def test_build_refused_for_an_unprepared_device(
    offline_on, package_signer, event, layout, device, device_admin
):
    offline_factories.enable_event(event, device_admin)
    offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    with pytest.raises(OfflineUnavailable) as exc:
        build_package(device=device)
    assert exc.value.code == "DEVICE_NOT_PREPARED"


def test_download_requires_a_valid_device_proof(offline_device):
    other_key = offline_factories.SyntheticDevice(device=offline_device.device)
    body, nonce, signature = other_key.signed("package", {"have_version": None})
    with pytest.raises(OfflineRequestRejected) as exc:
        download_package(
            device=offline_device.device,
            have_version=None,
            nonce=nonce,
            signature=signature,
            body=body,
        )
    assert exc.value.code == "PROOF_INVALID"
    assert AuditEvent.objects.filter(
        action_code=action_codes.OFFLINE_REQUEST_REJECTED, reason_code="PROOF_INVALID"
    ).exists()


def test_download_proof_is_bound_to_its_purpose_and_single_use(offline_device):
    body, nonce, signature = offline_device.signed("delta", {"have_version": None})
    with pytest.raises(OfflineRequestRejected):
        download_package(
            device=offline_device.device,
            have_version=None,
            nonce=nonce,
            signature=signature,
            body=body,
        )
    body, nonce, signature = offline_device.signed("package", {"have_version": None})
    download_package(
        device=offline_device.device, have_version=None, nonce=nonce, signature=signature, body=body
    )
    with pytest.raises(OfflineRequestRejected) as exc:
        download_package(
            device=offline_device.device,
            have_version=None,
            nonce=nonce,
            signature=signature,
            body=body,
        )
    assert exc.value.code == "NONCE_REPLAYED"


# ---------------------------------------------------------------------------
# Minimization (binding decision P2-E)
# ---------------------------------------------------------------------------


def _sentinel_participant(event, setup, staff):
    person = factories.make_person("Sentinel Participant Omar")
    registration = factories.make_registration(event=event, person=person)
    factories.assign(registration=registration, setup=setup, actor=staff)
    factories.add_identifier(
        person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN, verified=True
    )
    from apps.entry.services.restrictions import create_restriction

    security = factories.make_user(
        "restriction.manager@example.test", group_name="Security Restriction Managers", event=event
    )
    create_restriction(
        actor=security,
        person=person,
        event_edition=event,
        severity=RestrictionSeverity.MANUAL_REVIEW,
        category=RestrictionCategory.SECURITY_CONCERN,
        reason=SYNTHETIC_REASON,
        is_overrideable=True,
    )
    credential = factories.issue_active_pass(registration=registration, actor=staff)
    return registration, credential


def test_package_plaintext_contains_only_allow_listed_fields(
    offline_device, event, setup, staff, key, active_pass
):
    registration, credential = _sentinel_participant(event, setup, staff)
    package, raw = offline_factories.download(offline_device)
    manifest, body = _open(offline_device, raw)
    plaintext = json.dumps(body, ensure_ascii=False)
    # Sentinel VALUES: none may appear anywhere in the plaintext.
    for forbidden in (
        SYNTHETIC_NIN,
        SYNTHETIC_EMAIL,
        SYNTHETIC_REASON,
        registration.public_reference,
        str(registration.pk),
        str(registration.person_id),
        "SECURITY_CONCERN",
        setup.role.code,
    ):
        assert forbidden not in plaintext, forbidden

    # Forbidden field NAMES: checked against keys only (a random pass
    # identifier may contain any short substring by chance).
    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key.lower()
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    all_keys = set(keys(body))
    for word in (
        "photo",
        "nin",
        "passport",
        "email",
        "phone",
        "reference",
        "category",
        "role",
        "document",
        "fingerprint",
        "hint",
    ):
        assert not any(word in key for key in all_keys), word
    entries = {entry["jti"]: entry for entry in body["entries"]}
    assert set(entries) == {active_pass.jti, credential.jti}
    for entry in entries.values():
        assert set(entry) == PACKAGE_ENTRY
    restricted = entries[credential.jti]
    assert restricted["restrictions"] == [
        {
            "severity": "MANUAL_REVIEW",
            "overrideable": True,
            "from": restricted["restrictions"][0]["from"],
            "until": None,
        }
    ]
    assert manifest["entry_count"] == 2 == package.entry_count


def test_only_approved_admissible_contexts_are_packaged(
    offline_device, event, layout, setup, staff, key, active_pass
):
    from apps.accreditation.models import AccessProfile, AccessProfileAssignment, AssignmentStatus
    from apps.registrations.models import RegistrationPublicStatus

    # Not approved: excluded.
    pending = factories.make_registration(
        event=event,
        person=factories.make_person("Pending Person"),
        public_status=RegistrationPublicStatus.SUBMITTED,
    )
    factories.assign(registration=pending, setup=setup, actor=staff)
    # Approved but only allowed into VIP (not this device's zones): excluded.
    vip_profile = AccessProfile.objects.create(event_edition=event, code="VIPONLY", name="VIP")
    from apps.accreditation.models import AccessRule

    AccessRule.objects.create(
        event_edition=event,
        access_profile=vip_profile,
        code="VIP_RULE",
        name="VIP",
        zone=layout.vip,
    )
    vip = factories.make_registration(event=event, person=factories.make_person("VIP Person"))
    factories.assign(registration=vip, setup=setup, actor=staff)
    AccessProfileAssignment.objects.filter(registration=vip).update(access_profile=vip_profile)
    vip_pass = factories.issue_active_pass(registration=vip, actor=staff)
    assert AccessProfileAssignment.objects.get(registration=vip, status=AssignmentStatus.CURRENT)

    _package, raw = offline_factories.download(offline_device)
    _manifest, body = _open(offline_device, raw)
    jtis = {entry["jti"] for entry in body["entries"]}
    assert jtis == {active_pass.jti}
    assert vip_pass.jti not in jtis


def test_manifest_binds_device_scope_versions_and_bands(offline_device, active_pass):
    package, raw = offline_factories.download(offline_device)
    manifest, body = _open(offline_device, raw)
    assert manifest["device"] == offline_device.device.public_id
    assert manifest["package_version"] == package.package_version == body["package_version"]
    assert manifest["scope_version"] == package.scope_version
    assert manifest["schema_version"] == 1
    assert manifest["data_cutoff_at"] == int(package.data_cutoff_at.timestamp())
    assert manifest["aging_at"] - manifest["data_cutoff_at"] <= 15 * 60
    assert manifest["expires_at"] - manifest["data_cutoff_at"] <= 8 * 60 * 60
    assert manifest["expires_at"] <= int(offline_device.device.expires_at.timestamp())
    assert body["gate"] == "GA" and body["zones"] == ["HALL", "MAIN"]


def test_qr_keys_in_the_package_are_never_package_trust_roots(offline_device, active_pass):
    _package, raw = offline_factories.download(offline_device)
    _manifest, body = _open(offline_device, raw)
    qr_kids = {key["kid"] for key in body["qr_keys"]}
    anchor_kids = {anchor["kid"] for anchor in offline_device.trust_anchors}
    assert qr_kids == {"v1"}
    assert anchor_kids == {"p1", "p2"}
    assert not qr_kids & anchor_kids


# ---------------------------------------------------------------------------
# Retrieval: immutable, byte-identical, audited (binding decision P2-C)
# ---------------------------------------------------------------------------


def test_retry_of_the_same_ready_version_is_byte_identical(offline_device, active_pass):
    first, raw_1 = offline_factories.download(offline_device)
    second, raw_2 = offline_factories.download(offline_device)
    assert first.pk == second.pk
    assert raw_1 == raw_2
    first.refresh_from_db()
    assert first.download_count == 2
    audits = AuditEvent.objects.filter(action_code=action_codes.OFFLINE_PACKAGE_DOWNLOADED)
    assert audits.count() == 2
    for row in audits:
        text = json.dumps(row.after_summary)
        assert "ciphertext" not in text and "manifest" not in text
        assert row.after_summary["response_sha256"] == first.response_sha256


def test_have_version_returns_not_modified(offline_device, active_pass):
    package, _raw = offline_factories.download(offline_device)
    again, raw = offline_factories.download(offline_device, have_version=package.package_version)
    assert again.pk == package.pk and raw is None


def _clock_away_from_the_event_day_end(device) -> datetime:
    """A controlled `now` for the refresh scenarios (UX-C2, item D).

    A package never outlives the end of its event day, and a build that would
    expire within a minute of its cutoff is refused (`package_bands`). With
    the real clock, `now + refresh + 5 s` fell in that last minute about once
    a day, so the successor build was correctly CANCELLED and the test failed
    (observed once in a full gate, around 23:54 UTC). The scenario now starts
    at least ten minutes before the day end, whatever the time of day.
    """
    now = timezone.now()
    return min(now, event_day_end(now, device.event_edition.timezone) - timedelta(minutes=10))


def test_an_old_package_is_replaced_by_a_new_version(offline_device, active_pass, settings):
    base = _clock_away_from_the_event_day_end(offline_device.device)
    first, _ = offline_factories.download(offline_device, now=base)
    later = base + timedelta(seconds=settings.ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS + 5)
    # Still valid: served while its successor builds (the request never builds).
    served, raw = offline_factories.download(offline_device, now=later)
    assert served.pk == first.pk and raw is not None
    build = OfflinePackageBuild.objects.get(status=OfflinePackageBuildStatus.PENDING)
    assert build.package_version == first.package_version + 1
    assert build.request_reason == "REFRESH"
    offline_factories.run_pending_builds(now=later)
    second, _ = offline_factories.download(offline_device, now=later)
    assert second.package_version == first.package_version + 1
    first.refresh_from_db()
    assert first.status == OfflinePackageStatus.SUPERSEDED


def test_a_refresh_in_the_last_minute_of_the_event_day_keeps_the_valid_package(
    offline_device, active_pass, settings
):
    """The approved rule behind the observed failure, at a controlled instant:
    a successor that would expire within a minute of its cutoff is refused
    (NO_VALIDITY), and the still-valid package keeps being served, never
    superseded by nothing."""
    event_timezone = offline_device.device.event_edition.timezone
    day_end = event_day_end(timezone.now() - timedelta(days=1), event_timezone)
    refresh = settings.ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS
    base = day_end - timedelta(seconds=refresh + 5 + 30)
    first, _ = offline_factories.download(offline_device, now=base)
    later = base + timedelta(seconds=refresh + 5)  # 30 s before the event day ends
    offline_factories.download(offline_device, now=later)
    offline_factories.run_pending_builds(now=later)
    second, raw = offline_factories.download(offline_device, now=later)
    assert second.pk == first.pk and raw is not None
    build = OfflinePackageBuild.objects.get(package_version=first.package_version + 1)
    assert build.status == OfflinePackageBuildStatus.CANCELLED
    assert build.status_reason_code == "NO_VALIDITY"
    first.refresh_from_db()
    assert first.status == OfflinePackageStatus.READY


def test_a_missing_stored_object_never_reuses_a_version(offline_device, active_pass):
    from apps.documents.storage import get_private_storage

    first, raw_1 = offline_factories.download(offline_device)
    get_private_storage().delete(first.stored_object_key)
    second, raw_2 = offline_factories.download(offline_device)
    assert second.package_version == first.package_version + 1
    assert raw_1 != raw_2
    first.refresh_from_db()
    assert first.status == OfflinePackageStatus.REVOKED
    assert first.status_reason_code == "STORED_OBJECT_UNAVAILABLE"


def test_corrupted_stored_bytes_are_never_served(offline_device, active_pass, private_storage_root):
    first, _ = offline_factories.download(offline_device)
    (private_storage_root / first.stored_object_key).write_bytes(b'{"tampered": true}')
    second, raw = offline_factories.download(offline_device)
    assert second.package_version == first.package_version + 1
    assert b"tampered" not in raw


def test_plaintext_and_data_keys_never_reach_storage_or_audit(
    offline_device, active_pass, private_storage_root
):
    _package, raw = offline_factories.download(offline_device)
    stored = b"".join(path.read_bytes() for path in private_storage_root.iterdir())
    assert active_pass.jti.encode() not in stored
    assert b"Synthetic Participant" not in stored
    assert json.loads(raw)  # only the envelope, readable only by the device
    audit_text = json.dumps(
        list(
            AuditEvent.objects.filter(action_code__startswith="ENT_OFFLINE").values(
                "after_summary", "reason_code"
            )
        )
    )
    assert active_pass.jti not in audit_text and "Synthetic Participant" not in audit_text


def test_download_budget_is_enforced(offline_device, active_pass, settings):
    # Build requests count against the budget too (correction B): the first
    # call spends two (build requested + downloaded), the second one more.
    settings.ENTRY_OFFLINE_DOWNLOADS_PER_WINDOW = 3
    offline_factories.download(offline_device)
    offline_factories.download(offline_device)
    with pytest.raises(OfflineUnavailable) as exc:
        offline_factories.download(offline_device)
    assert exc.value.code == "RATE_LIMITED"
    assert AuditEvent.objects.filter(
        action_code=action_codes.OFFLINE_PACKAGE_DOWNLOAD_DENIED, reason_code="RATE_LIMITED"
    ).exists()


# ---------------------------------------------------------------------------
# Database-enforced immutability
# ---------------------------------------------------------------------------


def test_package_content_columns_are_immutable_in_the_database(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackage.objects.filter(pk=package.pk).update(ciphertext_sha256="0" * 64)
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackage.objects.filter(pk=package.pk).update(expires_at=timezone.now())
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackage.objects.filter(pk=package.pk).delete()
    # Lifecycle bookkeeping remains possible.
    OfflinePackage.objects.filter(pk=package.pk).update(download_count=7)


def test_a_terminal_package_cannot_be_revived(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    OfflinePackage.objects.filter(pk=package.pk).update(status="REVOKED")
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackage.objects.filter(pk=package.pk).update(status="READY")


def test_deltas_are_append_only(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    delta, _ = _delta(offline_device, package)
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflineCriticalDelta.objects.filter(pk=delta.pk).update(rebuild_required=True)
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflineCriticalDelta.objects.filter(pk=delta.pk).delete()


def test_bands_ordered_constraint(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
            ["entry_opkg_bands_ordered"],
        )
        assert cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Critical delta (binding decision P2-B)
# ---------------------------------------------------------------------------


def test_delta_never_moves_the_package_cutoff(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    cutoff = package.data_cutoff_at
    delta, raw = _delta(offline_device, package)
    manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    package.refresh_from_db()
    assert package.data_cutoff_at == cutoff
    assert manifest["critical_delta_cutoff_at"] >= int(cutoff.timestamp())
    assert body["package_id"] == package.public_id
    assert delta.delta_version == 1
    delta_2, _ = _delta(offline_device, package)
    assert delta_2.delta_version == 2


def test_delta_carries_pass_revocation_after_the_cutoff(offline_device, active_pass, staff):
    from apps.badges.services import new_operation_id, revoke_pass

    package, _ = offline_factories.download(offline_device)
    revoke_pass(
        credential=active_pass,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=active_pass.version,
        reason_code="SECURITY_CONCERN",
    )
    _delta_row, raw = _delta(offline_device, package)
    _manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert {"jti": active_pass.jti, "status": "REVOKED"} in body["passes"]


def test_delta_carries_new_restrictions_without_category_or_reason(
    offline_device, event, active_pass, registration
):
    from apps.entry.services.restrictions import create_restriction

    package, _ = offline_factories.download(offline_device)
    security = factories.make_user(
        "security.two@example.test", group_name="Security Restriction Managers", event=event
    )
    create_restriction(
        actor=security,
        person=registration.person,
        event_edition=event,
        severity=RestrictionSeverity.DENY_ENTRY,
        category=RestrictionCategory.IDENTITY_DISPUTE,
        reason=SYNTHETIC_REASON,
        is_overrideable=False,
    )
    _row, raw = _delta(offline_device, package)
    _manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert body["restrictions"][0]["jti"] == active_pass.jti
    assert body["restrictions"][0]["restrictions"][0]["severity"] == "DENY_ENTRY"
    text = json.dumps(body)
    assert SYNTHETIC_REASON not in text and "IDENTITY_DISPUTE" not in text


def test_delta_withdraws_a_context_whose_assignment_changed(
    offline_device, active_pass, registration
):
    from apps.accreditation.models import AccessProfileAssignment

    package, _ = offline_factories.download(offline_device)
    assignment = AccessProfileAssignment.objects.get(registration=registration)
    assignment.save()  # any change after the cutoff withdraws the context
    _row, raw = _delta(offline_device, package)
    _manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert {"jti": active_pass.jti, "reason": "ASSIGNMENT_CHANGED"} in body["access_withdrawn"]


def test_delta_withdraws_a_context_that_is_no_longer_approved(
    offline_device, active_pass, registration
):
    from apps.registrations.models import Registration

    package, _ = offline_factories.download(offline_device)
    Registration.objects.filter(pk=registration.pk).update(withdrawn_at=timezone.now())
    _row, raw = _delta(offline_device, package)
    _manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert {"jti": active_pass.jti, "reason": "REGISTRATION_NOT_APPROVED"} in body[
        "access_withdrawn"
    ]


def test_delta_flags_profile_rule_changes_and_key_revocation(
    offline_device, active_pass, setup, key, staff
):
    from apps.badges.services import revoke_verification_key

    package, _ = offline_factories.download(offline_device)
    setup.main_rule.save()
    revoke_verification_key(key=key, actor=staff, reason_code="SUSPECTED_COMPROMISE")
    _row, raw = _delta(offline_device, package)
    _manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert setup.access_profile.code in body["withdrawn_access_profiles"]
    assert body["revoked_kids"] == ["v1"]


def test_delta_requires_rebuild_when_it_cannot_represent_a_change(
    offline_device, active_pass, layout
):
    package, _ = offline_factories.download(offline_device)
    layout.gate_a.name = "Gate A renamed"
    layout.gate_a.save()
    _row, raw = _delta(offline_device, package)
    _manifest, body = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert body["rebuild_required"] is True
    assert "CHECKPOINT_CHANGED" in body["rebuild_reasons"]


def test_delta_refused_for_a_package_that_is_not_current(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    OfflinePackage.objects.filter(pk=package.pk).update(status="REVOKED")
    with pytest.raises(OfflineUnavailable) as exc:
        _delta(offline_device, package)
    assert exc.value.code == "PACKAGE_NOT_CURRENT"


# ---------------------------------------------------------------------------
# Cleanup: package data only, configurable retention
# ---------------------------------------------------------------------------


def test_cleanup_expires_packages_and_purges_retained_ciphertext(
    offline_device, active_pass, settings
):
    from apps.documents.storage import get_private_storage

    package, _ = offline_factories.download(offline_device)
    later = package.expires_at + timedelta(seconds=1)
    result = cleanup_offline_packages(now=later)
    package.refresh_from_db()
    assert result["expired"] == 1 and package.status == OfflinePackageStatus.EXPIRED
    assert get_private_storage().exists(package.stored_object_key)
    much_later = later + timedelta(seconds=settings.ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS + 1)
    result = cleanup_offline_packages(now=much_later)
    package.refresh_from_db()
    assert result["purged"] == 1
    assert package.stored_object_deleted_at is not None
    assert not get_private_storage().exists(package.stored_object_key)
    assert OfflinePackage.objects.filter(pk=package.pk).exists()  # metadata row kept


def test_cleanup_withdraws_devices_of_a_closed_event(offline_device, active_pass, event):
    package, _ = offline_factories.download(offline_device)
    type(event).objects.filter(pk=event.pk).update(status="COMPLETED")
    result = cleanup_offline_packages()
    package.refresh_from_db()
    assert result["closed_devices"] == 1
    assert package.status == OfflinePackageStatus.REVOKED


# ---------------------------------------------------------------------------
# Build lifecycle (independent re-review correction B)
# ---------------------------------------------------------------------------


def _stored_files(root):
    return sorted(path.name for path in root.rglob("*") if path.is_file())


def test_the_download_request_never_builds_and_answers_building(offline_device, active_pass):
    result = offline_factories.download_result(offline_device)
    assert result.status == "BUILDING"
    assert result.retry_after_seconds > 0
    assert result.package is None and result.raw is None
    assert not OfflinePackage.objects.exists()
    build = OfflinePackageBuild.objects.get()
    assert build.status == OfflinePackageBuildStatus.PENDING
    assert build.package_version == 1 and build.request_reason == "NO_PACKAGE"


def test_repeated_requests_converge_on_one_build_and_one_version(offline_device, active_pass):
    first = offline_factories.download_result(offline_device)
    second = offline_factories.download_result(offline_device)
    assert first.build.pk == second.build.pk
    assert OfflinePackageBuild.objects.count() == 1
    requested = AuditEvent.objects.filter(action_code=action_codes.OFFLINE_PACKAGE_BUILD_REQUESTED)
    assert requested.count() == 1


def test_a_repeated_or_redelivered_build_never_creates_a_second_version(
    offline_device, active_pass
):
    from apps.entry.services.offline_packages import run_package_build
    from apps.entry.tasks import build_offline_package_task

    build = offline_factories.download_result(offline_device).build
    first = run_package_build(build.pk)
    again = run_package_build(build.pk)
    assert first is not None and again.pk == first.pk
    assert build_offline_package_task(str(build.pk)) == first.package_version
    assert OfflinePackage.objects.count() == 1
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.READY
    assert build.package_id == first.pk and build.attempts == 1


def test_a_live_lease_is_left_alone_and_an_expired_one_reclaims_the_same_version(
    offline_device, active_pass
):
    import uuid

    from apps.entry.services.offline_packages import run_package_build

    build = offline_factories.download_result(offline_device).build
    OfflinePackageBuild.objects.filter(pk=build.pk).update(
        status=OfflinePackageBuildStatus.BUILDING,
        attempts=1,
        lease_token=uuid.uuid4(),
        lease_until=timezone.now() + timedelta(minutes=5),
    )
    assert run_package_build(build.pk) is None
    assert not OfflinePackage.objects.exists()
    OfflinePackageBuild.objects.filter(pk=build.pk).update(
        lease_until=timezone.now() - timedelta(seconds=1)
    )
    package = run_package_build(build.pk)
    assert package.package_version == build.package_version
    build.refresh_from_db()
    assert build.attempts == 2 and build.status == OfflinePackageBuildStatus.READY


def test_a_build_fails_after_the_maximum_attempts_and_never_reuses_its_version(
    offline_device, active_pass, settings
):
    from apps.entry.services.offline_packages import run_package_build

    build = offline_factories.download_result(offline_device).build
    OfflinePackageBuild.objects.filter(pk=build.pk).update(
        attempts=settings.ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS
    )
    assert run_package_build(build.pk) is None
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.FAILED
    assert build.status_reason_code == "MAX_ATTEMPTS"
    again = offline_factories.download_result(offline_device).build
    assert again.pk != build.pk
    assert again.package_version == build.package_version + 1


def test_a_lifecycle_change_cancels_the_open_build(
    offline_device, active_pass, device_admin, layout
):
    from apps.entry.services.offline_packages import run_package_build

    build = offline_factories.download_result(offline_device).build
    offline_factories.make_offline_capable(
        offline_device.device, admin=device_admin, layout=layout, zones=[layout.main]
    )
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.CANCELLED
    assert build.status_reason_code == "SCOPE_CHANGED"
    assert run_package_build(build.pk) is None
    assert not OfflinePackage.objects.exists()


def test_a_build_overtaken_before_commit_is_cancelled_and_its_ciphertext_deleted(
    offline_device, active_pass, event, private_storage_root, monkeypatch
):
    """Re-validation under the row locks at commit time, even when the
    overtaking change bypassed every service hook."""
    from apps.entry.models import OfflineEventSetting
    from apps.entry.services import offline_packages

    build = offline_factories.download_result(offline_device).build
    before = _stored_files(private_storage_root)
    real = offline_packages._prepare_package_bytes

    def overtaken(**kwargs):
        prepared = real(**kwargs)
        OfflineEventSetting.objects.filter(event_edition=event).update(enabled=False)
        return prepared

    monkeypatch.setattr(offline_packages, "_prepare_package_bytes", overtaken)
    assert offline_packages.run_package_build(build.pk) is None
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.CANCELLED
    assert build.status_reason_code == "OFFLINE_DISABLED"
    assert build.pending_object_key == ""
    assert not OfflinePackage.objects.exists()
    assert _stored_files(private_storage_root) == before


def test_a_failure_inside_the_commit_transaction_deletes_the_stored_ciphertext(
    offline_device, active_pass, private_storage_root, monkeypatch
):
    from apps.entry.services import offline_packages

    build = offline_factories.download_result(offline_device).build
    before = _stored_files(private_storage_root)
    real_audit = offline_packages.audit

    def failing_audit(**kwargs):
        if kwargs.get("action_code") == action_codes.OFFLINE_PACKAGE_BUILT:
            raise RuntimeError("synthetic failure inside the commit transaction")
        return real_audit(**kwargs)

    monkeypatch.setattr(offline_packages, "audit", failing_audit)
    with pytest.raises(RuntimeError):
        offline_packages.run_package_build(build.pk)
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.PENDING  # released, not lost
    assert build.pending_object_key == "" and build.attempts == 1
    assert not OfflinePackage.objects.exists()
    assert _stored_files(private_storage_root) == before
    monkeypatch.setattr(offline_packages, "audit", real_audit)
    package = offline_packages.run_package_build(build.pk)
    assert package.package_version == build.package_version  # same version, no gap


def test_cleanup_deletes_orphaned_build_ciphertext(offline_device, active_pass):
    import io

    from apps.documents.storage import get_private_storage

    build = offline_factories.download_result(offline_device).build
    storage = get_private_storage()
    key = storage.save(io.BytesIO(b'{"orphan": true}'), "application/json")
    OfflinePackageBuild.objects.filter(pk=build.pk).update(pending_object_key=key)
    result = cleanup_offline_packages()
    assert result["orphans"] == 1
    assert not storage.exists(key)
    build.refresh_from_db()
    assert build.pending_object_key == ""


def test_cleanup_releases_an_abandoned_build_lease(offline_device, active_pass):
    import uuid

    build = offline_factories.download_result(offline_device).build
    OfflinePackageBuild.objects.filter(pk=build.pk).update(
        status=OfflinePackageBuildStatus.BUILDING,
        attempts=1,
        lease_token=uuid.uuid4(),
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    assert cleanup_offline_packages()["abandoned_builds"] == 1
    build.refresh_from_db()
    assert build.status == OfflinePackageBuildStatus.PENDING and build.lease_token is None


def test_build_rows_are_never_deleted_and_terminal_status_is_final(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    build = OfflinePackageBuild.objects.get(package=package)
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackageBuild.objects.filter(pk=build.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackageBuild.objects.filter(pk=build.pk).update(status="PENDING")
    with pytest.raises(DatabaseError), transaction.atomic():
        OfflinePackageBuild.objects.filter(pk=build.pk).update(package_version=99)


def test_a_rebuild_blocking_change_withholds_the_current_package(
    offline_device, active_pass, settings
):
    from apps.badges.models import DigitalEntryPass

    offline_factories.download(offline_device)
    settings.ENTRY_OFFLINE_DELTA_MAX_CHANGES = 1
    for _ in range(2):  # QuerySet.update(): no service, no updated_at
        DigitalEntryPass.objects.filter(pk=active_pass.pk).update(status="ACTIVE")
    result = offline_factories.download_result(offline_device)
    assert result.status == "BUILDING"
    assert result.build.request_reason == "REBUILD_REQUIRED"


# ---------------------------------------------------------------------------
# Delta re-validation under the row locks (independent re-review correction C)
# ---------------------------------------------------------------------------


def test_delta_refused_for_a_superseded_package(offline_device, active_pass):
    first, _ = offline_factories.download(offline_device)
    build_package(device=offline_device.device)
    with pytest.raises(OfflineUnavailable) as exc:
        _delta(offline_device, first)
    assert exc.value.code == "PACKAGE_NOT_CURRENT"


def test_delta_refused_when_the_package_key_was_replaced_behind_the_service(
    offline_device, active_pass
):
    from apps.entry.models import DeviceKeyPurpose, DeviceKeyStatus, EntryDeviceKey

    package, _ = offline_factories.download(offline_device)
    EntryDeviceKey.objects.filter(
        device=offline_device.device, purpose=DeviceKeyPurpose.PACKAGE_UNWRAP
    ).update(status=DeviceKeyStatus.RETIRED, retired_at=timezone.now())
    with pytest.raises(OfflineUnavailable) as exc:
        _delta(offline_device, package)
    assert exc.value.code == "PACKAGE_NOT_CURRENT"
    assert not OfflineCriticalDelta.objects.exists()


def test_delta_refused_for_a_device_blocked_behind_the_service(offline_device, active_pass):
    from apps.entry.models import EntryDevice

    package, _ = offline_factories.download(offline_device)
    EntryDevice.objects.filter(pk=offline_device.device.pk).update(
        offline_blocked_at=timezone.now()
    )
    with pytest.raises(OfflineUnavailable) as exc:
        _delta(offline_device, package)
    assert exc.value.code == "DEVICE_OFFLINE_BLOCKED"
    assert not OfflineCriticalDelta.objects.exists()


def test_an_explicit_build_always_produces_a_newer_version(offline_device, active_pass):
    """Regression: the "overtaken by a committed build" shortcut must never
    hand back the package the caller already holds."""
    first, _ = offline_factories.download(offline_device)
    second = build_package(device=offline_device.device)
    assert second.package_version == first.package_version + 1
