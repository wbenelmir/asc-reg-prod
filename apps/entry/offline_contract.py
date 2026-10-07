"""Offline Package wire contract: schema versions, field allow-lists,
validity bands, and the operational state/action matrix (Phase 4 Prompt 2,
ADR-0023, `docs/security/offline_package_contract.md`), plus -- from Phase 4
Prompt 3 (ADR-0024, `docs/security/offline_sync_contract.md`) -- the signed
offline OPERATION, the signed server ACKNOWLEDGEMENT, and version 2 of the
operator grant, which binds the checkpoint (gate and zone).

This module is the single source of truth for WHAT may cross into a device.
The package builder calls `validate_package_body` on every body before it
encrypts anything, so an undeclared field -- at any depth -- fails the build
instead of reaching a device (binding decision P2-E). The same allow-lists
are asserted by `apps/entry/tests/test_offline_contract.py`.

Deliberately absent from every allow-list, and therefore impossible to ship:
NIN / passport values or any digest, fingerprint or lookup key of them, the
registration reference, masked identity hints, email, phone, documents,
notes, restriction reasons or categories, Participant Role, and photos. QR
(`jti`) is the only offline participant lookup key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

#: Schema versions a device accepts. A device refuses any other value.
PACKAGE_SCHEMA_VERSION = 1
DELTA_SCHEMA_VERSION = 1
#: Version 2 (Phase 4 Prompt 3) adds the checkpoint binding -- `event`,
#: `gate`, `zone`, `scope_version` -- the offline verifier needs. A device
#: never uses a version-1 grant for offline work (no checkpoint binding).
GRANT_SCHEMA_VERSION = 2
WIPE_ORDER_SCHEMA_VERSION = 1
#: The signed offline operation and the signed acknowledgement (Prompt 3).
OPERATION_SCHEMA_VERSION = 1
ACK_SCHEMA_VERSION = 1

#: JWS `typ` values -- each signed object type is distinct, so a signature
#: over one type can never be replayed as another.
TYP_PACKAGE_MANIFEST = "ASC-OPKG"  # noqa: S105 - a type label, not a secret
TYP_DELTA_MANIFEST = "ASC-ODELTA"  # noqa: S105
TYP_OPERATOR_GRANT = "ASC-OGRANT"  # noqa: S105
TYP_WIPE_ORDER = "ASC-OWIPE"  # noqa: S105
TYP_ACKNOWLEDGEMENT = "ASC-OACK"  # noqa: S105

#: Encryption and key-wrapping algorithm labels written into manifests.
BODY_ENCRYPTION_ALG = "A256GCM"
KEY_WRAP_ALG = "ECDH-ES-P256+HKDF-SHA256+A256GCM"


class OfflineContractError(ValueError):
    """A body or manifest violates the contract. Never carries field values."""


# ---------------------------------------------------------------------------
# Allow-lists (exact key sets at every level)
# ---------------------------------------------------------------------------

LABELS = frozenset({"en", "fr", "ar"})
WINDOW = frozenset({"from", "until"})
RULE = frozenset({"effect", "from", "until"})
ZONE_RULES = frozenset({"zone", "rules"})
RESTRICTION = frozenset({"severity", "overrideable", "from", "until"})

PACKAGE_ENTRY = frozenset(
    {
        "jti",
        "pid",
        "bai",
        "cv",
        "apc",
        "btc",
        "valid_from",
        "valid_until",
        "display_name",
        "badge_label",
        "assignment_until",
        "profile_window",
        "reentry",
        "zone_rules",
        "restrictions",
        "last_admitted_at",
    }
)
REVOKED_PASS = frozenset({"jti", "status"})
QR_KEY = frozenset({"kid", "spki", "status", "not_before", "not_after"})
OVERRIDE_REASON = frozenset({"code", "names", "overridable_reason_codes", "requires_note"})

PACKAGE_BODY = frozenset(
    {
        "schema_version",
        "package_id",
        "package_version",
        "device",
        "event",
        "gate",
        "zones",
        "scope_version",
        "sensitivity",
        "issued_at",
        "data_cutoff_at",
        "aging_at",
        "stale_at",
        "expires_at",
        "entries",
        "revoked_passes",
        "qr_keys",
        "override_reasons",
    }
)

DELTA_RESTRICTIONS = frozenset({"jti", "restrictions"})
DELTA_WITHDRAWN = frozenset({"jti", "reason"})
DELTA_BODY = frozenset(
    {
        "schema_version",
        "package_id",
        "package_version",
        "delta_version",
        "device",
        "scope_version",
        "issued_at",
        "critical_delta_cutoff_at",
        "passes",
        "restrictions",
        "access_withdrawn",
        "withdrawn_access_profiles",
        "revoked_kids",
        "rebuild_required",
        "rebuild_reasons",
    }
)

ENC = frozenset({"alg", "iv", "aad"})
WRAP = frozenset({"alg", "epk", "salt", "iv", "wrapped_key", "recipient"})
PACKAGE_MANIFEST = frozenset(
    {
        "typ",
        "schema_version",
        "package_id",
        "package_version",
        "device",
        "event",
        "scope_version",
        "sensitivity",
        "issued_at",
        "data_cutoff_at",
        "aging_at",
        "stale_at",
        "expires_at",
        "entry_count",
        "ciphertext_sha256",
        "ciphertext_length",
        "enc",
        "wrap",
    }
)
DELTA_MANIFEST = frozenset(
    {
        "typ",
        "schema_version",
        "package_id",
        "package_version",
        "delta_version",
        "device",
        "scope_version",
        "issued_at",
        "critical_delta_cutoff_at",
        "ciphertext_sha256",
        "ciphertext_length",
        "enc",
        "wrap",
    }
)
GRANT_PAYLOAD = frozenset(
    {
        "typ",
        "schema_version",
        "grant_id",
        "device",
        "event",
        "gate",
        "zone",
        "scope_version",
        "display_name",
        "permissions",
        "sensitivity",
        "issued_at",
        "expires_at",
    }
)
WIPE_ORDER_PAYLOAD = frozenset(
    {"typ", "schema_version", "order_id", "device", "reason_code", "issued_at"}
)

#: Exactly the risk-increasing changes a critical delta can carry (binding
#: decision P2-B). Anything else requires a full rebuild.
DELTA_WITHDRAWN_REASONS = frozenset(
    {
        "REGISTRATION_NOT_APPROVED",
        "ASSIGNMENT_CHANGED",
        "ACCESS_RULE_CHANGED",
        "PASS_CHANGED",
        # Attendance days no longer cover the package's day (apps.accreditation.
        # attendance); the device refuses with ATTENDANCE_DAY_NOT_AUTHORIZED.
        "ATTENDANCE_NOT_AUTHORIZED",
    }
)
DELTA_PASS_STATUSES = frozenset({"REVOKED", "REPLACED", "SUSPENDED", "EXPIRED", "INACTIVE"})
DELTA_REBUILD_REASONS = frozenset(
    {
        "SCOPE_CHANGED",
        "CHECKPOINT_CHANGED",
        "OVERRIDE_CATALOGUE_CHANGED",
        "KEY_SET_CHANGED",
        # Fail-closed reasons (independent re-review correction C): a change the
        # journal cannot translate (TRUNCATE, a deleted row the delta would
        # need, an unknown table), more journaled changes than one delta may
        # carry, or a package without a journal watermark.
        "UNTRACKED_CHANGE",
        "BULK_CHANGE",
        "SNAPSHOT_MISSING",
    }
)
#: Rebuild reasons after which the device must not admit on its current
#: package at all: offline it is Blocked (not merely Stale) until a new
#: package is activated.
REBUILD_BLOCKING_REASONS = frozenset(
    {"SCOPE_CHANGED", "UNTRACKED_CHANGE", "BULK_CHANGE", "SNAPSHOT_MISSING"}
)

#: `POST /entry/api/v1/offline/package/` answers with one of these.
PACKAGE_RESPONSE_READY = "READY"
PACKAGE_RESPONSE_NOT_MODIFIED = "NOT_MODIFIED"
PACKAGE_RESPONSE_BUILDING = "BUILDING"


def _check_keys(obj, allowed: frozenset[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise OfflineContractError(f"{where} must be an object.")
    keys = set(obj)
    extra = keys - allowed
    missing = allowed - keys
    if extra:
        # Names of undeclared keys only -- never their values.
        raise OfflineContractError(f"{where} has undeclared fields: {sorted(extra)}")
    if missing:
        raise OfflineContractError(f"{where} is missing fields: {sorted(missing)}")


def _check_list(value, where: str) -> list:
    if not isinstance(value, list):
        raise OfflineContractError(f"{where} must be a list.")
    return value


def _check_scalar(value, where: str) -> None:
    if isinstance(value, dict | list) or isinstance(value, float):
        raise OfflineContractError(f"{where} must be a scalar string, integer, boolean or null.")


def _check_window(value, where: str) -> None:
    _check_keys(value, WINDOW, where)
    for key in WINDOW:
        _check_scalar(value[key], f"{where}.{key}")


def _check_restrictions(values, where: str) -> None:
    for index, item in enumerate(_check_list(values, where)):
        _check_keys(item, RESTRICTION, f"{where}[{index}]")
        if item["severity"] not in ("DENY_ENTRY", "MANUAL_REVIEW"):
            raise OfflineContractError(f"{where}[{index}].severity is invalid.")
        for key in RESTRICTION:
            _check_scalar(item[key], f"{where}[{index}].{key}")


def validate_package_body(body) -> None:
    """Raise `OfflineContractError` unless `body` matches the allow-list exactly."""
    _check_keys(body, PACKAGE_BODY, "package")
    if body["schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise OfflineContractError("package.schema_version is not supported.")
    for key in PACKAGE_BODY - {"zones", "entries", "revoked_passes", "qr_keys", "override_reasons"}:
        _check_scalar(body[key], f"package.{key}")
    for index, zone in enumerate(_check_list(body["zones"], "package.zones")):
        _check_scalar(zone, f"package.zones[{index}]")
    for index, entry in enumerate(_check_list(body["entries"], "package.entries")):
        where = f"package.entries[{index}]"
        _check_keys(entry, PACKAGE_ENTRY, where)
        for key in PACKAGE_ENTRY - {"badge_label", "profile_window", "zone_rules", "restrictions"}:
            _check_scalar(entry[key], f"{where}.{key}")
        _check_keys(entry["badge_label"], LABELS, f"{where}.badge_label")
        for key in LABELS:
            _check_scalar(entry["badge_label"][key], f"{where}.badge_label.{key}")
        _check_window(entry["profile_window"], f"{where}.profile_window")
        for z_index, zone_rules in enumerate(
            _check_list(entry["zone_rules"], f"{where}.zone_rules")
        ):
            z_where = f"{where}.zone_rules[{z_index}]"
            _check_keys(zone_rules, ZONE_RULES, z_where)
            _check_scalar(zone_rules["zone"], f"{z_where}.zone")
            for r_index, rule in enumerate(_check_list(zone_rules["rules"], f"{z_where}.rules")):
                _check_keys(rule, RULE, f"{z_where}.rules[{r_index}]")
                if rule["effect"] not in ("ALLOW", "DENY"):
                    raise OfflineContractError(f"{z_where}.rules[{r_index}].effect is invalid.")
                for key in RULE:
                    _check_scalar(rule[key], f"{z_where}.rules[{r_index}].{key}")
        _check_restrictions(entry["restrictions"], f"{where}.restrictions")
    for index, item in enumerate(_check_list(body["revoked_passes"], "package.revoked_passes")):
        _check_keys(item, REVOKED_PASS, f"package.revoked_passes[{index}]")
        for key in REVOKED_PASS:
            _check_scalar(item[key], f"package.revoked_passes[{index}].{key}")
    for index, item in enumerate(_check_list(body["qr_keys"], "package.qr_keys")):
        _check_keys(item, QR_KEY, f"package.qr_keys[{index}]")
        for key in QR_KEY:
            _check_scalar(item[key], f"package.qr_keys[{index}].{key}")
    for index, item in enumerate(_check_list(body["override_reasons"], "package.override_reasons")):
        where = f"package.override_reasons[{index}]"
        _check_keys(item, OVERRIDE_REASON, where)
        _check_keys(item["names"], LABELS, f"{where}.names")
        for code_index, code in enumerate(
            _check_list(item["overridable_reason_codes"], f"{where}.overridable_reason_codes")
        ):
            _check_scalar(code, f"{where}.overridable_reason_codes[{code_index}]")
        _check_scalar(item["code"], f"{where}.code")
        _check_scalar(item["requires_note"], f"{where}.requires_note")


def validate_delta_body(body) -> None:
    """Raise `OfflineContractError` unless `body` matches the delta allow-list."""
    _check_keys(body, DELTA_BODY, "delta")
    if body["schema_version"] != DELTA_SCHEMA_VERSION:
        raise OfflineContractError("delta.schema_version is not supported.")
    for index, item in enumerate(_check_list(body["passes"], "delta.passes")):
        _check_keys(item, REVOKED_PASS, f"delta.passes[{index}]")
        if item["status"] not in DELTA_PASS_STATUSES:
            raise OfflineContractError(f"delta.passes[{index}].status is invalid.")
    for index, item in enumerate(_check_list(body["restrictions"], "delta.restrictions")):
        _check_keys(item, DELTA_RESTRICTIONS, f"delta.restrictions[{index}]")
        _check_restrictions(item["restrictions"], f"delta.restrictions[{index}].restrictions")
    for index, item in enumerate(_check_list(body["access_withdrawn"], "delta.access_withdrawn")):
        _check_keys(item, DELTA_WITHDRAWN, f"delta.access_withdrawn[{index}]")
        if item["reason"] not in DELTA_WITHDRAWN_REASONS:
            raise OfflineContractError(f"delta.access_withdrawn[{index}].reason is invalid.")
    for key in ("withdrawn_access_profiles", "revoked_kids", "rebuild_reasons"):
        for index, value in enumerate(_check_list(body[key], f"delta.{key}")):
            _check_scalar(value, f"delta.{key}[{index}]")
    for reason in body["rebuild_reasons"]:
        if reason not in DELTA_REBUILD_REASONS:
            raise OfflineContractError("delta.rebuild_reasons contains an invalid reason.")
    if not isinstance(body["rebuild_required"], bool):
        raise OfflineContractError("delta.rebuild_required must be a boolean.")


def validate_manifest(manifest, *, typ: str) -> None:
    allowed = {TYP_PACKAGE_MANIFEST: PACKAGE_MANIFEST, TYP_DELTA_MANIFEST: DELTA_MANIFEST}[typ]
    _check_keys(manifest, allowed, "manifest")
    if manifest["typ"] != typ:
        raise OfflineContractError("manifest.typ is invalid.")
    _check_keys(manifest["enc"], ENC, "manifest.enc")
    _check_keys(manifest["wrap"], WRAP, "manifest.wrap")


# ---------------------------------------------------------------------------
# Signed offline operation (Phase 4 Prompt 3, `offline_sync_contract.md`)
# ---------------------------------------------------------------------------

OPERATION_TYPE_DECISION = "ENTRY_DECISION"
OPERATION_TYPE_ATTEMPT = "VERIFICATION_ATTEMPT"
OPERATION_TYPES = frozenset({OPERATION_TYPE_DECISION, OPERATION_TYPE_ATTEMPT})

OPERATION = frozenset(
    {
        "schema_version",
        "operation_id",
        "type",
        "device",
        "event",
        "store",
        "sequence",
        "prev",
        "occurred_at",
        "device_time",
        "server_offset",
        "state",
        "package",
        "grant",
        "gate",
        "zone",
        "credential",
        "local",
        "decision",
        "decision_reason",
        "override",
    }
)
OPERATION_PACKAGE = frozenset(
    {"id", "version", "delta_version", "band", "data_cutoff_at", "critical_delta_cutoff_at"}
)
OPERATION_CREDENTIAL = frozenset({"jti", "kid"})
OPERATION_LOCAL = frozenset({"code", "result", "reason", "blockers", "advisories", "latency_ms"})
OPERATION_OVERRIDE = frozenset({"code", "note_digest"})

#: The operational states in which a device may RECORD an operation.
RECORDING_STATES = frozenset({"OFFLINE_ACTIVE", "STALE", "EXPIRED"})
OPERATION_BANDS = frozenset({"FRESH", "AGING", "STALE", "EXPIRED"})
#: Offline decisions use the online vocabulary (Flow §11.5: Admit Offline,
#: Do Not Admit, Manual Review = REDIRECTED to the review desk).
OFFLINE_DECISIONS = frozenset({"ADMIT", "DO_NOT_ADMIT", "REDIRECTED"})
OFFLINE_DECISION_REASONS = frozenset(
    {
        "FOLLOWS_RESULT",
        "IDENTITY_MISMATCH",
        "SENT_TO_REVIEW_DESK",
        "SENT_TO_OTHER_CHECKPOINT",
        "PARTICIPANT_LEFT",
    }
)
#: Local results a device can reach offline (the online vocabulary; an
#: unresolved scan is UNSUPPORTED or DENIED without a context).
OFFLINE_LOCAL_RESULTS = frozenset(
    {"ALLOWED", "ALLOWED_WITH_ADVISORY", "MANUAL_REVIEW", "DENIED", "STALE", "UNSUPPORTED"}
)
#: The device's local credential-verification codes (the QR contract codes
#: of `docs/security/qr_contract.md` §5, plus the offline-only ones).
OFFLINE_LOCAL_CODES = frozenset(
    {
        "VALID",
        "MALFORMED",
        "UNSUPPORTED_VERSION",
        "UNKNOWN_KEY",
        "INVALID_KEY",
        "INVALID_SIGNATURE",
        "CREDENTIAL_MISMATCH",
        "WRONG_EVENT",
        "REVOKED",
        "REPLACED",
        "SUSPENDED",
        "INACTIVE",
        "NOT_YET_VALID",
        "EXPIRED",
        "NOT_IN_PACKAGE",
        "PACKAGE_EXPIRED",
    }
)
#: Codes for which the pass signature and `jti` were verified, so an
#: ENTRY_DECISION may name the credential.
RESOLVED_LOCAL_CODES = frozenset(
    {"VALID", "REVOKED", "REPLACED", "SUSPENDED", "INACTIVE", "NOT_YET_VALID", "EXPIRED"}
)
MAX_OPERATION_BYTES = 8 * 1024
MAX_OPERATION_CODES = 16
_UUID4 = "0123456789abcdef"
_B64U = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_CODE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_HEX = frozenset("0123456789abcdef")
MAX_EPOCH = 253402300798  # Leave room for an exclusive one-second query boundary.


def is_client_operation_id(value) -> bool:
    """A canonical lowercase UUID version 4 (`crypto.randomUUID()`): 122
    random bits, so a client id is globally unique and unguessable."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    if any(c not in _UUID4 for part in parts for c in part):
        return False
    return parts[2][0] == "4" and parts[3][0] in "89ab"


