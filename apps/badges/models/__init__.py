"""Digital Entry Pass credential domain (Phase 3 Prompt 2) and generic
physical badge stock (Phase 3 Prompt 3).

New app (ADR-0017). `BadgeType`, `AccessProfile`, and every assignment
table remain owned by `apps.accreditation` exactly as Phase 2 built and
reviewed them -- nothing is moved, and no migration history is
rewritten.

`apps.badges.models.stock` (ADR-0020) adds `StockLocation`, `PrintBatch`,
`StockTransfer`, `BadgeIssuance`, `BadgeStockLedgerEntry`,
`BadgeStockBalance`, `StockReconciliation`, and `BadgeStockOperation` --
generic physical badge production, stock, and issuance only, joining this
app exactly as ADR-0017 anticipated. This module still contains NO entry
device, device scope, device session, entry event, override, security
restriction, offline package, or synchronization model. Those belong to
later Phase 3 prompts and to Phase 4.

Two naming decisions are load-bearing and are recorded in ADR-0018:

* `credential_version` is the monotonic credential number carried in the
  QR and checked during verification;
* `version`, inherited from `VersionedModel`, is the optimistic-concurrency
  lock counter ONLY. It never appears in a QR payload, a selector
  projection, or a participant-facing template.

"Not generated" is the ABSENCE of a `DigitalEntryPass` row. It is
deliberately not a member of `DigitalEntryPassStatus`.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel

# ---------------------------------------------------------------------------
# Verification keys (PUBLIC key material only)
# ---------------------------------------------------------------------------


class VerificationKeyStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    ACTIVE = "ACTIVE", _("Active")
    RETIRED = "RETIRED", _("Retired")
    REVOKED = "REVOKED", _("Revoked")


class VerificationKeyReasonCode(models.TextChoices):
    """Controlled reasons for a verification-key lifecycle change.

    Retirement and revocation are operationally very different acts, so the
    vocabulary keeps them distinguishable in the audit trail: a planned
    rotation must never be indistinguishable from a suspected compromise.
    """

    PLANNED_ROTATION = "PLANNED_ROTATION", _("Planned rotation")
    ROTATION_SUPERSEDED = "ROTATION_SUPERSEDED", _("Superseded by a newer key")
    SUSPECTED_COMPROMISE = "SUSPECTED_COMPROMISE", _("Suspected key compromise")
    CONFIRMED_COMPROMISE = "CONFIRMED_COMPROMISE", _("Confirmed key compromise")
    OPERATOR_ERROR = "OPERATOR_ERROR", _("Published in error")
    PROVIDER_MIGRATION = "PROVIDER_MIGRATION", _("Key provider migration")
    OTHER_APPROVED = "OTHER_APPROVED", _("Other approved reason")


#: The single permitted signing algorithm, stored per key so that
#: verification selects the algorithm from the TRUSTED key record rather
#: than from attacker-supplied header content.
ES256 = "ES256"


class VerificationKey(UUIDPrimaryKeyModel, TimestampedModel):
    """A published PUBLIC verification key for Digital Entry Pass QR codes.

    Private key material never reaches this table. The service layer
    refuses to store any PEM containing a private-key marker, and a
    regression test proves it. Private keys live only behind
    `apps.core.crypto.signing.SigningKeyProvider`, which returns signatures
    and public keys but never private bytes.

    Lifecycle: PENDING -> ACTIVE -> RETIRED, with REVOKED reachable from
    any non-revoked state. A RETIRED key keeps verifying credentials issued
    before rotation for as long as those credentials remain within their own
    validity window; a REVOKED key fails verification immediately for every
    credential bearing its `kid`, regardless of that credential's `exp`.
    """

    key_id = models.CharField(max_length=8, unique=True)
    algorithm = models.CharField(max_length=16, default=ES256)
    public_key_pem = models.TextField()
    # Canonical DER SubjectPublicKeyInfo, base64-encoded for storage. This --
    # not the PEM text -- is the comparison and fingerprint basis: two PEMs
    # differing only in line wrapping, trailing newline, or header spelling
    # canonicalize to identical DER, so key-equality checks cannot be fooled
    # by formatting.
    public_key_der_b64 = models.TextField(blank=True, default="")
    #: SHA-256 over the canonical DER bytes, never over raw PEM formatting.
    public_key_fingerprint = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=VerificationKeyStatus.choices,
        default=VerificationKeyStatus.PENDING,
    )
    not_before = models.DateTimeField(null=True, blank=True)
    not_after = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField()
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    promoted_at = models.DateTimeField(null=True, blank=True)
    promoted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revocation_reason_code = models.CharField(max_length=64, blank=True, default="")
    revocation_reason_text = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "badges_verification_key"
        permissions = [
            ("manage_verificationkey", "Can publish, promote, retire and revoke verification keys"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["key_id"], name="bdg_verifkey_key_id_uq"),
            # `algorithm` is always ES256, so a partial unique index over it
            # restricted to ACTIVE rows is exactly the "at most one current
            # signing key" rule expressed at the database level.
            models.UniqueConstraint(
                fields=["algorithm"],
                condition=models.Q(status=VerificationKeyStatus.ACTIVE),
                name="bdg_verifkey_one_active_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(algorithm=ES256),
                name="bdg_verifkey_es256_only",
            ),
            models.CheckConstraint(
                condition=models.Q(not_after__isnull=True)
                | models.Q(not_before__isnull=True)
                | models.Q(not_after__gt=models.F("not_before")),
                name="bdg_verifkey_window_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "not_before"], name="bdg_verifkey_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.key_id}:{self.status}"


# ---------------------------------------------------------------------------
# Event-scoped participant pseudonym (the QR `pid` claim)
# ---------------------------------------------------------------------------


class ParticipantEventPseudonym(UUIDPrimaryKeyModel, TimestampedModel):
    """Stable, random, event-scoped pseudonym for one Person (TRD s11.3 `pid`).

    Deliberately a stored random value rather than an HMAC of the Person's
    identifier: that avoids introducing a fifth versioned-key family purely
    for a pseudonym, and it means no key rotation can ever invalidate an
    already-issued credential. It follows the same keyless reasoning the
    project accepted in ADR-0016 for invitation tokens.

    The value is unlinkable across event editions and reveals nothing about
    the person. It is never derived from a primary key, a UUIDv7, a
    registration reference, or any identity value.
    """

    person = models.ForeignKey("people.Person", on_delete=models.PROTECT, related_name="+")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    pseudonym = models.CharField(max_length=22, unique=True)

    class Meta:
        db_table = "badges_participant_event_pseudonym"
        constraints = [
            models.UniqueConstraint(
                fields=["person", "event_edition"], name="bdg_pseudonym_person_event_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - never the person
        return self.pseudonym


# ---------------------------------------------------------------------------
# Credential series (stable identity across replacement)
# ---------------------------------------------------------------------------


class PassCredentialSeries(UUIDPrimaryKeyModel, TimestampedModel):
    """One credential series per exact Registration Context.

    Holds the two identifiers that stay STABLE across replacement -- the
    opaque `public_id` and the participant-facing `fallback_reference` --
    so a participant who printed or wrote down their reference keeps a
    working locator after their credential is replaced.

    Neither identifier contains or derives from a database primary key, a
    UUIDv7, participant identity, the registration reference, NIN or
    passport data, or email, phone, name, or organization data. Both are
    drawn from `secrets`.

    `next_credential_version` is allocated under `SELECT ... FOR UPDATE`,
    reusing the concurrency-safe sequence pattern this project already
    accepted for `EventEdition.next_registration_sequence`.
    """

    registration = models.OneToOneField(
        "registrations.Registration", on_delete=models.PROTECT, related_name="pass_series"
    )
    public_id = models.CharField(max_length=22, unique=True)
    fallback_reference = models.CharField(max_length=16, unique=True)
    next_credential_version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "badges_pass_credential_series"
        constraints = [
            models.UniqueConstraint(fields=["registration"], name="bdg_series_registration_uq"),
            models.UniqueConstraint(fields=["public_id"], name="bdg_series_public_id_uq"),
            models.UniqueConstraint(
                fields=["fallback_reference"], name="bdg_series_fallback_ref_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(next_credential_version__gte=1),
                name="bdg_series_next_version_positive",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.public_id


# ---------------------------------------------------------------------------
# Digital Entry Pass credential versions
# ---------------------------------------------------------------------------


class DigitalEntryPassStatus(models.TextChoices):
    """The complete persisted status vocabulary.

    There is deliberately no `NOT_GENERATED` and no `READY`: absence of a
    row is "not generated", and `INACTIVE` is the generated-but-not-yet-
    activated state.
    """

    INACTIVE = "INACTIVE", _("Inactive")
    ACTIVE = "ACTIVE", _("Active")
    SUSPENDED = "SUSPENDED", _("Suspended")
    REVOKED = "REVOKED", _("Revoked")
    EXPIRED = "EXPIRED", _("Expired")
    REPLACED = "REPLACED", _("Replaced")


#: Statuses in which a credential is still the registration's current one.
#: The database constraint and the service layer both read this single
#: definition, so they cannot disagree about what "current" means.
NON_TERMINAL_STATUSES: tuple[str, ...] = (
    DigitalEntryPassStatus.INACTIVE,
    DigitalEntryPassStatus.ACTIVE,
    DigitalEntryPassStatus.SUSPENDED,
)

#: Statuses that are final for a credential version.
TERMINAL_STATUSES: tuple[str, ...] = (
    DigitalEntryPassStatus.REVOKED,
    DigitalEntryPassStatus.EXPIRED,
    DigitalEntryPassStatus.REPLACED,
)


class PassReasonCode(models.TextChoices):
    """Controlled reason vocabulary for credential lifecycle changes."""

    INITIAL_ISSUE = "INITIAL_ISSUE", _("Initial issue")
    REPLACEMENT_ISSUE = "REPLACEMENT_ISSUE", _("Replacement issue")
    ASSIGNMENT_CHANGED = "ASSIGNMENT_CHANGED", _("Assignment changed")
    PARTICIPANT_REQUEST = "PARTICIPANT_REQUEST", _("Participant request")
    SECURITY_CONCERN = "SECURITY_CONCERN", _("Security concern")
    ADMINISTRATIVE_ERROR = "ADMINISTRATIVE_ERROR", _("Administrative error")
    LOST_OR_COMPROMISED = "LOST_OR_COMPROMISED", _("Lost or compromised")
    REGISTRATION_CLOSED = "REGISTRATION_CLOSED", _("Registration closed")
    KEY_COMPROMISE = "KEY_COMPROMISE", _("Signing key compromise")
    VALIDITY_ENDED = "VALIDITY_ENDED", _("Validity period ended")
    OTHER_APPROVED = "OTHER_APPROVED", _("Other approved reason")


class DigitalEntryPass(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """One credential version.

    Each row is historical evidence: once written, the signed material
    (`jti`, `nonce`, `payload_hash`, `signing_key_id`, the denormalized
    snapshot codes, the assignment foreign keys, and the validity window)
    is never mutated. Only the controlled lifecycle fields -- `status`, the
    lifecycle timestamps and actors, the status reason, `replaced_by`, and
    the `version` lock counter -- change after creation.

    The assignment snapshot columns are non-nullable on purpose: generation
    requires a current role, badge, and access-profile assignment, so a row
    without all three could never be legitimately created, and allowing NULL
    would make an impossible state representable.
    """

    series = models.ForeignKey(
        PassCredentialSeries, on_delete=models.PROTECT, related_name="credentials"
    )
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="entry_passes"
    )
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )

    credential_version = models.PositiveIntegerField()
    jti = models.CharField(max_length=22, unique=True)

    status = models.CharField(
        max_length=16,
        choices=DigitalEntryPassStatus.choices,
        default=DigitalEntryPassStatus.INACTIVE,
    )

    # Assignment snapshot -- the exact assignments current at issue time.
    role_assignment = models.ForeignKey(
        "accreditation.ParticipantRoleAssignment", on_delete=models.PROTECT, related_name="+"
    )
    badge_assignment = models.ForeignKey(
        "accreditation.BadgeTypeAssignment", on_delete=models.PROTECT, related_name="+"
    )
    access_assignment = models.ForeignKey(
        "accreditation.AccessProfileAssignment", on_delete=models.PROTECT, related_name="+"
    )

    # Denormalized codes actually carried in the signed payload. Held here
    # so the signed snapshot can be re-derived and compared byte-for-byte
    # even if the underlying reference data is later renamed.
    event_code = models.CharField(max_length=32)
    badge_type_code = models.CharField(max_length=64)
    access_profile_code = models.CharField(max_length=64)
    # The remaining two signed claims, snapshotted at issuance. Before the
    # correction pass these were read back from `BadgeTypeAssignment` and
    # `ParticipantEventPseudonym` at display time, which meant a later change
    # to either mutable row silently changed the bytes a "re-signed" token
    # carried. Every one of the eleven signed claims now comes from this row
    # and this row alone.
    badge_assignment_public_reference = models.CharField(max_length=22, blank=True, default="")
    participant_event_pseudonym = models.CharField(max_length=22, blank=True, default="")

    payload_version = models.PositiveSmallIntegerField()
    signing_key_id = models.CharField(max_length=8)
    payload_hash = models.CharField(max_length=64)
    nonce = models.CharField(max_length=16)
    #: The ES256 signature produced once, at issuance, hex-encoded (64 raw
    #: bytes -> 128 hex characters). Together with the canonical header --
    #: derived deterministically from `signing_key_id` -- and the canonical
    #: payload -- derived deterministically from the snapshot columns above --
    #: this reconstructs the originally issued compact JWS byte-for-byte
    #: WITHOUT the private key. The private key is therefore needed exactly
    #: once per credential, and an issued pass keeps working after its signing
    #: key is retired and the private material removed.
    signature_hex = models.CharField(max_length=128, blank=True, default="")

    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()

    reason_code = models.CharField(
        max_length=32, choices=PassReasonCode.choices, default=PassReasonCode.INITIAL_ISSUE
    )
    reason_text = models.CharField(max_length=300, blank=True, default="")
    status_reason_code = models.CharField(
        max_length=32, choices=PassReasonCode.choices, blank=True, default=""
    )
    status_reason_text = models.CharField(max_length=300, blank=True, default="")

    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resumed_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    replaced_at = models.DateTimeField(null=True, blank=True)
    replaced_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expired_at = models.DateTimeField(null=True, blank=True)

    replaced_by = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replaces"
    )

    class Meta:
        db_table = "badges_digital_entry_pass"
        permissions = [
            ("activate_digitalentrypass", "Can activate a Digital Entry Pass"),
            ("suspend_digitalentrypass", "Can suspend a Digital Entry Pass"),
            ("resume_digitalentrypass", "Can resume a suspended Digital Entry Pass"),
            ("revoke_digitalentrypass", "Can revoke or replace a Digital Entry Pass"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "credential_version"], name="bdg_pass_series_credver_uq"
            ),
            models.UniqueConstraint(fields=["jti"], name="bdg_pass_jti_uq"),
            # One current credential per registration, across all three
            # non-terminal statuses. The tuple above is the single source of
            # truth this condition is built from.
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status__in=NON_TERMINAL_STATUSES),
                name="bdg_pass_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__gt=models.F("valid_from")),
                name="bdg_pass_valid_range_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(credential_version__gte=1),
                name="bdg_pass_credver_positive_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(payload_version__gte=1),
                name="bdg_pass_payload_version_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "status"], name="bdg_pass_reg_status_idx"),
            models.Index(fields=["series", "credential_version"], name="bdg_pass_series_ver_idx"),
            models.Index(
                fields=["event_edition", "status", "valid_until"], name="bdg_pass_event_valid_idx"
            ),
            models.Index(fields=["signing_key_id"], name="bdg_pass_signing_key_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - never a participant value
        return f"pass:{self.jti}:v{self.credential_version}:{self.status}"

    @property
    def is_current(self) -> bool:
        """True while this credential version is the registration's current one."""
        return self.status in NON_TERMINAL_STATUSES

    @property
    def is_terminal(self) -> bool:
        """True once this credential version can never change status again."""
        return self.status in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Lifecycle idempotency ledger
