"""Offline Package builder, retrieval, critical delta, revocation and cleanup
(Phase 4 Prompt 2, ADR-0023, `docs/security/offline_package_contract.md`).

Build (binding decisions P2-C/P2-D/P2-E; independent re-review correction B):

1. the download API never builds. It records -- or finds -- the device's
   ONE open `OfflinePackageBuild` (version reserved there) and answers
   BUILDING; a Celery task (`build_offline_package_task`) runs
   `run_package_build`, which is idempotent and concurrency-safe;
2. the builder captures the change-journal watermark, then projects the
   MINIMUM data for signed-QR verification at this device's gate and zones
   -- only approved, current contexts holding an ACTIVE pass with at least
   one reachable ALLOW rule (no participant directory);
3. validates the body against the strict allow-list (fail closed);
4. encrypts it in memory to the device's registered unwrap key
   (AES-256-GCM + ECIES-style P-256 wrap) and discards the data key;
5. signs the manifest (ciphertext hash, bands, scope, versions) with the
   SEPARATE package-signing family;
6. stores the immutable response bytes (manifest + ciphertext) through the
   existing private-storage adapter, then commits the metadata under the
   device and build row locks after re-validating everything. Stored bytes
   that do not end up committed are deleted, whatever fails.

Retrieval returns the STORED bytes, so a retry of the same READY version is
byte-identical. A missing or corrupted stored object never reuses a version:
a new version is built instead. Plaintext never touches storage, a log, the
audit trail, or a cache.

Critical deltas (correction C) are driven by the database change journal
(`apps.entry.services.offline_journal`), never by `updated_at`.
"""

from __future__ import annotations

import io
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import (
    OPEN_BUILD_STATUSES,
    DeviceKeyPurpose,
    EntryDevice,
    OfflineCriticalDelta,
    OfflinePackage,
    OfflinePackageBuild,
    OfflinePackageBuildStatus,
    OfflinePackageStatus,
    OfflineRequestNonce,
)
from apps.entry.offline_contract import (
    DELTA_SCHEMA_VERSION,
    PACKAGE_RESPONSE_BUILDING,
    PACKAGE_RESPONSE_NOT_MODIFIED,
    PACKAGE_RESPONSE_READY,
    PACKAGE_SCHEMA_VERSION,
    REBUILD_BLOCKING_REASONS,
    TYP_DELTA_MANIFEST,
    TYP_PACKAGE_MANIFEST,
    ValidityWindowError,
    package_bands,
    validate_delta_body,
    validate_manifest,
    validate_package_body,
)
from apps.entry.services import audit, new_public_id
from apps.entry.services.offline_crypto import (
    b64url_encode,
    canonical_json,
    encrypt_for_device,
    jws_sign,
    load_device_public_key,
    pem_to_spki_b64,
    sha256_hex,
)
from apps.entry.services.offline_devices import (
    OfflineRequestRejected,
    OfflineUnavailable,
    active_device_key,
    offline_unavailability,
    record_request_rejection,
    verify_device_proof,
)
from apps.entry.services.offline_journal import capture_watermark, journal_changes, prune_journal

_CONTENT_TYPE = "application/json"
_NON_ACTIVE_PASS_STATUSES = ("REVOKED", "REPLACED", "SUSPENDED", "EXPIRED")


def _epoch(value):
    return int(value.timestamp()) if value is not None else None


# ---------------------------------------------------------------------------
# Projection (the minimum; binding decision P2-E)
# ---------------------------------------------------------------------------


def _current_assignments(queryset, registration_ids, now) -> dict:
    from apps.accreditation.models import AssignmentStatus

    rows = (
        queryset.filter(
            registration_id__in=registration_ids,
            status=AssignmentStatus.CURRENT,
            effective_from__lte=now,
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))
        .order_by("registration_id", "-effective_from")
    )
    current: dict = {}
    for row in rows:
        current.setdefault(row.registration_id, row)
    return current


def _restriction_rows(event, registration_by_id: dict, *, now, until) -> dict:
    """Active or upcoming (before `until`) restrictions per registration id.

    Severity, overrideability and window only -- never category or reason.
    """
    from apps.entry.models import RestrictionStatus, SecurityRestriction

    person_to_regs = defaultdict(list)
    for registration in registration_by_id.values():
        if registration.person_id is not None:
            person_to_regs[registration.person_id].append(registration.pk)
    rows = (
        SecurityRestriction.objects.filter(status=RestrictionStatus.ACTIVE)
        .filter(Q(event_edition__isnull=True) | Q(event_edition=event))
        .filter(
            Q(registration_id__in=list(registration_by_id)) | Q(person_id__in=list(person_to_regs))
        )
        .filter(starts_at__lt=until)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .order_by("starts_at", "pk")
    )
    result = defaultdict(list)
    for row in rows:
        targets = [row.registration_id] if row.registration_id else person_to_regs[row.person_id]
        for registration_id in targets:
            result[registration_id].append(
                {
                    "severity": row.severity,
                    "overrideable": bool(row.is_overrideable),
                    "from": _epoch(row.starts_at),
                    "until": _epoch(row.ends_at),
                }
            )
    return result


def _zone_rules(*, rules, direct_rules, zone_ancestors: dict, gate_id) -> list:
    """Per permitted zone, the rules `assess_context` would consider.

    Mirrors `apps.entry.services.access._rule_blocker`: active rules of the
    event, zone or ancestor, ENTRY/ANY, gate unset or equal, with windows.
    Direct rule assignments contribute their own effective window.
    """
    result = []
    for zone_code, ancestor_ids in zone_ancestors.items():
        applicable = []
        for rule, window_from, window_until in [*((r, None, None) for r in rules), *direct_rules]:
            if not rule.is_active or rule.zone_id not in ancestor_ids:
                continue
            if rule.event_type not in ("ENTRY", "ANY"):
                continue
            if rule.gate_id is not None and rule.gate_id != gate_id:
                continue
            starts = max((v for v in (rule.valid_from, window_from) if v is not None), default=None)
            ends = min((v for v in (rule.valid_until, window_until) if v is not None), default=None)
            applicable.append(
                {"effect": rule.effect, "from": _epoch(starts), "until": _epoch(ends)}
            )
        applicable.sort(key=lambda r: (r["effect"], r["from"] or 0, r["until"] or 0))
        result.append({"zone": zone_code, "rules": applicable})
    return result