def _is_int(value, *, minimum=0, maximum=MAX_EPOCH) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _is_code(value, *, max_length=64, allow_empty=False) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return len(value) <= max_length and all(c in _CODE for c in value)


def _is_b64u(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in _B64U for c in value)


def _is_hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _is_member(value, allowed) -> bool:
    return isinstance(value, str) and value in allowed


def _codes(value, where: str, allowed: frozenset[str] | None = None) -> None:
    if not isinstance(value, list) or len(value) > MAX_OPERATION_CODES:
        raise OfflineContractError(f"{where} must be a short list.")
    for item in value:
        if not _is_code(item, max_length=40) or (allowed is not None and item not in allowed):
            raise OfflineContractError(f"{where} holds an invalid code.")


def validate_operation(op) -> None:
    """Raise `OfflineContractError` unless `op` is a well-formed operation.

    Structure and vocabulary only: whether the device was ENTITLED to record
    it -- its grant, package, band and the pass itself -- is decided by the
    synchronization service against authoritative state."""
    from apps.entry.models import EntryDecisionReason, EntryReasonCode

    _check_keys(op, OPERATION, "operation")
    if type(op["schema_version"]) is not int or op["schema_version"] != OPERATION_SCHEMA_VERSION:
        raise OfflineContractError("operation.schema_version is not supported.")
    if not _is_member(op["type"], OPERATION_TYPES):
        raise OfflineContractError("operation.type is not supported.")
    if not is_client_operation_id(op["operation_id"]):
        raise OfflineContractError("operation.operation_id is not a client operation id.")
    if not _is_b64u(op["device"], 22) or not _is_b64u(op["store"], 22):
        raise OfflineContractError("operation.device or store is invalid.")
    if not _is_code(op["event"], max_length=32) or not _is_code(op["gate"], max_length=64):
        raise OfflineContractError("operation.event or gate is invalid.")
    if op["zone"] is not None and not _is_code(op["zone"], max_length=64):
        raise OfflineContractError("operation.zone is invalid.")
    if not _is_int(op["sequence"], minimum=1, maximum=2**53 - 1) or not _is_hex64(op["prev"]):
        raise OfflineContractError("operation.sequence or prev is invalid.")
    if not _is_int(op["occurred_at"], minimum=1) or not _is_int(op["device_time"], minimum=1):
        raise OfflineContractError("operation times are invalid.")
    if not _is_int(op["server_offset"], minimum=-(2**31), maximum=2**31):
        raise OfflineContractError("operation.server_offset is invalid.")
    if not _is_member(op["state"], RECORDING_STATES):
        raise OfflineContractError("operation.state is invalid.")
    package = op["package"]
    _check_keys(package, OPERATION_PACKAGE, "operation.package")
    if (
        not _is_b64u(package["id"], 22)
        or not _is_int(package["version"], minimum=1, maximum=2**53 - 1)
        or not (
            package["delta_version"] is None
            or _is_int(package["delta_version"], minimum=1, maximum=2**53 - 1)
        )
        or not _is_member(package["band"], OPERATION_BANDS)
        or not _is_int(package["data_cutoff_at"], minimum=1)
        or not _is_int(package["critical_delta_cutoff_at"], minimum=1)
    ):
        raise OfflineContractError("operation.package is invalid.")
    if op["grant"] is not None and not _is_b64u(op["grant"], 22):
        raise OfflineContractError("operation.grant is invalid.")
    credential = op["credential"]
    if credential is not None:
        _check_keys(credential, OPERATION_CREDENTIAL, "operation.credential")
        if not _is_b64u(credential["jti"], 22) or not _is_code(credential["kid"], max_length=8):
            raise OfflineContractError("operation.credential is invalid.")
    local = op["local"]
    _check_keys(local, OPERATION_LOCAL, "operation.local")
    reasons = frozenset(EntryReasonCode.values) - {""}
    if (
        not _is_member(local["code"], OFFLINE_LOCAL_CODES)
        or not _is_member(local["result"], OFFLINE_LOCAL_RESULTS)
        or not (local["reason"] == "" or _is_member(local["reason"], reasons))
        or not _is_int(local["latency_ms"], maximum=600_000)
    ):
        raise OfflineContractError("operation.local is invalid.")
    _codes(local["blockers"], "operation.local.blockers", reasons)
    _codes(local["advisories"], "operation.local.advisories", reasons)
    decision = op["decision"]
    reason = op["decision_reason"]
    if decision is not None and not _is_member(decision, OFFLINE_DECISIONS):
        raise OfflineContractError("operation.decision is invalid.")
    if not (
        reason == ""
        or (_is_member(reason, OFFLINE_DECISION_REASONS) and reason in EntryDecisionReason.values)
    ):
        raise OfflineContractError("operation.decision_reason is invalid.")
    override = op["override"]
    if override is not None:
        _check_keys(override, OPERATION_OVERRIDE, "operation.override")
        if not _is_code(override["code"], max_length=64) or not (
            override["note_digest"] == "" or _is_hex64(override["note_digest"])
        ):
            raise OfflineContractError("operation.override is invalid.")
    if op["type"] == OPERATION_TYPE_DECISION:
        if credential is None or decision is None or local["code"] not in RESOLVED_LOCAL_CODES:
            raise OfflineContractError("A decision must name a verified pass and a decision.")
        if op["grant"] is None or op["zone"] is None:
            raise OfflineContractError("A decision must name its operator grant and zone.")
        if local["result"] == "UNSUPPORTED":
            raise OfflineContractError("A decision needs a resolved local result.")
        if decision == "ADMIT" and reason:
            raise OfflineContractError("An admission carries no non-admission reason.")
        if decision != "ADMIT" and not reason:
            raise OfflineContractError("A non-admission names its reason.")
        if override is not None and decision != "ADMIT":
            raise OfflineContractError("An override is always an admission.")
    else:
        if override is not None or decision == "ADMIT" or decision == "DO_NOT_ADMIT":
            raise OfflineContractError("A verification attempt admits nothing.")
        if decision == "REDIRECTED" and reason != "SENT_TO_REVIEW_DESK":
            raise OfflineContractError("A referral is sent to the review desk.")
        if decision is None and reason:
            raise OfflineContractError("An attempt without a referral has no reason.")


