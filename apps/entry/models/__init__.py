"""Entry devices, checkpoint sessions, security restrictions, overrides,
and Entry Events (Schema §11, Phase 3 Prompt 4, ADR-0021).

Plan A (online entry) plus, from Phase 4 Prompt 2 (ADR-0023), Plan B
PREPARATION: device keys, per-event offline enablement, immutable Offline
Packages, critical deltas, operator offline grants, and the emergency-wipe
order and evidence records. From Phase 4 Prompt 3 (ADR-0024) Plan B also
SYNCHRONIZES: `SyncOperation` is the durable, immutable inbox of signed
offline operations, `ReconciliationCase` / `ReconciliationAction` hold the
conflicts a supervisor must review, and an `EntryEvent` may be offline --
then always linked to exactly one `SyncOperation`
(`entry_event_offline_coherent`).

Concept separation (UI/UX §9.1) is kept strict:

* `EntryDevice` / `DeviceScope` -- WHICH browser may operate WHERE;
* `EntryDeviceSession` -- one checkpoint set-up (gate + zone) on a device;
* `EntryOperatorSession` -- one named operator working on that set-up;
* `SecurityRestriction` -- a person- or context-level restriction;
* `EntryOverrideReason` -- the configurable override catalogue boundary;
* `EntryOverride` -- one exceptional supervisor decision;
* `EntryEvent` -- immutable physical entry evidence.

`EntryEvent` and `EntryOverride` are append-only: `entry.0001` installs a
`BEFORE UPDATE OR DELETE` trigger on both, exactly like the audit trail and
the stock ledger.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.fields import EncryptedTextField
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class EntryDeviceStatus(models.TextChoices):
    """Device lifecycle (Flow §14.6, Schema §11.1).

    `OFFLINE_READY` (Phase 4 Prompt 2) is an ENROLLED device that also passed
    offline preparation and its self-test. Both statuses work online; only
    `OFFLINE_READY` may ever operate offline (Prompt 3).
    """

    PENDING_ENROLLMENT = "PENDING_ENROLLMENT", _("Pending enrollment")
    ENROLLED = "ENROLLED", _("Enrolled")
    OFFLINE_READY = "OFFLINE_READY", _("Offline ready")
    SUSPENDED = "SUSPENDED", _("Suspended")
    REVOKED = "REVOKED", _("Revoked")
    EXPIRED = "EXPIRED", _("Expired")


#: Statuses in which a device authenticates and operates online.
OPERATIONAL_DEVICE_STATUSES: tuple[str, ...] = (
    EntryDeviceStatus.ENROLLED,
    EntryDeviceStatus.OFFLINE_READY,
)


class OfflineSensitivity(models.TextChoices):
    """Checkpoint sensitivity class selecting the approved validity values
    (binding decision P2-B, `settings.ENTRY_OFFLINE_VALIDITY`)."""

    STANDARD = "STANDARD", _("Standard")
    SENSITIVE = "SENSITIVE", _("Sensitive")


class VerificationMethod(models.TextChoices):
    """Lookup methods, in the approved priority order (Flow §10.4)."""

    QR = "QR", _("Digital Entry Pass QR")
    NIN = "NIN", _("National identity number")
    PASSPORT = "PASSPORT", _("Passport number")
    REFERENCE = "REFERENCE", _("Registration reference")
    MANUAL = "MANUAL", _("Controlled manual search")


#: Every method, in priority order. `DeviceScope.verification_methods` must
#: be a subset of this tuple.
VERIFICATION_METHOD_ORDER: tuple[str, ...] = tuple(VerificationMethod.values)


class EntryResult(models.TextChoices):
    """The explicit verification result vocabulary (Prompt 4, Flow §10.7).

    `UNSUPPORTED` and `TECHNICAL_ERROR` can only ever describe a
    verification that did not resolve a Registration Context, so they are
    audited but never become an `EntryEvent` (which requires one).
    """

    ALLOWED = "ALLOWED", _("Allowed")
    ALLOWED_WITH_ADVISORY = "ALLOWED_WITH_ADVISORY", _("Allowed with advisory")
    MANUAL_REVIEW = "MANUAL_REVIEW", _("Manual review required")
    DENIED = "DENIED", _("Denied")
    STALE = "STALE", _("Stale credential or verification")
    UNSUPPORTED = "UNSUPPORTED", _("Unsupported credential")
    TECHNICAL_ERROR = "TECHNICAL_ERROR", _("Technical error")


#: Results under which an operator may admit WITHOUT an override.
ADMITTABLE_RESULTS: tuple[str, ...] = (EntryResult.ALLOWED, EntryResult.ALLOWED_WITH_ADVISORY)


class EntryReasonCode(models.TextChoices):
    """Stable, operator-safe reason codes. Never a restricted detail."""

    NONE = "", _("No reason")
    PRIOR_ENTRY = "PRIOR_ENTRY", _("Previously admitted")
    RECENT_REENTRY = "RECENT_REENTRY", _("Very recent previous admission")
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL", _("Credential not valid")
    NO_MATCH = "NO_MATCH", _("No matching approved registration")
    UNSUPPORTED_CREDENTIAL = "UNSUPPORTED_CREDENTIAL", _("Credential format not supported")
    WRONG_EVENT = "WRONG_EVENT", _("Credential belongs to another event")
    REGISTRATION_NOT_APPROVED = "REGISTRATION_NOT_APPROVED", _("Registration not approved")
    SECURITY_RESTRICTION = "SECURITY_RESTRICTION", _("Security restriction")
    RESTRICTION_REVIEW = "RESTRICTION_REVIEW", _("Security review required")
    PASS_REVOKED = "PASS_REVOKED", _("Pass revoked")
    PASS_REPLACED = "PASS_REPLACED", _("Pass replaced by a newer version")
    PASS_SUSPENDED = "PASS_SUSPENDED", _("Pass suspended")
    PASS_INACTIVE = "PASS_INACTIVE", _("Pass not yet activated")
    PASS_EXPIRED = "PASS_EXPIRED", _("Pass validity ended")
    PASS_NOT_YET_VALID = "PASS_NOT_YET_VALID", _("Pass validity not started")
    NO_ACTIVE_PASS = "NO_ACTIVE_PASS", _("No active pass")
    ASSIGNMENT_CHANGED = "ASSIGNMENT_CHANGED", _("Assignment changed since pass issue")
    NO_ACCESS_ASSIGNMENT = "NO_ACCESS_ASSIGNMENT", _("No current access assignment")
    OUTSIDE_TIME_WINDOW = "OUTSIDE_TIME_WINDOW", _("Outside the access time window")
    WRONG_ZONE = "WRONG_ZONE", _("Not permitted at this checkpoint")
    ACCESS_RULE_DENY = "ACCESS_RULE_DENY", _("Checkpoint closed for this access profile")
    ALREADY_ADMITTED = "ALREADY_ADMITTED", _("Single-entry access already used")
    IDENTITY_UNVERIFIED = "IDENTITY_UNVERIFIED", _("Identity reference not verified")
    VERIFICATION_STALE = "VERIFICATION_STALE", _("Verification expired; verify again")
    TECHNICAL_ERROR = "TECHNICAL_ERROR", _("Technical error")
    # Phase 4 Prompt 3 (ADR-0024): results only an OFFLINE device can reach.
    # None of them is ever overrideable, and none can admit.
    OFFLINE_LOCAL_MISS = (
        "OFFLINE_LOCAL_MISS",
        _("Not in this device's offline data — manual review"),
    )
    OFFLINE_DATA_STALE = (
        "OFFLINE_DATA_STALE",
        _("Offline data too old for an admission — manual review"),
    )
    OFFLINE_DATA_CHANGED = (
        "OFFLINE_DATA_CHANGED",
        _("Offline data no longer current for this pass — manual review"),
    )
    OFFLINE_PACKAGE_EXPIRED = (
        "OFFLINE_PACKAGE_EXPIRED",
        _("Offline data expired — follow the manual procedure"),
    )


#: Reason codes that NO override may ever bypass, whatever the configurable
#: catalogue says (Flow §10.9: "No override may bypass a non-overridable
#: security restriction or accept a pass for a different Event Edition").
#: A non-overrideable `SECURITY_RESTRICTION` is decided per restriction row
#: at evaluation time, in addition to this fixed set. The offline-only codes
#: describe what a disconnected device CANNOT know, so they are never
#: overrideable either (Phase 4 Prompt 3).
NEVER_OVERRIDEABLE_REASON_CODES: frozenset[str] = frozenset(
    {
        EntryReasonCode.INVALID_CREDENTIAL,
        EntryReasonCode.NO_MATCH,
        EntryReasonCode.UNSUPPORTED_CREDENTIAL,
        EntryReasonCode.WRONG_EVENT,
        EntryReasonCode.REGISTRATION_NOT_APPROVED,
        EntryReasonCode.PASS_REVOKED,
        EntryReasonCode.VERIFICATION_STALE,
        EntryReasonCode.TECHNICAL_ERROR,
        EntryReasonCode.OFFLINE_LOCAL_MISS,
        EntryReasonCode.OFFLINE_DATA_STALE,
        EntryReasonCode.OFFLINE_DATA_CHANGED,
        EntryReasonCode.OFFLINE_PACKAGE_EXPIRED,
    }
)


class EntryDecision(models.TextChoices):
    """The operator's explicit choice (Flow §10.5)."""

    ADMIT = "ADMIT", _("Admit")
    DO_NOT_ADMIT = "DO_NOT_ADMIT", _("Do not admit")
    REDIRECTED = "REDIRECTED", _("Redirected")