def project_package_body(
    *,
    device: EntryDevice,
    scope,
    package_public_id: str,
    package_version: int,
    bands,
    data_cutoff_at,
) -> dict:
    """Build the allow-listed package body for one device scope version."""
    from apps.accreditation.models import (
        AccessProfileAssignment,
        AccessRule,
        AccessRuleAssignment,
        BadgeTypeAssignment,
    )
    from apps.badges.models import DigitalEntryPass, VerificationKey
    from apps.entry.models import EntryDecision, EntryEvent, EntryEventType, EntryOverrideReason
    from apps.entry.services.verification import eligible_contexts_queryset
    from apps.events.services import zone_and_ancestor_ids

    now = data_cutoff_at
    event = device.event_edition
    zones = sorted(scope.permitted_zones.filter(is_active=True), key=lambda z: z.code)
    zone_ancestors = {zone.code: set(zone_and_ancestor_ids(zone)) for zone in zones}

    passes = list(
        DigitalEntryPass.objects.select_related("registration__person")
        .filter(event_edition=event, status="ACTIVE", valid_until__gt=now)
        .filter(registration__in=eligible_contexts_queryset(event))
        .order_by("jti")
    )
    registration_by_id = {p.registration_id: p.registration for p in passes}
    reg_ids = list(registration_by_id)
    badges = (
        _current_assignments(BadgeTypeAssignment.objects.select_related("badge_type"), reg_ids, now)
        if reg_ids
        else {}
    )
    accesses = (
        _current_assignments(
            AccessProfileAssignment.objects.select_related("access_profile"), reg_ids, now
        )
        if reg_ids
        else {}
    )
    profile_ids = {a.access_profile_id for a in accesses.values()}
    rules_by_profile = defaultdict(list)
    for rule in AccessRule.objects.filter(event_edition=event, access_profile_id__in=profile_ids):
        rules_by_profile[rule.access_profile_id].append(rule)
    direct = defaultdict(list)
    if reg_ids:
        from apps.accreditation.models import AssignmentStatus

        for assignment in (
            AccessRuleAssignment.objects.select_related("access_rule")
            .filter(registration_id__in=reg_ids, status=AssignmentStatus.CURRENT)
            .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))
        ):
            if assignment.access_rule.event_edition_id == event.pk:
                direct[assignment.registration_id].append(
                    (assignment.access_rule, assignment.effective_from, assignment.effective_until)
                )
    restrictions = _restriction_rows(event, registration_by_id, now=now, until=bands.expires_at)
    last_admitted = (
        dict(
            EntryEvent.objects.filter(
                registration_id__in=reg_ids,
                event_type=EntryEventType.ENTRY,
                decision=EntryDecision.ADMIT,
            )
            .values("registration_id")
            .annotate(last=Max("occurred_at"))
            .values_list("registration_id", "last")
        )
        if reg_ids
        else {}
    )
    # A synchronized offline admission kept only in its SyncOperation (no
    # Entry Event) is prior admission evidence too (Phase 4 Prompt 3
    # correction 5, decision D3 option S1), so the device's single-entry rule
    # and re-entry advisory see it.
    from apps.entry.selectors.admissions import last_unlinked_admission_times

    for registration_id, moment in last_unlinked_admission_times(
        reg_ids, event_edition_id=event.pk
    ).items():
        known = last_admitted.get(registration_id)
        if known is None or moment > known:
            last_admitted[registration_id] = moment

    entries = []
    for credential in passes:
        registration = credential.registration
        badge = badges.get(registration.pk)
        access = accesses.get(registration.pk)
        if badge is None or access is None:
            continue
        # A pass whose snapshot no longer matches the current assignments
        # would be STALE online: leave it out (a local miss routes to Manual
        # Review), never ship a context the server would not admit.
        if (
            credential.badge_assignment_id != badge.pk
            or credential.access_assignment_id != access.pk
        ):
            continue
        profile = access.access_profile
        if not profile.is_active:
            continue
        zone_rules = _zone_rules(
            rules=rules_by_profile.get(profile.pk, []),
            direct_rules=direct.get(registration.pk, []),
            zone_ancestors=zone_ancestors,
            gate_id=scope.gate_id,
        )
        if not any(r["effect"] == "ALLOW" for z in zone_rules for r in z["rules"]):
            continue  # not admissible anywhere this device operates
        badge_type = badge.badge_type
        until_values = [v for v in (badge.effective_until, access.effective_until) if v is not None]
        entries.append(
            {
                "jti": credential.jti,
                "pid": credential.participant_event_pseudonym,
                "bai": credential.badge_assignment_public_reference,
                "cv": credential.credential_version,
                "apc": credential.access_profile_code,
                "btc": credential.badge_type_code,
                "valid_from": _epoch(credential.valid_from),
                "valid_until": _epoch(credential.valid_until),
                "display_name": registration.person.display_name if registration.person else "",
                "badge_label": {
                    "en": badge_type.name,
                    "fr": badge_type.name_fr or badge_type.name,
                    "ar": badge_type.name_ar or badge_type.name,
                },
                "assignment_until": _epoch(min(until_values)) if until_values else None,
                "profile_window": {
                    "from": _epoch(profile.valid_from),
                    "until": _epoch(profile.valid_until),
                },
                "reentry": profile.reentry_policy,
                "zone_rules": zone_rules,
                "restrictions": restrictions.get(registration.pk, []),
                "last_admitted_at": _epoch(last_admitted.get(registration.pk)),
            }
        )
    if len(entries) > settings.ENTRY_OFFLINE_MAX_PACKAGE_ENTRIES:
        raise OfflineUnavailable("The package would be too large.", code="PACKAGE_TOO_LARGE")

    revoked = [
        {"jti": jti, "status": status}
        for jti, status in DigitalEntryPass.objects.filter(
            event_edition=event, status__in=_NON_ACTIVE_PASS_STATUSES
        )
        .order_by("jti")
        .values_list("jti", "status")
    ]
    qr_keys = [
        {
            "kid": key.key_id,
            "spki": pem_to_spki_b64(key.public_key_pem.encode("ascii")),
            "status": key.status,
            "not_before": _epoch(key.not_before),
            "not_after": _epoch(key.not_after),
        }
        for key in VerificationKey.objects.exclude(status="PENDING").order_by("key_id")
    ]
    override_reasons = [
        {
            "code": reason.code,
            "names": {
                "en": reason.name,
                "fr": reason.name_fr or reason.name,
                "ar": reason.name_ar or reason.name,
            },
            "overridable_reason_codes": sorted(reason.overridable_reason_codes or []),
            "requires_note": bool(reason.requires_note),
        }
        for reason in EntryOverrideReason.objects.filter(
            event_edition=event, is_active=True
        ).order_by("code")
    ]
    body = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": package_public_id,
        "package_version": package_version,
        "device": device.public_id,
        "event": event.code,
        "gate": scope.gate.code,
        "zones": [zone.code for zone in zones],
        "scope_version": scope.scope_version,
        "sensitivity": scope.offline_sensitivity,
        "issued_at": _epoch(data_cutoff_at),
        "data_cutoff_at": _epoch(data_cutoff_at),
        "aging_at": _epoch(bands.aging_at),
        "stale_at": _epoch(bands.stale_at),
        "expires_at": _epoch(bands.expires_at),
        "entries": entries,
        "revoked_passes": revoked,
        "qr_keys": qr_keys,
        "override_reasons": override_reasons,
    }
    validate_package_body(body)
    return body