#: Exactly the signed acknowledgement members (`typ` ASC-OACK).
ACKNOWLEDGEMENT = frozenset(
    {
        "typ",
        "schema_version",
        "device",
        "store",
        "operation_id",
        "sequence",
        "payload_hash",
        "status",
        "outcome",
        "conflict_type",
        "processed_at",
    }
)


# ---------------------------------------------------------------------------
# Validity bands (binding decision P2-B)
# ---------------------------------------------------------------------------

BAND_FRESH = "FRESH"
BAND_AGING = "AGING"
BAND_STALE = "STALE"
BAND_EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class Bands:
    aging_at: datetime
    stale_at: datetime
    expires_at: datetime


#: The approved values (binding decision P2-B). `apps.entry.checks` refuses
#: any configured validity value above these maxima, and any health or
#: inactivity value that differs from them.
APPROVED_VALIDITY_MAXIMA: dict[str, dict[str, int]] = {
    "STANDARD": {
        "aging_after_seconds": 15 * 60,
        "stale_after_seconds": 2 * 60 * 60,
        "expires_after_seconds": 8 * 60 * 60,
        "grant_after_contact_seconds": 2 * 60 * 60,
        "clock_discontinuity_seconds": 5 * 60,
    },
    "SENSITIVE": {
        "aging_after_seconds": 5 * 60,
        "stale_after_seconds": 30 * 60,
        "expires_after_seconds": 2 * 60 * 60,
        "grant_after_contact_seconds": 60 * 60,
        "clock_discontinuity_seconds": 2 * 60,
    },
}
APPROVED_HEALTH: dict[str, int] = {
    "failures_to_offline": 3,
    "failure_span_seconds": 20,
    "successes_to_sync": 2,
    "success_spacing_seconds": 10,
}
APPROVED_INACTIVITY_LOCK_SECONDS = 10 * 60
APPROVED_RESULT_CLEAR_SECONDS = 45