class EntryDecisionReason(models.TextChoices):
    """Operator-chosen reason recorded with a non-admission (FR-ENT-007)."""

    FOLLOWS_RESULT = "FOLLOWS_RESULT", _("Follows the verification result")
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH", _("Person does not match the record")
    SENT_TO_REVIEW_DESK = "SENT_TO_REVIEW_DESK", _("Sent to the review desk")
    SENT_TO_OTHER_CHECKPOINT = "SENT_TO_OTHER_CHECKPOINT", _("Sent to a permitted checkpoint")
    PARTICIPANT_LEFT = "PARTICIPANT_LEFT", _("Participant left the checkpoint")


class EntryEventType(models.TextChoices):
    ENTRY = "ENTRY", _("Entry")
    EXIT = "EXIT", _("Exit")


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class EntryDevice(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """An enrolled entry browser (Schema §11.1, TRD §12.3).

    The device credential is a 256-bit random secret held ONLY in the
    device browser's HttpOnly cookie. This table stores its SHA-256 digest
    (`credential_hash`), never the secret, so a database read cannot be
    replayed as a device. The one-time activation code is likewise stored
    only as a digest and expires quickly.

    `public_id` is the only identifier ever shown or used in a URL.
    """

    public_id = models.CharField(max_length=22, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    public_name = models.CharField(max_length=100)
    status = models.CharField(
        max_length=24,
        choices=EntryDeviceStatus.choices,
        default=EntryDeviceStatus.PENDING_ENROLLMENT,
    )
    #: Random, non-secret reference to the CURRENT device credential. It is
    #: rotated on every (re-)enrollment, so audit evidence can distinguish
    #: two enrollments of the same physical device.
    device_key_id = models.CharField(max_length=16, blank=True, default="")
    credential_hash = models.CharField(max_length=64, blank=True, default="")
    activation_code_hash = models.CharField(max_length=64, blank=True, default="")
    activation_code_expires_at = models.DateTimeField(null=True, blank=True)
    enrolled_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(null=True, blank=True)
    #: Health column required by Schema §11.1. Always NULL until offline
    #: synchronization exists.
    last_sync_at = models.DateTimeField(null=True, blank=True)
    app_version = models.CharField(max_length=64, blank=True, default="")
    platform_summary = models.CharField(max_length=200, blank=True, default="")
    data_wipe_requested_at = models.DateTimeField(null=True, blank=True)
    # --- Offline preparation (Phase 4 Prompt 2, ADR-0023) ---
    offline_prepared_at = models.DateTimeField(null=True, blank=True)
    offline_prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    offline_self_test_at = models.DateTimeField(null=True, blank=True)
    #: Set by "Block offline use": offline preparation is withdrawn and the
    #: device must be prepared again. Online operation is unaffected.
    offline_blocked_at = models.DateTimeField(null=True, blank=True)
    #: Metadata the device last reported about its local store. Counts,
    #: sequence numbers and a hash only -- never an operation's content.
    reported_state = models.CharField(max_length=24, blank=True, default="")
    reported_pending_operations = models.PositiveIntegerField(null=True, blank=True)
    reported_locked_operations = models.PositiveIntegerField(null=True, blank=True)
    reported_sequence_high = models.BigIntegerField(null=True, blank=True)
    reported_chain_head = models.CharField(max_length=64, blank=True, default="")
    reported_package_version = models.BigIntegerField(null=True, blank=True)
    reported_delta_version = models.BigIntegerField(null=True, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status_reason_code = models.CharField(max_length=32, blank=True, default="")
    status_reason_text = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "entry_device"
        permissions = [
            ("manage_entrydevice", "Can enroll, rescope, suspend, and revoke entry devices"),
            (
                "emergency_wipe_device",
                "Can order a destructive emergency wipe of an entry device local store",
            ),
        ]
        constraints = [
            models.UniqueConstraint(fields=["public_id"], name="entry_device_public_id_uq"),
            models.UniqueConstraint(
                fields=["credential_hash"],
                condition=~models.Q(credential_hash=""),
                name="entry_device_credential_hash_uq",
            ),
            models.UniqueConstraint(
                fields=["device_key_id"],
                condition=~models.Q(device_key_id=""),
                name="entry_device_key_id_uq",
            ),
            # An ENROLLED device always has a credential and an enrollment time.
            models.CheckConstraint(
                condition=~models.Q(status="ENROLLED")
                | (~models.Q(credential_hash="") & models.Q(enrolled_at__isnull=False)),
                name="entry_device_enrolled_has_credential",
            ),
            # The same invariant for OFFLINE_READY (Phase 4 Prompt 2; a second
            # constraint, so the approved Prompt 4 one is left untouched).
            models.CheckConstraint(
                condition=~models.Q(status="OFFLINE_READY")
                | (~models.Q(credential_hash="") & models.Q(enrolled_at__isnull=False)),
                name="entry_device_offline_ready_has_credential",
            ),
            # OFFLINE_READY only after preparation AND a passed self-test, and
            # never while offline use is blocked.
            models.CheckConstraint(
                condition=~models.Q(status="OFFLINE_READY")
                | (
                    models.Q(offline_prepared_at__isnull=False)
                    & models.Q(offline_self_test_at__isnull=False)
                    & models.Q(offline_blocked_at__isnull=True)
                ),
                name="entry_device_offline_ready_prepared",
            ),
        ]
        indexes = [
            models.Index(fields=["event_edition", "status"], name="entry_device_event_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.public_name}:{self.status}"


class DeviceScope(UUIDPrimaryKeyModel):
    """One version of a device's operating scope (Schema §11.1).

    Scopes are versioned, never edited: a scope change closes the current
    row (`is_current=False`) and writes a new one with the next
    `scope_version`, so an Entry Event can always be traced to the exact
    scope it was recorded under.
    """

    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="scopes")
    scope_version = models.PositiveIntegerField()
    is_current = models.BooleanField(default=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    venue = models.ForeignKey("events.Venue", on_delete=models.PROTECT, related_name="+")
    gate = models.ForeignKey("events.Gate", on_delete=models.PROTECT, related_name="+")
    permitted_zones = models.ManyToManyField("events.Zone", related_name="+")
    verification_methods = models.JSONField(default=list)
    #: Offline continuity is permitted for this scope version (Phase 4
    #: Prompt 2). Necessary, never sufficient: the global flag, the event
    #: enablement, preparation and the self-test are also required. The
    #: only offline method is QR (binding decision P2-E).
    offline_capable = models.BooleanField(default=False)
    offline_sensitivity = models.CharField(
        max_length=16, choices=OfflineSensitivity.choices, default=OfflineSensitivity.STANDARD
    )
    reason = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "entry_device_scope"
        constraints = [
            models.UniqueConstraint(
                fields=["device", "scope_version"], name="entry_scope_device_version_uq"
            ),
            models.UniqueConstraint(
                fields=["device"],
                condition=models.Q(is_current=True),
                name="entry_scope_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(scope_version__gte=1), name="entry_scope_version_positive"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.device_id}:v{self.scope_version}"


class EntryDeviceSession(UUIDPrimaryKeyModel):
    """One checkpoint set-up on an enrolled device (Flow §10.3).

    Short-lived by design (`ENTRY_DEVICE_SESSION_SECONDS`). The gate and
    zone are fixed for the session's lifetime; working at another
    checkpoint requires a new session, validated against the device scope
    again.
    """

    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="sessions")
    scope = models.ForeignKey(DeviceScope, on_delete=models.PROTECT, related_name="+")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    gate = models.ForeignKey("events.Gate", on_delete=models.PROTECT, related_name="+")
    zone = models.ForeignKey("events.Zone", on_delete=models.PROTECT, related_name="+")
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "entry_device_session"
        constraints = [
            models.UniqueConstraint(
                fields=["device"],
                condition=models.Q(ended_at__isnull=True),
                name="entry_devsession_one_open_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("started_at")),
                name="entry_devsession_valid_range",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"device-session:{self.pk}"


class EntryOperatorSession(UUIDPrimaryKeyModel):
    """One named operator working within a device session.

    Its own inactivity and absolute lifetimes are shorter than the device
    session's (`ENTRY_OPERATOR_SESSION_*`). Shared accounts are prohibited
    (TRD §16.1), so a supervisor taking over the device opens their own
    operator session rather than acting inside someone else's.
    """

    device_session = models.ForeignKey(
        EntryDeviceSession, on_delete=models.PROTECT, related_name="operator_sessions"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    last_activity_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "entry_operator_session"
        constraints = [
            models.UniqueConstraint(
                fields=["device_session", "user"],
                condition=models.Q(ended_at__isnull=True),
                name="entry_opsession_one_open_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("started_at")),
                name="entry_opsession_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "ended_at"], name="entry_opsession_user_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"operator-session:{self.pk}"


# ---------------------------------------------------------------------------
# Security restrictions (Schema §11.6)
# ---------------------------------------------------------------------------


class RestrictionSeverity(models.TextChoices):
    DENY_ENTRY = "DENY_ENTRY", _("Deny entry")
    MANUAL_REVIEW = "MANUAL_REVIEW", _("Require manual review")


class RestrictionCategory(models.TextChoices):
    SECURITY_CONCERN = "SECURITY_CONCERN", _("Security concern")
    IDENTITY_DISPUTE = "IDENTITY_DISPUTE", _("Identity dispute")
    ACCREDITATION_HOLD = "ACCREDITATION_HOLD", _("Accreditation hold")
    OTHER_APPROVED = "OTHER_APPROVED", _("Other approved reason")


class RestrictionStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    REVOKED = "REVOKED", _("Revoked")


class SecurityRestriction(UUIDPrimaryKeyModel, TimestampedModel):
    """A person-level or context-level entry restriction (Schema §11.6).

    Exactly one of `person` / `registration` is set (database-enforced). A
    person-level restriction applies to EVERY Registration Context of that
    person (BR-ENT-003). `event_edition` NULL means every edition.

    The free-text reason is encrypted at rest and never reaches an entry
    screen: entry interfaces receive only the decision-relevant projection
    (`apps.entry.selectors.restriction_projection`).
    """

    person = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    event_edition = models.ForeignKey(
        "events.EventEdition", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    severity = models.CharField(max_length=16, choices=RestrictionSeverity.choices)
    category = models.CharField(max_length=24, choices=RestrictionCategory.choices)
    is_overrideable = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    reason_encrypted = EncryptedTextField()
    status = models.CharField(
        max_length=16, choices=RestrictionStatus.choices, default=RestrictionStatus.ACTIVE
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revocation_reason_code = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "entry_security_restriction"
        permissions = [
            ("manage_securityrestriction", "Can create and revoke security restrictions"),
            (
                "view_restriction_reason",
                "Can see the category of a security restriction at a checkpoint",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(person__isnull=False) & models.Q(registration__isnull=True))
                | (models.Q(person__isnull=True) & models.Q(registration__isnull=False)),
                name="entry_restriction_exactly_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gt=models.F("starts_at")),
                name="entry_restriction_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["person", "status"], name="entry_restriction_person_idx"),
            models.Index(fields=["registration", "status"], name="entry_restriction_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - never the reason
        return f"restriction:{self.pk}:{self.status}"


# ---------------------------------------------------------------------------
# Override catalogue boundary and overrides (Schema §11.5, OD-AF-13)
# ---------------------------------------------------------------------------


class EntryOverrideReason(UUIDPrimaryKeyModel, TimestampedModel):
    """One approved override type -- the configuration boundary for the
    still-unresolved final override catalogue (OD-AF-13).

    NOTHING is seeded: until the catalogue is approved, no override is
    possible at all (fail closed). `overridable_reason_codes` lists the
    `EntryReasonCode` values this override type may bypass; codes in
    `NEVER_OVERRIDEABLE_REASON_CODES` are refused by the service even if
    configured here, and so is any non-overrideable restriction.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    code = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    overridable_reason_codes = models.JSONField(default=list)
    requires_note = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "entry_override_reason"
        permissions = [
            ("manage_entryoverridereason", "Can configure the entry override catalogue"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="entry_override_reason_code_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code

    @property
    def localized_name(self) -> str:
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


class EntryOverride(UUIDPrimaryKeyModel):
    """One exceptional supervisor admission (Schema §11.5). Append-only."""

    operation_id = models.CharField(max_length=64, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="+"
    )
    digital_entry_pass = models.ForeignKey(
        "badges.DigitalEntryPass", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    original_result = models.CharField(max_length=24, choices=EntryResult.choices)
    original_reason_code = models.CharField(
        max_length=32, choices=EntryReasonCode.choices, blank=True, default=""
    )
    final_decision = models.CharField(
        max_length=16, choices=EntryDecision.choices, default=EntryDecision.ADMIT
    )
    reason = models.ForeignKey(EntryOverrideReason, on_delete=models.PROTECT, related_name="+")
    reason_code = models.CharField(max_length=64)
    note_encrypted = EncryptedTextField(blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    gate = models.ForeignKey("events.Gate", on_delete=models.PROTECT, related_name="+")
    zone = models.ForeignKey("events.Zone", on_delete=models.PROTECT, related_name="+")
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    #: Phase 4 Prompt 3 (ADR-0024): an override recorded OFFLINE under an
    #: operator grant, applied from exactly one synchronized operation.
    #: `original_result` is then the device-known local result.
    offline = models.BooleanField(default=False)
    sync_operation = models.OneToOneField(
        "SyncOperation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="entry_override",
    )

    class Meta:
        db_table = "entry_override"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(final_decision="ADMIT"), name="entry_override_admit_only"
            ),
            # An override exists to bypass a non-admittable result; it is
            # never needed -- and so never recorded -- for an allowed one.
            models.CheckConstraint(
                condition=~models.Q(original_result__in=list(ADMITTABLE_RESULTS)),
                name="entry_override_not_for_allowed",
            ),
            models.CheckConstraint(
                condition=(models.Q(offline=False) & models.Q(sync_operation__isnull=True))
                | (models.Q(offline=True) & models.Q(sync_operation__isnull=False)),
                name="entry_override_offline_coherent",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "occurred_at"], name="entry_override_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"override:{self.pk}"


# ---------------------------------------------------------------------------
# Entry Events (Schema §11.4)
# ---------------------------------------------------------------------------


class EntryEvent(UUIDPrimaryKeyModel):
    """Immutable physical entry evidence (Schema §11.4). Append-only.

    Written only after an explicit operator decision (Gate G7), inside the
    same transaction as its audit event. `operation_id` is the idempotency
    key: a retried decision returns the original event, and a reused
    identifier bound to a different command is a conflict, never a replay.
    """

    operation_id = models.CharField(max_length=64, unique=True)
    command_fingerprint = models.CharField(max_length=64)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="+"
    )
    digital_entry_pass = models.ForeignKey(
        "badges.DigitalEntryPass", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    badge_assignment = models.ForeignKey(
        "accreditation.BadgeTypeAssignment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    gate = models.ForeignKey("events.Gate", on_delete=models.PROTECT, related_name="+")
    zone = models.ForeignKey("events.Zone", on_delete=models.PROTECT, related_name="+")
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    device_session = models.ForeignKey(
        EntryDeviceSession, on_delete=models.PROTECT, related_name="+"
    )
    operator_session = models.ForeignKey(
        EntryOperatorSession, on_delete=models.PROTECT, related_name="+"
    )
    operator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    event_type = models.CharField(
        max_length=8, choices=EntryEventType.choices, default=EntryEventType.ENTRY
    )
    verification_method = models.CharField(max_length=16, choices=VerificationMethod.choices)
    result = models.CharField(max_length=24, choices=EntryResult.choices)
    reason_code = models.CharField(
        max_length=32, choices=EntryReasonCode.choices, blank=True, default=""
    )
    advisory_codes = models.JSONField(default=list, blank=True)
    decision = models.CharField(max_length=16, choices=EntryDecision.choices)
    decision_reason_code = models.CharField(
        max_length=32, choices=EntryDecisionReason.choices, blank=True, default=""
    )
    prior_entry_event = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    override = models.OneToOneField(
        EntryOverride, null=True, blank=True, on_delete=models.PROTECT, related_name="entry_event"
    )
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField()
    #: Phase 4 Prompt 3 (ADR-0024): TRUE for evidence recorded by an
    #: enrolled device while disconnected and applied from exactly one
    #: `SyncOperation`. `result`, `reason_code` and `advisory_codes` are then
    #: the DEVICE-KNOWN local result the operator acted on; the server's own
    #: evaluation is kept on the `SyncOperation` and any reconciliation case.
    offline = models.BooleanField(default=False)
    package_version = models.BigIntegerField(null=True, blank=True)
    sync_operation = models.OneToOneField(
        "SyncOperation", null=True, blank=True, on_delete=models.PROTECT, related_name="entry_event"
    )
    #: Set at insert (the row is append-only): the offline admission was
    #: found in conflict with authoritative state at synchronization. Such
    #: an event is always presented as "offline -- conflict", never as a
    #: normal admission, and always has an open or closed reconciliation case.
    offline_conflict = models.BooleanField(default=False)

    class Meta:
        db_table = "entry_event"
        permissions = [
            ("verify_entry", "Can verify credentials and record entry decisions at a checkpoint"),
            (
                "lookup_identity_reference",
                "Can look up a participant by NIN or passport number at a checkpoint",
            ),
            (
                "lookup_registration_reference",
                "Can look up a participant by registration reference at a checkpoint",
            ),
            ("manual_search_entry", "Can run the controlled manual search at a checkpoint"),
            ("override_entry", "Can record a reason-coded supervisor override"),
        ]
        constraints = [
            # Gate G7 in the database: an admission on a non-admittable
            # result is impossible without a linked override.
            models.CheckConstraint(
                condition=~models.Q(decision="ADMIT")
                | models.Q(result__in=list(ADMITTABLE_RESULTS))
                | models.Q(override__isnull=False),
                name="entry_event_admit_requires_allowed_or_override",
            ),
            # Phase 4 Prompt 3 replaces `entry_event_online_only`: an online
            # event carries no offline evidence at all; an offline event
            # always names its package version and its one sync operation.
            models.CheckConstraint(
                condition=(
                    models.Q(offline=False)
                    & models.Q(package_version__isnull=True)
                    & models.Q(sync_operation__isnull=True)
                    & models.Q(offline_conflict=False)
                )
                | (
                    models.Q(offline=True)
                    & models.Q(package_version__isnull=False)
                    & models.Q(sync_operation__isnull=False)
                ),
                name="entry_event_offline_coherent",
            ),
            # UNSUPPORTED / TECHNICAL_ERROR never resolve a context.
            models.CheckConstraint(
                condition=~models.Q(result__in=["UNSUPPORTED", "TECHNICAL_ERROR"]),
                name="entry_event_resolved_result_only",
            ),
        ]
        indexes = [
            models.Index(fields=["event_edition", "occurred_at"], name="entry_event_event_idx"),
            models.Index(fields=["registration", "-occurred_at"], name="entry_event_reg_idx"),
            models.Index(fields=["gate", "occurred_at"], name="entry_event_gate_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"entry-event:{self.pk}:{self.decision}"


# ---------------------------------------------------------------------------
# Online verification telemetry (Phase 3 Prompt 5, ADR-0022)
# ---------------------------------------------------------------------------


class VerificationMode(models.TextChoices):
    ONLINE = "ONLINE", _("Online")
    OFFLINE = "OFFLINE", _("Offline")


class VerificationSample(models.Model):
    """One online verification, measured (Phase 3 Prompt 5, ADR-0022).

    Operational telemetry, not evidence: it answers "how fast, how often,
    with which outcome, where" and nothing about WHO. By construction it
    holds NO registration, person, credential, pass, token, identity value,
    search text, or operator: only the checkpoint coordinates (event, gate,
    zone, device), the lookup method, the result/reason codes, the internal
    credential-verification code for QR lookups (e.g. `INVALID_SIGNATURE`),
    and the server-side latency. It is prunable (`ENTRY_METRICS_RETENTION_DAYS`)
    and never read by any admission decision.

    A bigint key (not the project's UUIDv7 default, ADR-0002) for the same
    reason as `audit.AuditEvent`: a high-volume, append-mostly table with
    time-ordered index locality.
    """

    id = models.BigAutoField(primary_key=True)
    occurred_at = models.DateTimeField()
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    gate = models.ForeignKey("events.Gate", on_delete=models.PROTECT, related_name="+")
    zone = models.ForeignKey("events.Zone", on_delete=models.PROTECT, related_name="+")
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    method = models.CharField(max_length=16, choices=VerificationMethod.choices)
    result = models.CharField(max_length=32, choices=EntryResult.choices)
    reason_code = models.CharField(max_length=32, blank=True, default="")
    credential_code = models.CharField(max_length=32, blank=True, default="")
    latency_ms = models.PositiveIntegerField()
    #: Phase 4 Prompt 3: ONLINE (server-side latency) or OFFLINE (the local
    #: verification latency the device measured, reported at synchronization).
    mode = models.CharField(max_length=8, choices=VerificationMode.choices, default="ONLINE")

    class Meta:
        db_table = "entry_verification_sample"
        permissions = [
            (
                "view_entry_observability",
                "Can view entry verification metrics, device health, and anomaly signals",
            ),
        ]
        indexes = [
            models.Index(fields=["gate", "occurred_at"], name="entry_sample_gate_idx"),
            models.Index(fields=["event_edition", "occurred_at"], name="entry_sample_event_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"sample:{self.method}:{self.result}:{self.latency_ms}ms"


# ---------------------------------------------------------------------------
# Enrolled-device offline preparation (Phase 4 Prompt 2, ADR-0023)
# ---------------------------------------------------------------------------


class OfflineEventSetting(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """Per-event enablement of offline continuity (binding decision P2-E).

    Absent row = disabled. Enabling is necessary, never sufficient: the
    global `ENTRY_OFFLINE_ENABLED` flag, an offline-capable device scope,
    preparation, and a passed self-test are also required.
    """

    event_edition = models.OneToOneField(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    enabled = models.BooleanField(default=False)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    changed_at = models.DateTimeField()
    reason_code = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "entry_offline_event_setting"
        permissions = [
            ("enable_offline_entry", "Can enable or disable offline continuity for an event"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"offline-setting:{self.event_edition_id}:{self.enabled}"


class DeviceKeyPurpose(models.TextChoices):
    """The two non-extractable WebCrypto key pairs a prepared device holds."""

    OPERATION_SIGNING = "SIGN", _("Operation signing (ECDSA P-256)")
    PACKAGE_UNWRAP = "UNWRAP", _("Package unwrapping (ECDH P-256)")


class DeviceKeyStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    RETIRED = "RETIRED", _("Retired")


class EntryDeviceKey(UUIDPrimaryKeyModel):
    """One device PUBLIC key registered at offline preparation.

    The private halves are non-extractable WebCrypto keys that never leave
    the device browser; only SubjectPublicKeyInfo DER (base64url) is stored.
    Re-preparation retires the previous keys; rows are never deleted.
    """

    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="offline_keys")
    purpose = models.CharField(max_length=8, choices=DeviceKeyPurpose.choices)
    key_version = models.PositiveIntegerField()
    public_key_spki = models.TextField()
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(
        max_length=8, choices=DeviceKeyStatus.choices, default=DeviceKeyStatus.ACTIVE
    )
    created_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "entry_device_key"
        constraints = [
            models.UniqueConstraint(
                fields=["device", "purpose"],
                condition=models.Q(status="ACTIVE"),
                name="entry_device_key_one_active_uq",
            ),
            models.UniqueConstraint(
                fields=["device", "purpose", "key_version"], name="entry_device_key_version_uq"
            ),
            models.UniqueConstraint(fields=["fingerprint"], name="entry_device_key_fp_uq"),
            models.CheckConstraint(
                condition=models.Q(status="ACTIVE", retired_at__isnull=True)
                | models.Q(status="RETIRED", retired_at__isnull=False),
                name="entry_device_key_status_coherent",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"device-key:{self.purpose}:v{self.key_version}:{self.status}"


class OfflinePackageStatus(models.TextChoices):
    READY = "READY", _("Ready")
    SUPERSEDED = "SUPERSEDED", _("Superseded")
    EXPIRED = "EXPIRED", _("Expired")
    REVOKED = "REVOKED", _("Revoked")


class OfflinePackage(UUIDPrimaryKeyModel):
    """Metadata of one immutable, device-encrypted Offline Package version
    (Schema §11.2, binding decisions P2-C/P2-D/P2-E).

    Never the plaintext and never the data key: the builder encrypts in
    memory to the device ECDH key and discards the data key, so the retained
    ciphertext (`stored_object_key`, private storage) is unreadable by the
    server. Content columns are immutable in the database
    (`entry_offline_package_guard` trigger); only lifecycle and download
    bookkeeping may change, and rows are never deleted.
    """

    public_id = models.CharField(max_length=22, unique=True)
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="packages")
    scope = models.ForeignKey(DeviceScope, on_delete=models.PROTECT, related_name="+")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    package_version = models.BigIntegerField()
    scope_version = models.PositiveIntegerField()
    schema_version = models.PositiveSmallIntegerField()
    sensitivity = models.CharField(max_length=16, choices=OfflineSensitivity.choices)
    signing_key_id = models.CharField(max_length=8)
    recipient_key = models.ForeignKey(EntryDeviceKey, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=16, choices=OfflinePackageStatus.choices, default=OfflinePackageStatus.READY
    )
    issued_at = models.DateTimeField()
    data_cutoff_at = models.DateTimeField()
    aging_at = models.DateTimeField()
    stale_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    manifest_sha256 = models.CharField(max_length=64)
    ciphertext_sha256 = models.CharField(max_length=64)
    response_sha256 = models.CharField(max_length=64)
    response_bytes = models.PositiveIntegerField()
    entry_count = models.PositiveIntegerField()
    stored_object_key = models.CharField(max_length=64)
    stored_object_deleted_at = models.DateTimeField(null=True, blank=True)
    download_count = models.PositiveIntegerField(default=0)
    first_downloaded_at = models.DateTimeField(null=True, blank=True)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    activation_reported_at = models.DateTimeField(null=True, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_reason_code = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField()
    # The change-journal watermark the projection was read at (entry.0004,
    # `apps.entry.services.offline_journal`): the PostgreSQL snapshot taken
    # before the first projection query, its xmin, the building transaction
    # id and the highest journal id then visible. A delta re-sends every
    # journaled change NOT covered by this snapshot. Empty on packages built
    # before entry.0004: such a package is never current again.
    data_snapshot = models.TextField(blank=True, default="")
    journal_xmin = models.BigIntegerField(null=True, blank=True)
    build_txid = models.BigIntegerField(null=True, blank=True)
    journal_high_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "entry_offline_package"
        constraints = [
            models.UniqueConstraint(
                fields=["device", "package_version"], name="entry_opkg_device_version_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(package_version__gte=1), name="entry_opkg_version_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(aging_at__gt=models.F("data_cutoff_at"))
                & models.Q(stale_at__gte=models.F("aging_at"))
                & models.Q(expires_at__gte=models.F("stale_at")),
                name="entry_opkg_bands_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["device", "-package_version"], name="entry_opkg_device_idx"),
            models.Index(fields=["status", "expires_at"], name="entry_opkg_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"offline-package:{self.device_id}:v{self.package_version}:{self.status}"


class OfflinePackageBuildStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    BUILDING = "BUILDING", _("Building")
    READY = "READY", _("Ready")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


OPEN_BUILD_STATUSES: tuple[str, ...] = (
    OfflinePackageBuildStatus.PENDING,
    OfflinePackageBuildStatus.BUILDING,
)


class OfflinePackageBuild(UUIDPrimaryKeyModel):
    """One requested package build (independent re-review correction B).

    The download API never builds: it records (or finds) the device's ONE
    open build -- a partial unique index allows at most one PENDING or
    BUILDING row per device -- and answers "building" until bytes exist.
    The package version is RESERVED here, so concurrent requests and
    Celery retries converge on this row and this version; a worker claims
    it with a lease token and commits only while it still holds the lease.
    `pending_object_key` records stored ciphertext that no committed
    package references yet, so a failed or abandoned attempt can always be
    cleaned up. Rows are never deleted, and a terminal status is final
    (`entry_offline_build_guard`).
    """

    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    scope = models.ForeignKey(DeviceScope, on_delete=models.PROTECT, related_name="+")
    recipient_key = models.ForeignKey(EntryDeviceKey, on_delete=models.PROTECT, related_name="+")
    package_version = models.BigIntegerField()
    status = models.CharField(
        max_length=16,
        choices=OfflinePackageBuildStatus.choices,
        default=OfflinePackageBuildStatus.PENDING,
    )
    request_reason = models.CharField(max_length=32)
    status_reason_code = models.CharField(max_length=32, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    pending_object_key = models.CharField(max_length=64, blank=True, default="")
    package = models.OneToOneField(
        OfflinePackage, on_delete=models.PROTECT, null=True, blank=True, related_name="build"
    )
    requested_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "entry_offline_package_build"
        constraints = [
            models.UniqueConstraint(
                fields=["device"],
                condition=models.Q(status__in=OPEN_BUILD_STATUSES),
                name="entry_obuild_one_open_per_device",
            ),
            models.UniqueConstraint(
                fields=["device", "package_version"], name="entry_obuild_device_version_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(package_version__gte=1), name="entry_obuild_version_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(status=OfflinePackageBuildStatus.READY)
                | models.Q(package__isnull=False),
                name="entry_obuild_ready_has_package",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "lease_until"], name="entry_obuild_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"offline-build:{self.device_id}:v{self.package_version}:{self.status}"


class OfflineChangeJournal(models.Model):
    """Durable invalidation journal for Offline Package projections (independent
    re-review correction C).

    Written ONLY by PostgreSQL triggers (entry.0004) on every table whose
    rows can change what a package or delta says: every INSERT, UPDATE and
    DELETE -- including `QuerySet.update()`, bulk operations and raw SQL,
    which bypass `updated_at` -- appends one row, and a TRUNCATE appends a
    statement-level row. `txid` is the writing transaction's id; a delta
    re-sends every row its package's PostgreSQL snapshot did not cover, so
    the result is correct in COMMIT order, not in insert order. Derived
    data: it holds internal ids only (never a name, identifier or reason)
    and is pruned after `ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS`.
    """

    id = models.BigAutoField(primary_key=True)
    txid = models.BigIntegerField()
    table_name = models.CharField(max_length=63)
    operation = models.CharField(max_length=1)
    row_id = models.CharField(max_length=64, blank=True, default="")
    event_edition_id = models.UUIDField(null=True, blank=True)
    refs = models.JSONField(default=dict)
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "entry_offline_change_journal"
        indexes = [
            models.Index(fields=["txid"], name="entry_ojournal_txid_idx"),
            models.Index(fields=["recorded_at"], name="entry_ojournal_recorded_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"offline-journal:{self.id}:{self.table_name}:{self.operation}"


class OfflineCriticalDelta(UUIDPrimaryKeyModel):
    """Metadata of one signed critical delta issued for one package.

    Append-only. A delta advances only `critical_delta_cutoff_at` on the
    device, never `package_data_cutoff_at` (binding decision P2-B). Its
    ciphertext is never stored: every request builds a new delta version.
    """

    package = models.ForeignKey(OfflinePackage, on_delete=models.PROTECT, related_name="deltas")
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    delta_version = models.BigIntegerField()
    issued_at = models.DateTimeField()
    critical_delta_cutoff_at = models.DateTimeField()
    manifest_sha256 = models.CharField(max_length=64)
    response_sha256 = models.CharField(max_length=64)
    counts = models.JSONField(default=dict)
    rebuild_required = models.BooleanField(default=False)

    class Meta:
        db_table = "entry_offline_delta"
        constraints = [
            models.UniqueConstraint(
                fields=["package", "delta_version"], name="entry_odelta_package_version_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(delta_version__gte=1), name="entry_odelta_version_positive"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"offline-delta:{self.package_id}:v{self.delta_version}"


class OfflineOperatorGrant(UUIDPrimaryKeyModel):
    """Server-side record of one signed operator offline grant (binding
    decision P2-B: bounded by the operator session absolute expiry and by
    the time since the last authenticated online contact).

    Issued online only; there is no local sign-in. Rows are never deleted;
    revocation sets `revoked_at` and the device learns it at next contact.
    """

    public_id = models.CharField(max_length=22, unique=True)
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    device_session = models.ForeignKey(
        EntryDeviceSession, on_delete=models.PROTECT, related_name="+"
    )
    operator_session = models.ForeignKey(
        EntryOperatorSession, on_delete=models.PROTECT, related_name="+"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    sensitivity = models.CharField(max_length=16, choices=OfflineSensitivity.choices)
    may_override = models.BooleanField(default=False)
    signing_key_id = models.CharField(max_length=8)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "entry_offline_grant"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("issued_at")),
                name="entry_ogrant_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["device", "revoked_at"], name="entry_ogrant_device_idx"),
            models.Index(fields=["operator_session"], name="entry_ogrant_opsession_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"offline-grant:{self.public_id}"


class EmergencyWipeReason(models.TextChoices):
    DEVICE_LOST = "DEVICE_LOST", _("Device lost")
    DEVICE_STOLEN = "DEVICE_STOLEN", _("Device stolen")
    DEVICE_COMPROMISED = "DEVICE_COMPROMISED", _("Device compromised")
    SECURITY_ORDER = "SECURITY_ORDER", _("Security authority order")


class ExpectedEvidenceLoss(models.TextChoices):
    """What the ordering authority accepted could be lost (binding decision P2-F)."""

    NONE_REPORTED = "NONE_REPORTED", _("No unacknowledged operations reported")
    OPERATIONS_AT_RISK = "OPERATIONS_AT_RISK", _("Unacknowledged operations may be destroyed")
    UNKNOWN = "UNKNOWN", _("Unknown: the device never reported its local store")


class DeviceWipeOrder(UUIDPrimaryKeyModel):
    """One destructive emergency-wipe order (binding decision P2-F). Append-only.

    Distinct from suspension, revocation and "block offline use", none of
    which delete unacknowledged operations. Records the evidence-loss scope
    known at ordering time (the last reported counts, sequence range and
    chain head); the device pre-wipe report, if it manages to send one, is a
    separate `DeviceEvidenceReport`.
    """

    public_id = models.CharField(max_length=22, unique=True)
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    ordered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason_code = models.CharField(max_length=32, choices=EmergencyWipeReason.choices)
    note_encrypted = EncryptedTextField(blank=True, default="")
    confirmed = models.BooleanField()
    mfa_verified_at = models.DateTimeField()
    signing_key_id = models.CharField(max_length=8)
    known_pending_operations = models.PositiveIntegerField(null=True, blank=True)
    known_locked_operations = models.PositiveIntegerField(null=True, blank=True)
    known_sequence_high = models.BigIntegerField(null=True, blank=True)
    known_chain_head = models.CharField(max_length=64, blank=True, default="")
    known_reported_at = models.DateTimeField(null=True, blank=True)
    expected_evidence_loss = models.CharField(max_length=24, choices=ExpectedEvidenceLoss.choices)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "entry_device_wipe_order"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confirmed=True), name="entry_wipe_order_confirmed"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"wipe-order:{self.public_id}"


class DeviceEvidenceReport(UUIDPrimaryKeyModel):
    """Metadata a device reported immediately before executing an emergency
    wipe. Append-only; counts, sequence range and chain head only -- never
    the content of an operation."""

    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    wipe_order = models.ForeignKey(DeviceWipeOrder, on_delete=models.PROTECT, related_name="+")
    pending_operations = models.PositiveIntegerField()
    locked_operations = models.PositiveIntegerField()
    sequence_low = models.BigIntegerField(null=True, blank=True)
    sequence_high = models.BigIntegerField(null=True, blank=True)
    chain_head = models.CharField(max_length=64, blank=True, default="")
    received_at = models.DateTimeField()

    class Meta:
        db_table = "entry_device_evidence_report"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"evidence-report:{self.wipe_order_id}"


class OfflineRequestNonce(models.Model):
    """Single-use record of a device-signed request nonce (Phase 4 Prompt 2).

    A signed device request carries a short-lived server nonce; inserting its
    digest here in the request transaction makes the nonce single-use (a
    replayed request collides on the unique digest). Rows are technical
    bookkeeping, not evidence, and are pruned once expired.
    """

    id = models.BigAutoField(primary_key=True)
    nonce_digest = models.CharField(max_length=64, unique=True)
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    purpose = models.CharField(max_length=24)
    used_at = models.DateTimeField()

    class Meta:
        db_table = "entry_offline_nonce"
        indexes = [models.Index(fields=["used_at"], name="entry_ononce_used_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"nonce:{self.purpose}"


# ---------------------------------------------------------------------------
# Offline synchronization and reconciliation (Phase 4 Prompt 3, ADR-0024)
# ---------------------------------------------------------------------------


class SyncOperationType(models.TextChoices):
    """What a signed offline operation records (`docs/security/offline_sync_contract.md`)."""

    #: An operator decision (Admit / Do not admit / Manual review, or a
    #: supervisor override) on a signature-verified pass the device resolved
    #: from its package.
    ENTRY_DECISION = "ENTRY_DECISION", _("Offline entry decision")
    #: A scan that resolved no pass (invalid, unsupported, other event, not
    #: in the package) or a referral while the offline data had expired.
    VERIFICATION_ATTEMPT = "VERIFICATION_ATTEMPT", _("Offline verification attempt")
    #: Stored for rejected evidence whose declared type is not supported.
    UNSUPPORTED = "UNSUPPORTED", _("Unsupported operation")


class SyncOperationStatus(models.TextChoices):
    """Server outcome of one operation (Schema §11.3, Flow §11.6).

    PENDING is the only non-final status: the evidence is durable, its
    processing has not committed yet, and the device must retry. "Processing"
    exists only inside the processing transaction, and "Duplicate" is an
    acknowledgement outcome (a replay returns the ORIGINAL row's outcome),
    never a row status.
    """

    PENDING = "PENDING", _("Received, not processed yet")
    APPLIED = "APPLIED", _("Applied")
    CONFLICT = "CONFLICT", _("Conflict")
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED", _("Requires reconciliation")
    SECURITY_CONFLICT = "SECURITY_CONFLICT", _("Security conflict")
    REJECTED = "REJECTED", _("Rejected")
    QUARANTINED = "QUARANTINED", _("Quarantined")


FINAL_SYNC_STATUSES: tuple[str, ...] = tuple(
    status for status in SyncOperationStatus.values if status != SyncOperationStatus.PENDING
)
#: Final statuses that always carry a reconciliation case.
CONFLICTED_SYNC_STATUSES: tuple[str, ...] = (
    SyncOperationStatus.CONFLICT,
    SyncOperationStatus.RECONCILIATION_REQUIRED,
    SyncOperationStatus.SECURITY_CONFLICT,
)


class SyncOperation(UUIDPrimaryKeyModel):
    """The durable, immutable inbox row of one signed offline operation
    (Schema §11.3). Never deleted.

    The row is written FIRST, in its own transaction, as soon as the
    operation's device signature, structure and references are verified
    (status PENDING, or directly REJECTED / QUARANTINED). Processing then
    runs in a second transaction and moves PENDING to exactly one final
    status. The `entry_sync_operation_guard` trigger keeps every evidence
    column immutable from insert, the outcome columns immutable once final,
    and lets only the duplicate-submission counter grow.

    `operation_id` is the client's globally unique id: a repeated id never
    creates a second row -- nor, through the unique `EntryEvent.operation_id`
    and `EntryEvent.sync_operation`, a second Entry Event.
    """

    public_id = models.CharField(max_length=22, unique=True)
    operation_id = models.CharField(max_length=36, unique=True)
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    #: The device's local operation store (a new store -- after a wipe or
    #: loss of browser data -- starts a new sequence and hash chain).
    store_id = models.CharField(max_length=22)
    device_sequence = models.BigIntegerField()
    operation_type = models.CharField(max_length=24, choices=SyncOperationType.choices)
    schema_version = models.PositiveSmallIntegerField(null=True, blank=True)
    #: The validated operation body exactly as signed: codes and opaque
    #: identifiers only. An override note is never here, only its salted
    #: digest; the note itself is `note_encrypted`.
    payload_json = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    signature = models.CharField(max_length=128, blank=True, default="")
    signing_key = models.ForeignKey(
        EntryDeviceKey, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    prev_hash = models.CharField(max_length=64, blank=True, default="")
    chain_hash = models.CharField(max_length=64, blank=True, default="")
    note_encrypted = EncryptedTextField(blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField()
    package = models.ForeignKey(
        OfflinePackage, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    package_version = models.BigIntegerField(null=True, blank=True)
    delta_version = models.BigIntegerField(null=True, blank=True)
    grant = models.ForeignKey(
        OfflineOperatorGrant, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    operator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    gate = models.ForeignKey(
        "events.Gate", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    zone = models.ForeignKey(
        "events.Zone", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    digital_entry_pass = models.ForeignKey(
        "badges.DigitalEntryPass", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    status = models.CharField(
        max_length=24, choices=SyncOperationStatus.choices, default=SyncOperationStatus.PENDING
    )
    #: Stable machine-readable outcome (Schema §11.3 `error_code`).
    outcome_code = models.CharField(max_length=40, blank=True, default="")
    conflict_type = models.CharField(max_length=40, blank=True, default="")
    #: What the SERVER knew when it evaluated the operation: result, reason,
    #: blockers, the device's knowledge baseline and the relevant timestamps.
    #: Codes and times only -- never a name, identifier value or reason text.
    server_evaluation = models.JSONField(default=dict)
    flags = models.JSONField(default=list)
    #: The resulting domain record (Schema §11.3): the Entry Event id.
    result_reference = models.UUIDField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    duplicate_submissions = models.PositiveIntegerField(default=0)
    last_duplicate_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "entry_sync_operation"
        constraints = [
            models.UniqueConstraint(
                fields=["device", "store_id", "device_sequence"],
                name="entry_syncop_store_sequence_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(device_sequence__gte=1), name="entry_syncop_sequence_positive"
            ),
            models.CheckConstraint(
                condition=(models.Q(status="PENDING") & models.Q(processed_at__isnull=True))
                | (~models.Q(status="PENDING") & models.Q(processed_at__isnull=False)),
                name="entry_syncop_processed_iff_final",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    operation_type__in=["ENTRY_DECISION", "VERIFICATION_ATTEMPT", "UNSUPPORTED"]
                ),
                name="entry_syncop_type_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "PENDING",
                        "APPLIED",
                        "CONFLICT",
                        "RECONCILIATION_REQUIRED",
                        "SECURITY_CONFLICT",
                        "REJECTED",
                        "QUARANTINED",
                    ]
                ),
                name="entry_syncop_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["device", "status", "occurred_at"], name="entry_syncop_device_idx"
            ),
            models.Index(fields=["event_edition", "status"], name="entry_syncop_event_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"sync-operation:{self.public_id}:{self.status}"


class ReconciliationCaseType(models.TextChoices):
    """Why a supervisor must look at an offline operation (Flow §11.8-§11.9,
    TRD §18.6, approved plan §6 mapping table). Classification codes only --
    never a new business outcome."""

    DUPLICATE_ENTRY = "DUPLICATE_ENTRY", _("Duplicate entry under a single-entry rule")
    DUPLICATE_ADMISSION_ADVISORY = (
        "DUPLICATE_ADMISSION_ADVISORY",
        _("Closely timed admissions on different devices"),
    )
    PASS_REVOKED = "PASS_REVOKED", _("Pass revoked before the offline admission")
    PASS_SUSPENDED = "PASS_SUSPENDED", _("Pass suspended before the offline admission")
    PASS_REPLACED = "PASS_REPLACED", _("Pass replaced before the offline admission")
    WRONG_EVENT = "WRONG_EVENT", _("Pass of another event")
    WRONG_SCOPE = "WRONG_SCOPE", _("Device scope changed before the offline admission")
    PACKAGE_UNUSABLE = "PACKAGE_UNUSABLE", _("Offline data withdrawn before the admission")
    PACKAGE_STALE = "PACKAGE_STALE", _("Admission on stale offline data")
    PACKAGE_EXPIRED = "PACKAGE_EXPIRED", _("Admission on expired offline data")
    ACCESS_CHANGED = "ACCESS_CHANGED", _("Access rule, assignment or registration changed")
    RESTRICTION_ADDED = "RESTRICTION_ADDED", _("Security restriction found at synchronization")
    AUTHORIZATION_WITHDRAWN = (
        "AUTHORIZATION_WITHDRAWN",
        _("Operator authorization withdrawn before the admission"),
    )
    NON_OVERRIDEABLE_RESTRICTION = (
        "NON_OVERRIDEABLE_RESTRICTION",
        _("Override despite a non-overrideable restriction"),
    )
    POLICY_VIOLATION = "POLICY_VIOLATION", _("Offline policy violated")
    OPERATION_ID_REUSED = "OPERATION_ID_REUSED", _("Operation identifier reused with other content")
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION", _("Unsupported or malformed operation")
    SECURITY_REJECTION = "SECURITY_REJECTION", _("Operation failed its security checks")
    QUARANTINED_DEVICE = "QUARANTINED_DEVICE", _("Upload from a suspended or revoked device")
    SEQUENCE_GAP = "SEQUENCE_GAP", _("Missing local records (sequence gap)")
    CHAIN_BREAK = "CHAIN_BREAK", _("Local record chain broken")
    CLOCK_ANOMALY = "CLOCK_ANOMALY", _("Device clock anomaly")


class ReconciliationSeverity(models.TextChoices):
    SECURITY = "SECURITY", _("Security")
    CONFLICT = "CONFLICT", _("Conflict")
    ADVISORY = "ADVISORY", _("Advisory")


class ReconciliationStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    CLOSED = "CLOSED", _("Closed")


class ReconciliationCase(UUIDPrimaryKeyModel, VersionedModel):
    """One conflict an authorized supervisor must review (Schema §11.7).

    Keeps apart what the DEVICE knew (`device_known_state`), what the SERVER
    knew (`server_known_state`) and what PHYSICALLY happened
    (`operational_fact`). Reconciliation never rewrites the original Entry
    Event: resolution is an append-only `ReconciliationAction`, and closing a
    case is the only state change (`entry_reconciliation_case_guard`).
    Scoped by event, venue and gate for the supervisor queue.
    """

    public_id = models.CharField(max_length=22, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    venue = models.ForeignKey("events.Venue", on_delete=models.PROTECT, related_name="+")
    gate = models.ForeignKey("events.Gate", on_delete=models.PROTECT, related_name="+")
    device = models.ForeignKey(EntryDevice, on_delete=models.PROTECT, related_name="+")
    case_type = models.CharField(max_length=40, choices=ReconciliationCaseType.choices)
    severity = models.CharField(max_length=16, choices=ReconciliationSeverity.choices)
    status = models.CharField(
        max_length=8, choices=ReconciliationStatus.choices, default=ReconciliationStatus.OPEN
    )
    sync_operation = models.ForeignKey(
        SyncOperation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    entry_event = models.ForeignKey(
        EntryEvent, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    related_entry_event = models.ForeignKey(
        EntryEvent, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    device_known_state = models.JSONField(default=dict)
    server_known_state = models.JSONField(default=dict)
    operational_fact = models.JSONField(default=dict)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "entry_reconciliation_case"
        permissions = [
            (
                "reconcile_offline_operation",
                "Can review and close offline synchronization reconciliation cases",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status="OPEN")
                    & models.Q(closed_at__isnull=True)
                    & models.Q(closed_by__isnull=True)
                )
                | (
                    models.Q(status="CLOSED")
                    & models.Q(closed_at__isnull=False)
                    & models.Q(closed_by__isnull=False)
                ),
                name="entry_recon_closed_coherent",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "status", "opened_at"], name="entry_recon_event_idx"
            ),
            models.Index(fields=["gate", "status"], name="entry_recon_gate_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"reconciliation-case:{self.public_id}:{self.case_type}:{self.status}"


class ReconciliationCaseOperation(models.Model):
    """Append-only link between a case and every operation it covers (a
    duplicate admission across two devices, the uploads of a quarantined
    device)."""

    id = models.BigAutoField(primary_key=True)
    case = models.ForeignKey(ReconciliationCase, on_delete=models.PROTECT, related_name="links")
    sync_operation = models.ForeignKey(SyncOperation, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField()

    class Meta:
        db_table = "entry_reconciliation_case_operation"
        constraints = [
            models.UniqueConstraint(
                fields=["case", "sync_operation"], name="entry_recon_case_operation_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"reconciliation-link:{self.case_id}:{self.sync_operation_id}"


class ReconciliationActionType(models.TextChoices):
    NOTE = "NOTE", _("Note added")
    CLOSE = "CLOSE", _("Case closed")


class ReconciliationAction(UUIDPrimaryKeyModel):
    """One append-only supervisor action on a case: a note, or the closure.

    The note (the substance of the review) is encrypted at rest; the audit
    trail records only that a note exists."""

    case = models.ForeignKey(ReconciliationCase, on_delete=models.PROTECT, related_name="actions")
    action = models.CharField(max_length=8, choices=ReconciliationActionType.choices)
    note_encrypted = EncryptedTextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField()

    class Meta:
        db_table = "entry_reconciliation_action"
        indexes = [
            models.Index(fields=["case", "created_at"], name="entry_recon_action_case_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"reconciliation-action:{self.case_id}:{self.action}"