# ---------------------------------------------------------------------------


class PassLifecycleOperationType(models.TextChoices):
    GENERATE = "GENERATE", _("Generate")
    ACTIVATE = "ACTIVATE", _("Activate")
    SUSPEND = "SUSPEND", _("Suspend")
    RESUME = "RESUME", _("Resume")
    REVOKE = "REVOKE", _("Revoke")
    REPLACE = "REPLACE", _("Replace")
    EXPIRE = "EXPIRE", _("Expire")
    KEY_PUBLISH = "KEY_PUBLISH", _("Publish verification key")
    KEY_PROMOTE = "KEY_PROMOTE", _("Promote verification key")
    KEY_RETIRE = "KEY_RETIRE", _("Retire verification key")
    KEY_REVOKE = "KEY_REVOKE", _("Revoke verification key")


class PassLifecycleOperation(models.Model):
    """Append-only idempotency record for one lifecycle command.

    `operation_id` is supplied by the caller, must be cryptographically
    random, and is globally unique. A retry carrying the same
    `operation_id` returns the original outcome instead of performing the
    command a second time.

    Critically, the identifier is **bound to one exact command**. Uniqueness
    on `operation_id` alone is not enough: it makes a replayed identifier
    return whatever command happened to claim it first, so a suspend form
    replayed against a revoke endpoint would quietly report the suspend's
    outcome. `command_fingerprint` closes that hole by covering the
    operation type, the acting user, the exact target, and the material
    request parameters. A reused identifier whose fingerprint differs is a
    conflict, never a replay.

    Rows are written inside the SAME transaction as the state change they
    describe, never deferred to `transaction.on_commit`.
    """

    id = models.BigAutoField(primary_key=True)
    operation_id = models.CharField(max_length=64, unique=True)
    operation_type = models.CharField(max_length=24, choices=PassLifecycleOperationType.choices)
    #: SHA-256 over the canonical `(type, actor, target, parameters)` tuple.
    command_fingerprint = models.CharField(max_length=64, blank=True, default="")
    #: Stable identity of the object the command acts on, so a replay can be
    #: matched without loading the target row.
    target_type = models.CharField(max_length=32, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    series = models.ForeignKey(
        PassCredentialSeries, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    previous_pass = models.ForeignKey(
        DigitalEntryPass, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resulting_pass = models.ForeignKey(
        DigitalEntryPass, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    verification_key = models.ForeignKey(
        "badges.VerificationKey",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "badges_pass_lifecycle_operation"
        constraints = [
            models.UniqueConstraint(fields=["operation_id"], name="bdg_pass_operation_id_uq"),
        ]
        indexes = [
            models.Index(fields=["series", "created_at"], name="bdg_pass_op_series_idx"),
            models.Index(fields=["target_type", "target_id"], name="bdg_pass_op_target_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.operation_type}:{self.operation_id}"


# ---------------------------------------------------------------------------
# Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
# ---------------------------------------------------------------------------

from apps.badges.models.stock import (  # noqa: E402, F401 -- re-exported for `apps.badges.models.*`
    ISSUANCE_TERMINAL_STATUSES,
    PRINT_BATCH_TRANSITIONS,
    BadgeIssuance,
    BadgeIssuanceReasonCode,
    BadgeIssuanceStatus,
    BadgeStockAllocation,
    BadgeStockBalance,
    BadgeStockEntryType,
    BadgeStockLedgerEntry,
    BadgeStockOperation,
    PrintBatch,
    PrintBatchStatus,
    StockAdjustmentReasonCode,
    StockAllocationPurpose,
    StockAllocationStatus,
    StockLocation,
    StockLocationType,
    StockOperationType,
    StockReconciliation,
    StockTransfer,
    StockTransferStatus,
)