class ValidityWindowError(ValueError):
    """The package would be expired before it could be used."""


def validity_for(sensitivity: str) -> dict:
    return settings.ENTRY_OFFLINE_VALIDITY[sensitivity]


def event_day_end(moment: datetime, timezone_name: str) -> datetime:
    """The end of `moment`'s calendar day in the event timezone (exclusive)."""
    zone = ZoneInfo(timezone_name or "UTC")
    local = moment.astimezone(zone)
    next_midnight = datetime(local.year, local.month, local.day, tzinfo=zone) + timedelta(days=1)
    return next_midnight.astimezone(moment.tzinfo)


def package_bands(
    *, data_cutoff_at: datetime, sensitivity: str, event_timezone: str, device_expires_at: datetime
) -> Bands:
    """Aging, stale and hard-expiry instants for a package built at `data_cutoff_at`.

    Hard expiry is the EARLIEST of the approved maximum after the cutoff, the
    end of the event day in the event timezone, and the device expiry. The
    aging and stale instants never fall after the hard expiry.
    """
    values = validity_for(sensitivity)
    expires_at = min(
        data_cutoff_at + timedelta(seconds=values["expires_after_seconds"]),
        event_day_end(data_cutoff_at, event_timezone),
        device_expires_at,
    )
    if expires_at <= data_cutoff_at + timedelta(seconds=60):
        raise ValidityWindowError("The package would expire within a minute of its cutoff.")
    aging_at = min(data_cutoff_at + timedelta(seconds=values["aging_after_seconds"]), expires_at)
    stale_at = min(data_cutoff_at + timedelta(seconds=values["stale_after_seconds"]), expires_at)
    return Bands(aging_at=aging_at, stale_at=stale_at, expires_at=expires_at)