# ---------------------------------------------------------------------------
# Build lifecycle (independent re-review correction B)
# ---------------------------------------------------------------------------


class _BuildCancelled(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _LeaseLost(Exception):
    """Another worker owns the build now, or it reached a terminal state."""


@dataclass(frozen=True)
class _PreparedPackage:
    public_id: str
    cutoff: object
    bands: object
    entry_count: int
    compact: str
    key_id: str
    manifest: dict
    response: bytes
    watermark: object


def _next_version(device) -> int:
    """Versions are never reused: neither by packages nor by reservations."""
    latest_package = OfflinePackage.objects.filter(device=device).aggregate(
        v=Max("package_version")
    )["v"]
    latest_build = OfflinePackageBuild.objects.filter(device=device).aggregate(
        v=Max("package_version")
    )["v"]
    return max(latest_package or 0, latest_build or 0) + 1


def _build_invalid_reason(device: EntryDevice, build: OfflinePackageBuild, *, now) -> str:
    """Why `build` may no longer produce a package for `device`, or ""."""
    from apps.entry.services.devices import current_scope

    scope = current_scope(device)
    reason = offline_unavailability(device, scope=scope, now=now)
    if reason:
        return reason
    if scope.pk != build.scope_id:
        return "SCOPE_CHANGED"
    recipient = active_device_key(device, DeviceKeyPurpose.PACKAGE_UNWRAP)
    if recipient is None or active_device_key(device, DeviceKeyPurpose.OPERATION_SIGNING) is None:
        return "DEVICE_NOT_PREPARED"
    if recipient.pk != build.recipient_key_id:
        return "REPROVISIONED"
    return ""


def _dispatch_build(build_id) -> None:
    """Enqueue the build once the current transaction commits. With the
    local eager broker substitute (ADR-0009) Celery runs the task in-process
    right after the commit; with a real broker a worker runs it."""
    from apps.entry.tasks import build_offline_package_task

    transaction.on_commit(lambda: build_offline_package_task.delay(str(build_id)))


def _finish(build: OfflinePackageBuild, status: str, *, reason: str, now) -> None:
    build.status = status
    build.status_reason_code = reason[:32]
    build.finished_at = now
    build.lease_token = None
    build.lease_until = None
    build.save(
        update_fields=[
            "status",
            "status_reason_code",
            "finished_at",
            "lease_token",
            "lease_until",
        ]
    )
    if status != OfflinePackageBuildStatus.READY:
        audit(
            action_code=action_codes.OFFLINE_PACKAGE_BUILD_ENDED,
            target_type="EntryDevice",
            target_uuid=build.device_id,
            event_edition_id=build.event_edition_id,
            result="DENIED" if status == OfflinePackageBuildStatus.CANCELLED else "FAILED",
            reason_code=reason[:32],
            actor_type="SYSTEM",
            after_summary={"package_version": build.package_version, "status": status},
        )


def request_package_build(
    device: EntryDevice, *, reason: str, now=None, seen_version: int | None = None
) -> OfflinePackageBuild:
    """The device's ONE open build for its current scope and key, created if
    there is none. Idempotent and serialized by the device row lock (plus a
    partial unique index): concurrent callers converge on the same row and
    the same reserved version. Never builds in the caller's thread.

    `seen_version` is the package the caller saw before taking the lock
    (None: no package). If a NEWER current package committed meanwhile, its
    READY build is returned instead of reserving yet another version."""
    from apps.entry.services.devices import current_scope

    now = now or timezone.now()
    with transaction.atomic():
        locked = (
            EntryDevice.objects.select_for_update()
            .select_related("event_edition")
            .get(pk=device.pk)
        )
        scope = current_scope(locked)
        unavailable = offline_unavailability(locked, scope=scope, now=now)
        if unavailable:
            raise OfflineUnavailable("Offline data is not available.", code=unavailable)
        recipient = active_device_key(locked, DeviceKeyPurpose.PACKAGE_UNWRAP)
        if (
            recipient is None
            or active_device_key(locked, DeviceKeyPurpose.OPERATION_SIGNING) is None
        ):
            raise OfflineUnavailable("This device is not prepared.", code="DEVICE_NOT_PREPARED")
        latest = current_package(locked, now=now)
        if (
            latest is not None
            and latest.recipient_key_id == recipient.pk
            and (seen_version is None or latest.package_version > seen_version)
        ):
            ready = OfflinePackageBuild.objects.filter(package=latest).first()
            if ready is not None:
                return ready  # overtaken by a build that just committed
        current = (
            OfflinePackageBuild.objects.select_for_update()
            .filter(device=locked, status__in=OPEN_BUILD_STATUSES)
            .first()
        )
        if current is not None:
            if current.scope_id == scope.pk and current.recipient_key_id == recipient.pk:
                lease_live = current.lease_until is not None and current.lease_until > now
                if not lease_live:
                    _dispatch_build(current.pk)  # PENDING, or an abandoned lease
                return current
            _finish(current, OfflinePackageBuildStatus.CANCELLED, reason="SUPERSEDED", now=now)
        build = OfflinePackageBuild.objects.create(
            device=locked,
            event_edition=locked.event_edition,
            scope=scope,
            recipient_key=recipient,
            package_version=_next_version(locked),
            request_reason=reason[:32],
            requested_at=now,
        )
        audit(
            action_code=action_codes.OFFLINE_PACKAGE_BUILD_REQUESTED,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            reason_code=reason[:32],
            actor_type="SYSTEM",
            after_summary={
                "device": locked.public_id,
                "package_version": build.package_version,
                "scope_version": scope.scope_version,
            },
        )
        _dispatch_build(build.pk)
    return build


def _prepare_package_bytes(*, build, device, scope, recipient, now) -> _PreparedPackage:
    """Project, validate, encrypt and sign -- no lock held, nothing stored.

    The watermark is captured in the SAME transaction and BEFORE the first
    projection query, so every change it does not cover reaches the next
    delta (`apps.entry.services.offline_journal`)."""
    from apps.core.crypto.package_signing import get_package_signing_key_provider

    provider = get_package_signing_key_provider()
    public_id = new_public_id()
    with transaction.atomic():
        watermark = capture_watermark()
        cutoff = now or timezone.now()
        try:
            bands = package_bands(
                data_cutoff_at=cutoff,
                sensitivity=scope.offline_sensitivity,
                event_timezone=device.event_edition.timezone,
                device_expires_at=device.expires_at,
            )
        except ValidityWindowError as exc:
            raise OfflineUnavailable(
                "The package would expire at once.", code="NO_VALIDITY"
            ) from exc
        body = project_package_body(
            device=device,
            scope=scope,
            package_public_id=public_id,
            package_version=build.package_version,
            bands=bands,
            data_cutoff_at=cutoff,
        )
    entry_count = len(body["entries"])
    ciphertext, enc, wrap = encrypt_for_device(
        canonical_json(body),
        recipient=load_device_public_key(recipient.public_key_spki),
        recipient_fingerprint=recipient.fingerprint,
        device_public_id=device.public_id,
        object_id=public_id,
        kind="OPKG",
    )
    del body  # the plaintext projection is never kept (memory only)
    manifest = {
        "typ": TYP_PACKAGE_MANIFEST,
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": public_id,
        "package_version": build.package_version,
        "device": device.public_id,
        "event": device.event_edition.code,
        "scope_version": scope.scope_version,
        "sensitivity": scope.offline_sensitivity,
        "issued_at": _epoch(cutoff),
        "data_cutoff_at": _epoch(cutoff),
        "aging_at": _epoch(bands.aging_at),
        "stale_at": _epoch(bands.stale_at),
        "expires_at": _epoch(bands.expires_at),
        "entry_count": entry_count,
        "ciphertext_sha256": sha256_hex(ciphertext),
        "ciphertext_length": len(ciphertext),
        "enc": enc,
        "wrap": wrap,
    }
    validate_manifest(manifest, typ=TYP_PACKAGE_MANIFEST)
    compact, key_id = jws_sign(manifest, typ=TYP_PACKAGE_MANIFEST, provider=provider)
    response = canonical_json({"manifest": compact, "ciphertext": b64url_encode(ciphertext)})
    return _PreparedPackage(
        public_id=public_id,
        cutoff=cutoff,
        bands=bands,
        entry_count=entry_count,
        compact=compact,
        key_id=key_id,
        manifest=manifest,
        response=response,
        watermark=watermark,
    )


def _discard_object(storage, object_key: str) -> None:
    """Delete stored ciphertext that no committed package references.

    Best effort: if deletion fails, `pending_object_key` still names the
    object and `cleanup_offline_packages` retries."""
    from apps.documents.storage import ObjectNotFound

    try:
        if OfflinePackage.objects.filter(stored_object_key=object_key).exists():
            return  # the commit succeeded after all (e.g. a lost acknowledgement)
    except Exception:  # noqa: BLE001, S110 - unreadable database: delete anyway
        pass
    try:
        storage.delete(object_key)
    except ObjectNotFound:
        pass
    except Exception:  # noqa: BLE001 - keep the pending key for the sweeper
        return
    try:
        with transaction.atomic():
            OfflinePackageBuild.objects.filter(pending_object_key=object_key).update(
                pending_object_key=""
            )
    except Exception:  # noqa: BLE001, S110 - the sweeper finds nothing to delete
        pass


def _end_attempt(build_id, token, *, status: str | None, reason: str, now=None) -> None:
    """End this worker's attempt: a terminal status, or (status None) release
    the lease so the SAME build and version can be claimed again."""
    now = now or timezone.now()
    try:
        with transaction.atomic():
            build = OfflinePackageBuild.objects.select_for_update().get(pk=build_id)
            if build.status != OfflinePackageBuildStatus.BUILDING or build.lease_token != token:
                return
            if status is None:
                if build.attempts >= settings.ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS:
                    _finish(build, OfflinePackageBuildStatus.FAILED, reason=reason, now=now)
                    return
                build.status = OfflinePackageBuildStatus.PENDING
                build.status_reason_code = reason[:32]
                build.lease_token = None
                build.lease_until = None
                build.save(
                    update_fields=["status", "status_reason_code", "lease_token", "lease_until"]
                )
                return
            _finish(build, status, reason=reason, now=now)
    except Exception:  # noqa: BLE001, S110 - the lease expires on its own
        pass


def run_package_build(build_id, *, now=None) -> OfflinePackage | None:
    """Claim, build and commit ONE requested build. Idempotent and safe to
    run concurrently or repeatedly (Celery redelivery, retries):

    * a READY build returns its package; a terminal one returns None;
    * a build leased to a live worker is left alone;
    * every commit re-locks the device and the build (always in that
      order) and re-validates availability, scope and recipient key, so a
      build overtaken by a block, revocation, re-scope or re-provisioning
      is cancelled, never committed;
    * stored ciphertext that is not committed is deleted, whatever fails --
      including the database commit itself.
    """
    from apps.documents.storage import get_private_storage

    started = now or timezone.now()
    token = uuid.uuid4()
    device_id = (
        OfflinePackageBuild.objects.filter(pk=build_id).values_list("device_id", flat=True).first()
    )
    if device_id is None:
        return None
    with transaction.atomic():
        device = (
            EntryDevice.objects.select_for_update()
            .select_related("event_edition")
            .get(pk=device_id)
        )
        build = (
            OfflinePackageBuild.objects.select_for_update(of=("self",))
            .select_related("scope", "recipient_key", "package")
            .get(pk=build_id)
        )
        if build.status == OfflinePackageBuildStatus.READY:
            return build.package
        if build.status not in OPEN_BUILD_STATUSES:
            return None
        if (
            build.status == OfflinePackageBuildStatus.BUILDING
            and build.lease_until is not None
            and build.lease_until > started
        ):
            return None
        if build.attempts >= settings.ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS:
            _finish(build, OfflinePackageBuildStatus.FAILED, reason="MAX_ATTEMPTS", now=started)
            return None
        invalid = _build_invalid_reason(device, build, now=started)
        if invalid:
            _finish(build, OfflinePackageBuildStatus.CANCELLED, reason=invalid, now=started)
            return None
        build.status = OfflinePackageBuildStatus.BUILDING
        build.attempts += 1
        build.lease_token = token
        build.lease_until = started + timedelta(seconds=settings.ENTRY_OFFLINE_BUILD_LEASE_SECONDS)
        build.started_at = started
        build.save(update_fields=["status", "attempts", "lease_token", "lease_until", "started_at"])
    try:
        prepared = _prepare_package_bytes(
            build=build, device=device, scope=build.scope, recipient=build.recipient_key, now=now
        )
    except OfflineUnavailable as exc:  # too large, no validity window
        _end_attempt(build_id, token, status=OfflinePackageBuildStatus.CANCELLED, reason=exc.code)
        return None
    except Exception:
        _end_attempt(build_id, token, status=None, reason="BUILD_ERROR")
        raise
    storage = get_private_storage()
    object_key = storage.save(io.BytesIO(prepared.response), _CONTENT_TYPE)
    try:
        with transaction.atomic():
            claimed = OfflinePackageBuild.objects.filter(
                pk=build_id, status=OfflinePackageBuildStatus.BUILDING, lease_token=token
            ).update(pending_object_key=object_key)
            if not claimed:
                raise _LeaseLost()
        with transaction.atomic():
            committed_at = now or timezone.now()
            device = (
                EntryDevice.objects.select_for_update()
                .select_related("event_edition")
                .get(pk=device_id)
            )
            build = OfflinePackageBuild.objects.select_for_update().get(pk=build_id)
            if build.status != OfflinePackageBuildStatus.BUILDING or build.lease_token != token:
                raise _LeaseLost()
            invalid = _build_invalid_reason(device, build, now=committed_at)
            if invalid:
                raise _BuildCancelled(invalid)
            OfflinePackage.objects.filter(device=device, status=OfflinePackageStatus.READY).update(
                status=OfflinePackageStatus.SUPERSEDED,
                status_changed_at=committed_at,
                status_reason_code="SUPERSEDED",
            )
            bands = prepared.bands
            package = OfflinePackage.objects.create(
                public_id=prepared.public_id,
                device=device,
                scope_id=build.scope_id,
                event_edition=device.event_edition,
                package_version=build.package_version,
                scope_version=prepared.manifest["scope_version"],
                schema_version=PACKAGE_SCHEMA_VERSION,
                sensitivity=prepared.manifest["sensitivity"],
                signing_key_id=prepared.key_id,
                recipient_key_id=build.recipient_key_id,
                issued_at=prepared.cutoff,
                data_cutoff_at=prepared.cutoff,
                aging_at=bands.aging_at,
                stale_at=bands.stale_at,
                expires_at=bands.expires_at,
                manifest_sha256=sha256_hex(prepared.compact.encode("ascii")),
                ciphertext_sha256=prepared.manifest["ciphertext_sha256"],
                response_sha256=sha256_hex(prepared.response),
                response_bytes=len(prepared.response),
                entry_count=prepared.entry_count,
                stored_object_key=object_key,
                created_at=committed_at,
                data_snapshot=prepared.watermark.snapshot,
                journal_xmin=prepared.watermark.xmin,
                build_txid=prepared.watermark.txid,
                journal_high_id=prepared.watermark.high_id,
            )
            build.status = OfflinePackageBuildStatus.READY
            build.package = package
            build.pending_object_key = ""
            build.status_reason_code = ""
            build.finished_at = committed_at
            build.lease_token = None
            build.lease_until = None
            build.save(
                update_fields=[
                    "status",
                    "package",
                    "pending_object_key",
                    "status_reason_code",
                    "finished_at",
                    "lease_token",
                    "lease_until",
                ]
            )
            audit(
                action_code=action_codes.OFFLINE_PACKAGE_BUILT,
                target_type="OfflinePackage",
                target_uuid=package.pk,
                event_edition_id=device.event_edition_id,
                actor_type="SYSTEM",
                after_summary={
                    "device": device.public_id,
                    "package_version": package.package_version,
                    "scope_version": package.scope_version,
                    "entries": package.entry_count,
                    "response_sha256": package.response_sha256,
                    "expires_at": bands.expires_at.isoformat(),
                    "attempt": build.attempts,
                },
            )
    except _BuildCancelled as exc:
        _discard_object(storage, object_key)
        _end_attempt(build_id, token, status=OfflinePackageBuildStatus.CANCELLED, reason=exc.code)
        return None
    except _LeaseLost:
        _discard_object(storage, object_key)
        return None
    except BaseException:
        # Includes a failure of the COMMIT itself: nothing references the
        # object unless the commit really happened (checked first).
        _discard_object(storage, object_key)
        _end_attempt(build_id, token, status=None, reason="COMMIT_FAILED")
        raise
    return package


def build_package(*, device: EntryDevice, now=None, reason: str = "MANUAL") -> OfflinePackage:
    """Request and run a build to completion in the caller (operations and
    tests only; the download API never calls this)."""
    existing = current_package(device, now=now)
    build = request_package_build(
        device,
        reason=reason,
        now=now,
        seen_version=existing.package_version if existing is not None else None,
    )
    package = run_package_build(build.pk, now=now)
    if package is None:
        build.refresh_from_db()
        if build.status == OfflinePackageBuildStatus.READY:
            return build.package
        raise OfflineUnavailable(
            "Offline data is not available.", code=build.status_reason_code or "BUILD_PENDING"
        )
    return package


# ---------------------------------------------------------------------------
# Retrieval (authorized, rate-limited, audited, byte-identical)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageDownload:
    """What `POST /entry/api/v1/offline/package/` answers."""

    status: str
    package: OfflinePackage | None = None
    raw: bytes | None = None
    build: OfflinePackageBuild | None = None
    retry_after_seconds: int = 0


def current_package(device: EntryDevice, *, now=None) -> OfflinePackage | None:
    """The newest READY, unexpired, journal-watermarked package for the
    device's CURRENT scope."""
    from apps.entry.services.devices import current_scope

    now = now or timezone.now()
    scope = current_scope(device)
    if scope is None:
        return None
    return (
        OfflinePackage.objects.filter(
            device=device,
            scope=scope,
            status=OfflinePackageStatus.READY,
            expires_at__gt=now,
            stored_object_deleted_at__isnull=True,
        )
        .exclude(data_snapshot="")
        .order_by("-package_version")
        .first()
    )


def _enforce_download_budget(device: EntryDevice, *, now) -> None:
    from apps.audit.models import AuditEvent

    since = now - timedelta(seconds=settings.ENTRY_OFFLINE_DOWNLOAD_WINDOW_SECONDS)
    used = AuditEvent.objects.filter(
        target_type="EntryDevice",
        target_uuid=device.pk,
        action_code__in=(
            action_codes.OFFLINE_PACKAGE_DOWNLOADED,
            action_codes.OFFLINE_PACKAGE_BUILD_REQUESTED,
            action_codes.OFFLINE_DELTA_ISSUED,
        ),
        occurred_at__gte=since,
    ).count()
    if used >= settings.ENTRY_OFFLINE_DOWNLOADS_PER_WINDOW:
        _deny(device, "RATE_LIMITED")
        raise OfflineUnavailable("Too many offline data requests.", code="RATE_LIMITED")


def _deny(device, code: str) -> None:
    with transaction.atomic():
        audit(
            action_code=action_codes.OFFLINE_PACKAGE_DOWNLOAD_DENIED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            result="DENIED",
            reason_code=code,
            actor_type="SYSTEM",
        )


def _read_stored(package: OfflinePackage) -> bytes | None:
    """The stored response bytes, or None if missing or not byte-identical."""
    from apps.documents.storage import PrivateStorageError, get_private_storage

    try:
        with get_private_storage().open(package.stored_object_key) as handle:
            raw = handle.read()
    except PrivateStorageError:
        return None
    if sha256_hex(raw) != package.response_sha256:
        return None
    return raw


def _mark_unusable(package: OfflinePackage, *, reason: str, now) -> None:
    OfflinePackage.objects.filter(pk=package.pk, status=OfflinePackageStatus.READY).update(
        status=OfflinePackageStatus.REVOKED, status_changed_at=now, status_reason_code=reason
    )


def download_package(
    *, device: EntryDevice, have_version: object, nonce, signature, body: bytes, now=None
) -> PackageDownload:
    """Serve the newest READY package, or answer BUILDING.

    Never builds in the request. A missing, unreadable or rebuild-blocked
    package yields BUILDING (after recording the device's one open build);
    a merely old package, or one whose delta would require a non-blocking
    rebuild, is still served while its successor builds.
    """
    now = now or timezone.now()
    try:
        verify_device_proof(
            device, purpose="package", nonce=nonce, signature=signature, body=body, now=now
        )
    except OfflineRequestRejected as exc:
        record_request_rejection(device, purpose="package", code=exc.code)
        raise
    reason = offline_unavailability(device, now=now)
    if reason:
        _deny(device, reason)
        raise OfflineUnavailable("Offline data is not available.", code=reason)
    _enforce_download_budget(device, now=now)
    package = current_package(device, now=now)
    raw = None
    if package is not None:
        raw = _read_stored(package)
        if raw is None:
            _mark_unusable(package, reason="STORED_OBJECT_UNAVAILABLE", now=now)
            package = None
    request_reason = ""
    serve = package is not None
    if package is None:
        request_reason = "NO_PACKAGE"
    else:
        rebuild = journal_changes(package).rebuild_reasons
        if rebuild & REBUILD_BLOCKING_REASONS:
            request_reason, serve = "REBUILD_REQUIRED", False
        elif rebuild:
            request_reason = "REBUILD_REQUIRED"
        elif now - package.data_cutoff_at >= timedelta(
            seconds=settings.ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS
        ):
            request_reason = "REFRESH"
    build = None
    if request_reason:
        build = request_package_build(
            device,
            reason=request_reason,
            now=now,
            seen_version=package.package_version if package is not None else None,
        )
        if build.status == OfflinePackageBuildStatus.READY and build.package_id is not None:
            newer = OfflinePackage.objects.get(pk=build.package_id)
            newer_raw = _read_stored(newer)
            if newer_raw is not None:
                package, raw, serve = newer, newer_raw, True
    if not serve:
        return PackageDownload(
            status=PACKAGE_RESPONSE_BUILDING,
            build=build,
            retry_after_seconds=settings.ENTRY_OFFLINE_BUILD_RETRY_AFTER_SECONDS,
        )
    not_modified = isinstance(have_version, int) and have_version == package.package_version
    with transaction.atomic():
        attempt = package.download_count + 1
        OfflinePackage.objects.filter(pk=package.pk).update(
            download_count=attempt,
            first_downloaded_at=package.first_downloaded_at or now,
            last_downloaded_at=now,
        )
        audit(
            action_code=action_codes.OFFLINE_PACKAGE_DOWNLOADED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            actor_type="SYSTEM",
            after_summary={
                "package_version": package.package_version,
                "attempt": attempt,
                "not_modified": not_modified,
                "response_sha256": package.response_sha256,
            },
        )
    if not_modified:
        return PackageDownload(status=PACKAGE_RESPONSE_NOT_MODIFIED, package=package)
    return PackageDownload(status=PACKAGE_RESPONSE_READY, package=package, raw=raw)


# ---------------------------------------------------------------------------
# Critical delta (binding decision P2-B; independent re-review correction C)
# ---------------------------------------------------------------------------


def delta_body(*, package: OfflinePackage, delta_version: int, now) -> dict:
    """The risk-increasing changes the package does not already reflect.

    Driven by the change journal (`journal_changes`), never by `updated_at`:
    every journaled change the package snapshot did not cover names the
    targets to re-state from CURRENT authoritative state. Covered, exactly:

    * pass revocation, replacement, suspension and expiry (every non-active
      pass of the event), and any other change to an active pass
      (withdrawn: PASS_CHANGED);
    * the eligibility of EVERY active pass (withdrawn:
      REGISTRATION_NOT_APPROVED);
    * restriction changes (the CURRENT restriction set of every affected
      context; the device merges monotonically, so a lifted restriction
      stays in force until the next full rebuild);
    * verification-key revocation (every revoked key);
    * access changes that could remove admission: a changed assignment, a
      changed direct rule (per context) or a changed profile or profile rule
      (whole Access Profiles by code).

    Anything a delta cannot represent sets `rebuild_required` with its
    reason; the fail-closed reasons (`REBUILD_BLOCKING_REASONS`) Block the
    device offline until it holds a new package.
    """
    from apps.accreditation.models import AccessProfile, AccessRuleAssignment
    from apps.badges.models import DigitalEntryPass, VerificationKey
    from apps.entry.services.devices import current_scope
    from apps.entry.services.verification import eligible_contexts_queryset

    event = package.event_edition
    changes = journal_changes(package)
    rebuild_reasons = set(changes.rebuild_reasons)
    passes = [
        {"jti": jti, "status": status}
        for jti, status in DigitalEntryPass.objects.filter(
            event_edition=event, status__in=_NON_ACTIVE_PASS_STATUSES
        )
        .order_by("jti")
        .values_list("jti", "status")
    ]
    active = DigitalEntryPass.objects.filter(event_edition=event, status="ACTIVE")
    withdrawn: dict[str, str] = {}
    for jti in active.exclude(registration__in=eligible_contexts_queryset(event)).values_list(
        "jti", flat=True
    ):
        withdrawn[jti] = "REGISTRATION_NOT_APPROVED"
    if changes.pass_ids:
        rows = list(
            DigitalEntryPass.objects.filter(pk__in=changes.pass_ids).values_list(
                "jti", "status", "event_edition_id"
            )
        )
        if len(rows) < len(changes.pass_ids):
            rebuild_reasons.add("UNTRACKED_CHANGE")
        for jti, status, event_id in rows:
            if event_id == event.pk and status == "ACTIVE":
                withdrawn.setdefault(jti, "PASS_CHANGED")
    if changes.assignment_registration_ids:
        for jti in active.filter(
            registration_id__in=changes.assignment_registration_ids
        ).values_list("jti", flat=True):
            withdrawn.setdefault(jti, "ASSIGNMENT_CHANGED")
    if changes.rule_ids:
        direct_rule_regs = AccessRuleAssignment.objects.filter(
            access_rule_id__in=changes.rule_ids
        ).values_list("registration_id", flat=True)
        for jti in active.filter(registration_id__in=direct_rule_regs).values_list(
            "jti", flat=True
        ):
            withdrawn.setdefault(jti, "ACCESS_RULE_CHANGED")
    profiles: set[str] = set()
    if changes.profile_ids:
        rows = list(
            AccessProfile.objects.filter(pk__in=changes.profile_ids).values_list(
                "code", "event_edition_id"
            )
        )
        if len(rows) < len(changes.profile_ids):
            rebuild_reasons.add("UNTRACKED_CHANGE")
        profiles = {code for code, event_id in rows if event_id == event.pk}

    affected = list(
        active.select_related("registration").filter(
            Q(registration_id__in=changes.restriction_registration_ids)
            | Q(registration__person_id__in=changes.restriction_person_ids)
        )
    )
    registration_by_id = {p.registration_id: p.registration for p in affected}
    current_restrictions = _restriction_rows(
        event, registration_by_id, now=now, until=package.expires_at
    )
    restriction_items = [
        {"jti": p.jti, "restrictions": current_restrictions.get(p.registration_id, [])}
        for p in sorted(affected, key=lambda p: p.jti)
        if current_restrictions.get(p.registration_id)
    ]
    revoked_kids = sorted(
        VerificationKey.objects.filter(status="REVOKED").values_list("key_id", flat=True)
    )
    if changes.key_ids:
        statuses = list(
            VerificationKey.objects.filter(pk__in=changes.key_ids).values_list("status", flat=True)
        )
        if len(statuses) < len(changes.key_ids) or any(s != "REVOKED" for s in statuses):
            rebuild_reasons.add("KEY_SET_CHANGED")
    scope = current_scope(package.device)
    if scope is None or scope.pk != package.scope_id:
        rebuild_reasons.add("SCOPE_CHANGED")
    body = {
        "schema_version": DELTA_SCHEMA_VERSION,
        "package_id": package.public_id,
        "package_version": package.package_version,
        "delta_version": delta_version,
        "device": package.device.public_id,
        "scope_version": package.scope_version,
        "issued_at": _epoch(now),
        "critical_delta_cutoff_at": _epoch(now),
        "passes": passes,
        "restrictions": restriction_items,
        "access_withdrawn": [
            {"jti": jti, "reason": reason} for jti, reason in sorted(withdrawn.items())
        ],
        "withdrawn_access_profiles": sorted(profiles),
        "revoked_kids": revoked_kids,
        "rebuild_required": bool(rebuild_reasons),
        "rebuild_reasons": sorted(rebuild_reasons),
    }
    validate_delta_body(body)
    return body


def _delta_refusal(device: EntryDevice, package: OfflinePackage | None, *, now) -> str:
    """Re-validated UNDER the device and package row locks: never a delta for
    a package revoked, superseded, expired, blocked, re-scoped or
    re-provisioned concurrently."""
    from apps.entry.services.devices import current_scope

    reason = offline_unavailability(device, now=now)
    if reason:
        return reason
    if (
        package is None
        or package.status != OfflinePackageStatus.READY
        or package.expires_at <= now
        or package.stored_object_deleted_at is not None
    ):
        return "PACKAGE_NOT_CURRENT"
    scope = current_scope(device)
    if scope is None or scope.pk != package.scope_id:
        return "PACKAGE_NOT_CURRENT"
    recipient = active_device_key(device, DeviceKeyPurpose.PACKAGE_UNWRAP)
    if recipient is None or recipient.pk != package.recipient_key_id:
        return "PACKAGE_NOT_CURRENT"
    return ""


def issue_delta(
    *, device: EntryDevice, package_public_id: object, nonce, signature, body: bytes, now=None
) -> tuple[OfflineCriticalDelta, bytes]:
    """Build, encrypt and sign a NEW critical delta for the device's package."""
    from apps.core.crypto.package_signing import get_package_signing_key_provider

    now = now or timezone.now()
    try:
        verify_device_proof(
            device, purpose="delta", nonce=nonce, signature=signature, body=body, now=now
        )
    except OfflineRequestRejected as exc:
        record_request_rejection(device, purpose="delta", code=exc.code)
        raise
    reason = offline_unavailability(device, now=now)
    if reason:
        _deny(device, reason)
        raise OfflineUnavailable("Offline data is not available.", code=reason)
    _enforce_download_budget(device, now=now)
    refusal = ""
    with transaction.atomic():
        # Lock order: device, then package (as every lifecycle path does).
        locked = (
            EntryDevice.objects.select_for_update()
            .select_related("event_edition")
            .get(pk=device.pk)
        )
        package = (
            OfflinePackage.objects.select_for_update()
            .select_related("device", "event_edition", "recipient_key")
            .filter(device=locked, public_id=package_public_id)
            .first()
            if isinstance(package_public_id, str)
            else None
        )
        refusal = _delta_refusal(locked, package, now=now)
        if not refusal:
            recipient = package.recipient_key
            latest = package.deltas.aggregate(v=Max("delta_version"))["v"] or 0
            delta_version = latest + 1
            body_dict = delta_body(package=package, delta_version=delta_version, now=now)
            delta_id = f"{package.public_id}.d{delta_version}"
            ciphertext, enc, wrap = encrypt_for_device(
                canonical_json(body_dict),
                recipient=load_device_public_key(recipient.public_key_spki),
                recipient_fingerprint=recipient.fingerprint,
                device_public_id=locked.public_id,
                object_id=delta_id,
                kind="ODELTA",
            )
            counts = {
                "passes": len(body_dict["passes"]),
                "restrictions": len(body_dict["restrictions"]),
                "withdrawn": len(body_dict["access_withdrawn"]),
                "profiles": len(body_dict["withdrawn_access_profiles"]),
                "revoked_kids": len(body_dict["revoked_kids"]),
            }
            rebuild_required = body_dict["rebuild_required"]
            rebuild_reasons = body_dict["rebuild_reasons"]
            del body_dict
            manifest = {
                "typ": TYP_DELTA_MANIFEST,
                "schema_version": DELTA_SCHEMA_VERSION,
                "package_id": package.public_id,
                "package_version": package.package_version,
                "delta_version": delta_version,
                "device": locked.public_id,
                "scope_version": package.scope_version,
                "issued_at": _epoch(now),
                "critical_delta_cutoff_at": _epoch(now),
                "ciphertext_sha256": sha256_hex(ciphertext),
                "ciphertext_length": len(ciphertext),
                "enc": enc,
                "wrap": wrap,
            }
            validate_manifest(manifest, typ=TYP_DELTA_MANIFEST)
            compact, _kid = jws_sign(
                manifest, typ=TYP_DELTA_MANIFEST, provider=get_package_signing_key_provider()
            )
            response = canonical_json(
                {"manifest": compact, "ciphertext": b64url_encode(ciphertext)}
            )
            delta = OfflineCriticalDelta.objects.create(
                package=package,
                device=locked,
                delta_version=delta_version,
                issued_at=now,
                critical_delta_cutoff_at=now,
                manifest_sha256=sha256_hex(compact.encode("ascii")),
                response_sha256=sha256_hex(response),
                counts=counts,
                rebuild_required=rebuild_required,
            )
            audit(
                action_code=action_codes.OFFLINE_DELTA_ISSUED,
                target_type="EntryDevice",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                actor_type="SYSTEM",
                after_summary={
                    "package_version": package.package_version,
                    "delta_version": delta_version,
                    "rebuild_required": rebuild_required,
                    "rebuild_reasons": rebuild_reasons,
                    **counts,
                },
            )
    if refusal:
        _deny(device, refusal)
        if refusal == "PACKAGE_NOT_CURRENT":
            raise OfflineUnavailable("The package is not current.", code=refusal)
        raise OfflineUnavailable("Offline data is not available.", code=refusal)
    return delta, response


# ---------------------------------------------------------------------------
# Revocation and cleanup (package data only -- never device evidence)
# ---------------------------------------------------------------------------


def revoke_device_packages(device: EntryDevice, *, reason: str, now=None) -> int:
    """Revoke READY packages and cancel open builds. Caller holds the device
    row lock (every lifecycle path goes through `withdraw_offline_readiness`)."""
    now = now or timezone.now()
    count = OfflinePackage.objects.filter(device=device, status=OfflinePackageStatus.READY).update(
        status=OfflinePackageStatus.REVOKED, status_changed_at=now, status_reason_code=reason[:32]
    )
    cancelled = OfflinePackageBuild.objects.filter(
        device=device, status__in=OPEN_BUILD_STATUSES
    ).update(
        status=OfflinePackageBuildStatus.CANCELLED,
        status_reason_code=reason[:32],
        finished_at=now,
        lease_token=None,
        lease_until=None,
    )
    if count or cancelled:
        audit(
            action_code=action_codes.OFFLINE_PACKAGE_REVOKED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            reason_code=reason,
            actor_type="SYSTEM",
            after_summary={"packages": count, "builds_cancelled": cancelled},
        )
    return count


def cleanup_offline_packages(*, now=None, limit: int = 500) -> dict[str, int]:
    """Idempotent sweep: expire lapsed packages, revoke packages of closed or
    disabled events, release abandoned build leases, delete orphaned build
    ciphertext, purge retained ciphertext past its retention, prune the
    change journal and expired request nonces. Package metadata rows are
    never deleted, and no device evidence exists on the server to delete."""
    from apps.documents.storage import ObjectNotFound, get_private_storage
    from apps.entry.services.offline_devices import (
        CLOSED_EVENT_STATUSES,
        withdraw_offline_readiness,
    )

    now = now or timezone.now()
    storage = get_private_storage()
    expired = OfflinePackage.objects.filter(
        status=OfflinePackageStatus.READY, expires_at__lte=now
    ).update(
        status=OfflinePackageStatus.EXPIRED, status_changed_at=now, status_reason_code="EXPIRED"
    )
    closed = 0
    for device in EntryDevice.objects.filter(
        event_edition__status__in=CLOSED_EVENT_STATUSES,
        packages__status=OfflinePackageStatus.READY,
    ).distinct()[:limit]:
        with transaction.atomic():
            locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
            withdraw_offline_readiness(locked, reason="EVENT_CLOSED", now=now)
            locked.save()
            closed += 1
    abandoned = 0
    for build in OfflinePackageBuild.objects.filter(
        status=OfflinePackageBuildStatus.BUILDING, lease_until__lte=now
    )[:limit]:
        _end_attempt(build.pk, build.lease_token, status=None, reason="LEASE_EXPIRED", now=now)
        abandoned += 1
    orphans = 0
    for build in (
        OfflinePackageBuild.objects.exclude(pending_object_key="")
        .exclude(status=OfflinePackageBuildStatus.BUILDING, lease_until__gt=now)
        .order_by("requested_at")[:limit]
    ):
        _discard_object(storage, build.pending_object_key)
        orphans += 1
    retention = timedelta(seconds=settings.ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS)
    purged = 0
    for package in OfflinePackage.objects.filter(
        stored_object_deleted_at__isnull=True,
        status__in=(
            OfflinePackageStatus.SUPERSEDED,
            OfflinePackageStatus.EXPIRED,
            OfflinePackageStatus.REVOKED,
        ),
        status_changed_at__lte=now - retention,
    )[:limit]:
        try:
            storage.delete(package.stored_object_key)
        except ObjectNotFound:
            pass
        with transaction.atomic():
            OfflinePackage.objects.filter(pk=package.pk).update(stored_object_deleted_at=now)
            audit(
                action_code=action_codes.OFFLINE_PACKAGE_CIPHERTEXT_PURGED,
                target_type="OfflinePackage",
                target_uuid=package.pk,
                event_edition_id=package.event_edition_id,
                actor_type="SYSTEM",
                after_summary={"package_version": package.package_version},
            )
        purged += 1
    journal = prune_journal(now=now)
    nonce_cutoff = now - timedelta(seconds=2 * settings.ENTRY_OFFLINE_NONCE_SECONDS)
    nonces, _ = OfflineRequestNonce.objects.filter(used_at__lt=nonce_cutoff).delete()
    return {
        "expired": expired,
        "closed_devices": closed,
        "abandoned_builds": abandoned,
        "orphans": orphans,
        "purged": purged,
        "journal": journal,
        "nonces": nonces,
    }