def band_at(now: datetime, *, aging_at: datetime, stale_at: datetime, expires_at: datetime) -> str:
    """The band of a package at `now`. Expired wins over every other band."""
    if now >= expires_at:
        return BAND_EXPIRED
    if now >= stale_at:
        return BAND_STALE
    if now >= aging_at:
        return BAND_AGING
    return BAND_FRESH


# ---------------------------------------------------------------------------
# Operational states and permitted actions (the required seven states)
# ---------------------------------------------------------------------------

STATE_ONLINE = "ONLINE"
STATE_OFFLINE_READY = "OFFLINE_READY"
STATE_OFFLINE_ACTIVE = "OFFLINE_ACTIVE"
STATE_SYNCING = "SYNCING"
STATE_STALE = "STALE"
STATE_EXPIRED = "EXPIRED"
STATE_BLOCKED = "BLOCKED"

OPERATIONAL_STATES: tuple[str, ...] = (
    STATE_ONLINE,
    STATE_OFFLINE_READY,
    STATE_OFFLINE_ACTIVE,
    STATE_SYNCING,
    STATE_STALE,
    STATE_EXPIRED,
    STATE_BLOCKED,
)

#: Every action a device can take. Offline admission actions are defined
#: here so their permission is fixed now; their implementation is Prompt 3.
ACTIONS: tuple[str, ...] = (
    "VERIFY_ONLINE",
    "RECORD_ONLINE_DECISION",
    "VERIFY_OFFLINE_QR",
    "ADMIT_OFFLINE",
    "DO_NOT_ADMIT_OFFLINE",
    "MANUAL_REVIEW_OFFLINE",
    "OVERRIDE_OFFLINE",
    "PREPARE_DEVICE",
    "REFRESH_PACKAGE",
    "UPLOAD_OPERATIONS",
    "MANUAL_PROCEDURE",
)

#: Permitted actions per state. Everything not listed is FORBIDDEN.
PERMITTED_ACTIONS: dict[str, frozenset[str]] = {
    STATE_ONLINE: frozenset(
        {
            "VERIFY_ONLINE",
            "RECORD_ONLINE_DECISION",
            "PREPARE_DEVICE",
            "REFRESH_PACKAGE",
            "UPLOAD_OPERATIONS",
        }
    ),
    STATE_OFFLINE_READY: frozenset(
        {
            "VERIFY_ONLINE",
            "RECORD_ONLINE_DECISION",
            "PREPARE_DEVICE",
            "REFRESH_PACKAGE",
            "UPLOAD_OPERATIONS",
        }
    ),
    # Health restored: new verifications go to the cloud authority; no
    # offline admission while the cloud is healthy.
    STATE_SYNCING: frozenset(
        {"VERIFY_ONLINE", "RECORD_ONLINE_DECISION", "REFRESH_PACKAGE", "UPLOAD_OPERATIONS"}
    ),
    STATE_OFFLINE_ACTIVE: frozenset(
        {
            "VERIFY_OFFLINE_QR",
            "ADMIT_OFFLINE",
            "DO_NOT_ADMIT_OFFLINE",
            "MANUAL_REVIEW_OFFLINE",
            "OVERRIDE_OFFLINE",
            "MANUAL_PROCEDURE",
        }
    ),
    # Stale permits only Manual Review or Do Not Admit; no override.
    STATE_STALE: frozenset(
        {"VERIFY_OFFLINE_QR", "DO_NOT_ADMIT_OFFLINE", "MANUAL_REVIEW_OFFLINE", "MANUAL_PROCEDURE"}
    ),
    # Expired never produces a normal admission success, nor a "verified"
    # presentation: referral to the manual procedure only.
    STATE_EXPIRED: frozenset({"MANUAL_REVIEW_OFFLINE", "MANUAL_PROCEDURE"}),
    STATE_BLOCKED: frozenset({"MANUAL_PROCEDURE"}),
}

#: Actions that can never be permitted in any offline state other than
#: Offline Active -- checked by tests so a future edit cannot widen Stale or
#: Expired by accident.
ADMISSION_ACTIONS = frozenset({"ADMIT_OFFLINE", "OVERRIDE_OFFLINE"})


def client_config() -> dict:
    """The configuration the device shell embeds (a non-executable JSON block).

    Approved values and the action matrix only -- no participant data, no
    secret, no identifier beyond API paths.
    """
    return {
        "states": list(OPERATIONAL_STATES),
        "actions": list(ACTIONS),
        "permitted": {state: sorted(actions) for state, actions in PERMITTED_ACTIONS.items()},
        "validity": settings.ENTRY_OFFLINE_VALIDITY,
        "health": {
            **settings.ENTRY_OFFLINE_HEALTH,
            "retry_seconds": settings.ENTRY_OFFLINE_HEALTH_RETRY_SECONDS,
            "poll_seconds": settings.ENTRY_CONNECTION_POLL_SECONDS,
        },
        "inactivity_lock_seconds": settings.ENTRY_OFFLINE_OPERATOR_INACTIVITY_LOCK_SECONDS,
        "result_clear_seconds": settings.ENTRY_RESULT_CLEAR_SECONDS,
        "package_refresh_seconds": settings.ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS,
        "delta_refresh_seconds": settings.ENTRY_OFFLINE_DELTA_REFRESH_SECONDS,
        "rebuild_blocking_reasons": sorted(REBUILD_BLOCKING_REASONS),
        "build_poll_limit": settings.ENTRY_OFFLINE_BUILD_POLL_LIMIT,
        "min_free_storage_bytes": settings.ENTRY_OFFLINE_MIN_FREE_STORAGE_BYTES,
        "schema": {
            "package": PACKAGE_SCHEMA_VERSION,
            "delta": DELTA_SCHEMA_VERSION,
            "grant": GRANT_SCHEMA_VERSION,
            "wipe": WIPE_ORDER_SCHEMA_VERSION,
            "operation": OPERATION_SCHEMA_VERSION,
            "ack": ACK_SCHEMA_VERSION,
        },
        "allow": {
            "package": sorted(PACKAGE_BODY),
            "entry": sorted(PACKAGE_ENTRY),
            "labels": sorted(LABELS),
            "window": sorted(WINDOW),
            "rule": sorted(RULE),
            "zone_rules": sorted(ZONE_RULES),
            "restriction": sorted(RESTRICTION),
            "revoked_pass": sorted(REVOKED_PASS),
            "qr_key": sorted(QR_KEY),
            "override_reason": sorted(OVERRIDE_REASON),
            "delta": sorted(DELTA_BODY),
            "manifest": sorted(PACKAGE_MANIFEST),
            "delta_manifest": sorted(DELTA_MANIFEST),
            "grant": sorted(GRANT_PAYLOAD),
            "ack": sorted(ACKNOWLEDGEMENT),
        },
        # Phase 4 Prompt 3: the offline verifier and the synchronization
        # client. The QR bounds mirror the ONLINE verifier exactly.
        "qr": {
            "max_bytes": int(settings.QR_MAX_TOKEN_BYTES),
            "supported_versions": [int(v) for v in settings.QR_SUPPORTED_PAYLOAD_VERSIONS],
            "clock_skew_seconds": int(settings.QR_CLOCK_SKEW_SECONDS),
        },
        "recent_reentry_seconds": int(settings.ENTRY_RECENT_REENTRY_SECONDS),
        "never_overrideable": sorted(_never_overrideable()),
        "stale_policy": STALE_POLICY,
        "expired_policy": EXPIRED_POLICY,
        "sync": {
            "batch_size": int(settings.ENTRY_OFFLINE_SYNC_BATCH_SIZE),
            "batch_bytes": int(settings.ENTRY_OFFLINE_SYNC_BATCH_BYTES),
            "retry_base_seconds": int(settings.ENTRY_OFFLINE_SYNC_RETRY_BASE_SECONDS),
            "retry_max_seconds": int(settings.ENTRY_OFFLINE_SYNC_RETRY_MAX_SECONDS),
            "ack_retention_seconds": int(settings.ENTRY_OFFLINE_ACK_RETENTION_SECONDS),
            "durable_statuses": sorted(DURABLE_ACK_STATUSES),
            "conflicted_statuses": sorted(CONFLICTED_ACK_STATUSES),
        },
    }


#: The approved Stale and Expired behaviour (binding decision P2-B), stated
#: as explicit named policies. `apps.entry.checks` (entry.E012) refuses any
#: permitted-action matrix that departs from them, and the synchronization
#: service enforces them again on every uploaded operation.
STALE_POLICY = "MANUAL_REVIEW_OR_DO_NOT_ADMIT"
EXPIRED_POLICY = "MANUAL_PROCEDURE_ONLY"
STALE_PERMITTED = frozenset(
    {"VERIFY_OFFLINE_QR", "DO_NOT_ADMIT_OFFLINE", "MANUAL_REVIEW_OFFLINE", "MANUAL_PROCEDURE"}
)
EXPIRED_PERMITTED = frozenset({"MANUAL_REVIEW_OFFLINE", "MANUAL_PROCEDURE"})

#: Acknowledgement statuses the server returns only after the outcome is
#: durably committed; a device marks a record acknowledged on these only.
DURABLE_ACK_STATUSES = frozenset(
    {
        "APPLIED",
        "CONFLICT",
        "RECONCILIATION_REQUIRED",
        "SECURITY_CONFLICT",
        "REJECTED",
        "QUARANTINED",
    }
)
CONFLICTED_ACK_STATUSES = frozenset(
    {"CONFLICT", "RECONCILIATION_REQUIRED", "SECURITY_CONFLICT", "REJECTED", "QUARANTINED"}
)
#: Non-durable acknowledgement statuses: the device keeps the record pending.
PENDING_ACK_STATUSES = frozenset({"PENDING", "NOT_PROCESSED"})


def _never_overrideable() -> frozenset[str]:
    from apps.entry.models import NEVER_OVERRIDEABLE_REASON_CODES

    return frozenset(str(code) for code in NEVER_OVERRIDEABLE_REASON_CODES)
