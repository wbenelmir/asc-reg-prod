"""Person, account, contact and identity model (Schema §4).

`IdentityVerificationAttempt` is added later by `people.0002`, once
`registrations.Registration` exists to optionally reference (Schema §7.1).

`people.0003` (IDV, amendment A-13, ADR-0026) adds the post-submission identity
verification: one `IdentityVerification` per submitted Registration (its own
state machine, separate from participation status), append-only
`IdentityRevision` snapshots, durable `IdentityVerificationJob` work items,
append-only `IdentityDecision` staff history, and the minimum retained official
facts on `IdentityVerificationAttempt`.

`people.0005` (owner decision IDV-Q3, 2026-10-02) adds `NinExemption`, the
staff-granted documentary route for one Algerian draft without a usable NIN,
and the choice values it needs (route, identifier type, source, reason).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.fields import BlindIndexField, EncryptedTextField
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel


class PersonStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    MERGED = "MERGED", "Merged"
    ANONYMIZED = "ANONYMIZED", "Anonymized"
    ARCHIVED = "ARCHIVED", "Archived"


class PersonSex(models.TextChoices):
    UNSPECIFIED = "UNSPECIFIED", "Unspecified"
    MALE = "MALE", "Male"
    FEMALE = "FEMALE", "Female"
    OTHER = "OTHER", "Other"


class PersonRetentionClass(models.TextChoices):
    STANDARD = "STANDARD", "Standard"
    EXTENDED = "EXTENDED", "Extended"
    LEGAL_HOLD = "LEGAL_HOLD", "Legal hold"


class Person(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """The canonical, event-independent natural-person record (Schema §4.2).

    No official identifier (NIN/passport) is stored directly here --
    `IdentityIdentifier` is a separate, independently encrypted table.
    """

    status = models.CharField(
        max_length=16, choices=PersonStatus.choices, default=PersonStatus.ACTIVE
    )
    display_name = models.CharField(max_length=200)
    birth_date = models.DateField(null=True, blank=True)
    birth_country = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    birth_place = models.CharField(max_length=200, blank=True, default="")
    nationality = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    sex_code = models.CharField(max_length=16, choices=PersonSex.choices, blank=True, default="")
    preferred_language = models.CharField(max_length=8, default="en")
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="merged_persons"
    )
    retention_class = models.CharField(
        max_length=16, choices=PersonRetentionClass.choices, default=PersonRetentionClass.STANDARD
    )
    purge_after = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "people_person"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(merged_into=models.F("id")),
                name="ppl_person_no_self_merge",
            ),
        ]
        indexes = [models.Index(fields=["status"], name="ppl_person_status_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.display_name


class PersonNameType(models.TextChoices):
    LEGAL = "LEGAL", "Legal"
    PREFERRED = "PREFERRED", "Preferred"
    FORMER = "FORMER", "Former"


class PersonNameSource(models.TextChoices):
    APPLICANT = "APPLICANT", "Applicant"
    VERIFICATION_SERVICE = "VERIFICATION_SERVICE", "Verification service"
    OPERATOR_CORRECTION = "OPERATOR_CORRECTION", "Operator correction"


class PersonName(UUIDPrimaryKeyModel, TimestampedModel):
    """Structured, multi-script name (Schema §4.3)."""

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="names")
    name_type = models.CharField(max_length=16, choices=PersonNameType.choices)
    script_code = models.CharField(
        max_length=8, default="Latn", help_text="ISO 15924, e.g. Latn, Arab."
    )
    given_names = models.CharField(max_length=200)
    family_name = models.CharField(max_length=200)
    full_name = models.CharField(max_length=400)
    normalized_search_name = models.CharField(max_length=400, blank=True, default="")
    is_current = models.BooleanField(default=True)
    source = models.CharField(max_length=24, choices=PersonNameSource.choices)

    class Meta:
        db_table = "people_person_name"
        constraints = [
            models.UniqueConstraint(
                fields=["person", "script_code"],
                condition=models.Q(is_current=True, name_type=PersonNameType.LEGAL),
                name="ppl_pname_cur_legal_uq",
            ),
        ]
        indexes = [models.Index(fields=["person", "name_type"], name="ppl_pname_type_idx")]


class ParticipantAccountStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ACTIVE = "ACTIVE", "Active"
    LOCKED = "LOCKED", "Locked"
    DISABLED = "DISABLED", "Disabled"


class ParticipantAccount(UUIDPrimaryKeyModel, TimestampedModel):
    """Passwordless participant authentication identity (Schema §4.4).

    `person` is a `OneToOneField` -- an unconditional, structural uniqueness
    guarantee that at most one row exists per Person across every status,
    per the fixed product rule. This is NOT a partial constraint
    filtered by status, so disabling/locking an account or replacing its
    email can never create a second row for the same Person.
    """

    person = models.OneToOneField(
        Person, on_delete=models.PROTECT, related_name="participant_account"
    )
    primary_login_contact = models.ForeignKey(
        "people.ContactPoint",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=ParticipantAccountStatus.choices,
        default=ParticipantAccountStatus.PENDING,
    )
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "people_participant_account"


class ContactPointType(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    MOBILE = "MOBILE", "Mobile"


class ContactPointStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REPLACED = "REPLACED", "Replaced"
    BOUNCED = "BOUNCED", "Bounced"
    DISABLED = "DISABLED", "Disabled"


class ContactPoint(UUIDPrimaryKeyModel, TimestampedModel):
    """Email or mobile contact destination (Schema §4.5).

    `value_hash_key_version` is not in the Schema's illustrative field
    table but is required by ADR-0006's versioned-blind-index rotation
    contract, mirroring `IdentityIdentifier.hash_key_version`.
    """

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="contact_points")
    type = models.CharField(max_length=8, choices=ContactPointType.choices)
    value_encrypted = EncryptedTextField()
    value_hash = BlindIndexField()
    value_hash_key_version = models.SmallIntegerField()
    masked_value = models.CharField(max_length=200)
    country_code = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_primary = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    login_enabled = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16, choices=ContactPointStatus.choices, default=ContactPointStatus.ACTIVE
    )

    class Meta:
        db_table = "people_contact_point"
        constraints = [
            # Schema §4.5/§16.2: one active login-enabled VERIFIED email hash
            # maps to at most one row globally -- enforced on the blind
            # index only, never on the encrypted plaintext (Schema §16.2).
            models.UniqueConstraint(
                fields=["value_hash"],
                condition=models.Q(
                    status=ContactPointStatus.ACTIVE,
                    login_enabled=True,
                    is_verified=True,
                    type=ContactPointType.EMAIL,
                ),
                name="ppl_contact_active_login_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["person", "type"], name="ppl_contact_person_type_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the raw value
        return self.masked_value


class IdentityVerificationMethod(models.TextChoices):
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE", "External service"
    MANUAL_DOCUMENT = "MANUAL_DOCUMENT", "Manual document"
    SUPERVISED_CORRECTION = "SUPERVISED_CORRECTION", "Supervised correction"


class IdentityVerificationResult(models.TextChoices):
    MATCH = "MATCH", "Match"
    NO_MATCH = "NO_MATCH", "No match"
    INCONCLUSIVE = "INCONCLUSIVE", "Inconclusive"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"
    ERROR = "ERROR", "Error"


class IdentifierType(models.TextChoices):
    NIN = "NIN", "National identity number"
    PASSPORT = "PASSPORT", "Passport"
    # IDV-Q3: the document number of an Algerian national identity card,
    # declared only on the staff-granted NIN exemption route. Never a NIN.
    NATIONAL_ID_CARD = "NATIONAL_ID_CARD", "National identity card number"


class IdentifierStatus(models.TextChoices):
    DECLARED = "DECLARED", "Declared"
    VERIFIED = "VERIFIED", "Verified"
    REVOKED = "REVOKED", "Revoked"
    EXPIRED = "EXPIRED", "Expired"
    REPLACED = "REPLACED", "Replaced"


class IdentityIdentifier(UUIDPrimaryKeyModel, TimestampedModel):
    """Official identifier, stored separately from Person and Registration (Schema §4.6)."""

    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name="identity_identifiers"
    )
    identifier_type = models.CharField(max_length=16, choices=IdentifierType.choices)
    country_code = models.ForeignKey("core.Country", on_delete=models.PROTECT, related_name="+")
    value_encrypted = EncryptedTextField()
    search_hash = BlindIndexField()
    hash_key_version = models.SmallIntegerField()
    masked_value = models.CharField(max_length=200)
    issued_at = models.DateField(null=True, blank=True)
    expires_at = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=IdentifierStatus.choices, default=IdentifierStatus.DECLARED
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "people_identity_identifier"
        constraints = [
            # Schema §4.6/§16.1/§16.2: verified active NIN/passport unique by
            # type, issuing country and blind index -- partial index over
            # VERIFIED rows only. Declared/inconclusive rows never collide.
            models.UniqueConstraint(
                fields=["identifier_type", "country_code", "search_hash"],
                condition=models.Q(status=IdentifierStatus.VERIFIED),
                name="ppl_identid_verified_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["identifier_type", "country_code", "search_hash"],
                name="ppl_identid_lookup_idx",
            ),
            models.Index(fields=["person"], name="ppl_identid_person_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the raw value
        return self.masked_value


class IdentityVerificationAttempt(UUIDPrimaryKeyModel):
    """Immutable record of one identity-verification attempt (Schema §7.1).

    Added by `people.0002`, once `registrations.Registration` exists to
    optionally reference. Full upstream payloads are never retained --
    `provider_metadata` is bounded, redacted, technical metadata only.
    Append-only: no `updated_at`.

    `people.0003` binds each attempt to the exact `IdentityRevision` it checked
    and adds the minimum retained official facts (A13-03): the official Latin
    names, the normalized official birth date only when it was compared
    (`presume` false), the raw official date text only when it could not be
    parsed, the parsed `presume` flag and the comparison outcomes. They are
    encrypted at rest. Nothing else from a response is stored. An attempt that
    arrived for a superseded revision is kept with `applied=False`.
    """

    identity_identifier = models.ForeignKey(
        IdentityIdentifier, on_delete=models.PROTECT, related_name="verification_attempts"
    )
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="verification_attempts",
    )
    method = models.CharField(max_length=24, choices=IdentityVerificationMethod.choices)
    provider_code = models.CharField(max_length=64, blank=True, default="")
    request_reference = models.CharField(max_length=200, blank=True, default="")
    result = models.CharField(max_length=16, choices=IdentityVerificationResult.choices)
    matched_fields = models.JSONField(default=dict, blank=True)
    provider_metadata = models.JSONField(default=dict, blank=True)
    attempted_at = models.DateTimeField()
    performed_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # --- people.0003 (IDV) ---
    revision = models.ForeignKey(
        "people.IdentityRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    job = models.ForeignKey(
        "people.IdentityVerificationJob",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="provider_attempts",
    )
    attempt_number = models.PositiveSmallIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=24, blank=True, default="")
    reason_code = models.CharField(max_length=40, blank=True, default="")
    detail_code = models.CharField(max_length=40, blank=True, default="")
    is_official_provider = models.BooleanField(default=False)
    applied = models.BooleanField(default=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    comparison = models.JSONField(default=dict, blank=True)
    presume_flag = models.CharField(max_length=16, blank=True, default="")
    policy_basis = models.CharField(max_length=64, blank=True, default="")
    official_family_name_latin = EncryptedTextField(blank=True, default="")
    official_given_names_latin = EncryptedTextField(blank=True, default="")
    official_birth_date = EncryptedTextField(blank=True, default="")
    official_birth_date_text = EncryptedTextField(blank=True, default="")
    evidence_document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "people_identity_verification_attempt"
        indexes = [
            models.Index(fields=["identity_identifier"], name="ppl_verifattempt_identid_idx"),
            models.Index(fields=["registration"], name="ppl_verifattempt_reg_idx"),
            models.Index(fields=["revision"], name="ppl_verifattempt_rev_idx"),
        ]


# ---------------------------------------------------------------------------
# Post-submission identity verification (IDV-1 to IDV-4, amendment A-13,
# ADR-0026). Identity status is its own state machine: it never approves
# participation, and the participant's public status never shows it.
# ---------------------------------------------------------------------------


class IdentityRoute(models.TextChoices):
    NIN = "NIN", _("Algerian national identity number")
    PASSPORT = "PASSPORT", _("Foreign passport")
    # IDV-Q3: an Algerian national without a usable NIN, on a staff-granted
    # exemption; manual documentary review only, never a ministry check.
    NIN_EXEMPTION = "NIN_EXEMPTION", _("Algerian document without NIN (staff exemption)")


class IdentityStatus(models.TextChoices):
    PENDING = "PENDING", _("Verification pending")
    API_VERIFIED = "API_VERIFIED", _("Verified by the ministry service")
    MANUAL_REVIEW = "MANUAL_REVIEW", _("Needs manual review")
    MANUALLY_VERIFIED = "MANUALLY_VERIFIED", _("Verified manually")
    RETURNED_FOR_CORRECTION = "RETURNED_FOR_CORRECTION", _("Returned for correction")
    REJECTED = "REJECTED", _("Rejected")

    @classmethod
    def open_statuses(cls) -> tuple[str, ...]:
        """Statuses of a claim still in progress (counted as a pending duplicate)."""
        return (cls.PENDING, cls.MANUAL_REVIEW, cls.RETURNED_FOR_CORRECTION)

    @classmethod
    def verified_statuses(cls) -> tuple[str, ...]:
        return (cls.API_VERIFIED, cls.MANUALLY_VERIFIED)


class VerificationSource(models.TextChoices):
    MINISTRY_API = "MINISTRY_API", _("Ministry service")
    SIMULATED_API = "SIMULATED_API", _("Development simulation (not official)")
    MANUAL_NATIONAL_ID_CARD = (
        "MANUAL_NATIONAL_ID_CARD",
        _("Manual review of the national identity card"),
    )
    MANUAL_PASSPORT = "MANUAL_PASSPORT", _("Manual review of the passport identity page")
    STAFF_EXCEPTION = "STAFF_EXCEPTION", _("Staff-assisted exception")
    # IDV-Q3.
    MANUAL_NIN_EXEMPTION = (
        "MANUAL_NIN_EXEMPTION",
        _("Manual documentary review (NIN exemption)"),
    )


class IdentityReasonCode(models.TextChoices):
    """Safe reason codes for manual review. Never shown to the participant."""

    NOT_FOUND = "NOT_FOUND", _("The ministry service did not find this NIN")
    PROVIDER_IDENTITY_MISMATCH = (
        "PROVIDER_IDENTITY_MISMATCH",
        _("The service answered for a different NIN"),
    )
    DATA_MISMATCH = "DATA_MISMATCH", _("Names or birth date differ from the official record")
    AMBIGUOUS_DATE = "AMBIGUOUS_DATE", _("The official birth date could not be read safely")
    PRESUME_FLAG_UNRECOGNIZED = (
        "PRESUME_FLAG_UNRECOGNIZED",
        _("The presumed-date flag is missing or not recognized"),
    )
    INVALID_RESPONSE = "INVALID_RESPONSE", _("The service response was invalid")
    PROVIDER_AUTH_ERROR = "PROVIDER_AUTH_ERROR", _("The service refused authentication")
    PROVIDER_UNAVAILABLE = (
        "PROVIDER_UNAVAILABLE",
        _("The service was unavailable after every retry"),
    )
    PROVIDER_NOT_CONFIGURED = (
        "PROVIDER_NOT_CONFIGURED",
        _("The ministry service is not configured"),
    )
    DUPLICATE_IDENTIFIER = (
        "DUPLICATE_IDENTIFIER",
        _("Another person's registration uses this identifier"),
    )
    FOREIGN_PASSPORT_REVIEW = (
        "FOREIGN_PASSPORT_REVIEW",
        _("Foreign participant: passport review required"),
    )
    PARTICIPANT_RESUBMITTED = (
        "PARTICIPANT_RESUBMITTED",
        _("The participant resubmitted after a correction request"),
    )
    IDENTITY_DATA_CHANGED = (
        "IDENTITY_DATA_CHANGED",
        _("The identity data changed while a check was pending"),
    )
    # IDV-C1 (R-IDV-06, R-IDV-04).
    REGISTRATION_CLOSED = (
        "REGISTRATION_CLOSED",
        _("The registration was withdrawn, cancelled or not approved"),
    )
    PROVIDER_ATTEMPTS_EXHAUSTED = (
        "PROVIDER_ATTEMPTS_EXHAUSTED",
        _("Automatic checks stopped after the attempt limit"),
    )
    # IDV-Q3.
    NIN_EXEMPTION_REVIEW = (
        "NIN_EXEMPTION_REVIEW",
        _("Algerian participant without a usable NIN: documentary review"),
    )


class IdentityVerification(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """The identity verification of one submitted Registration (ADR-0026).

    Created atomically with the final submission. `event_edition` and
    `organization` are copied from the Registration so scope filtering works on
    this table, like `reviews.ReviewCase`. `version` is the optimistic
    concurrency token for every staff command and the worker. `reason_code` is
    the current manual-review reason; `verification_source` is set only while
    the identity is verified.
    """

    registration = models.OneToOneField(
        "registrations.Registration", on_delete=models.PROTECT, related_name="identity_verification"
    )
    person = models.ForeignKey(
        Person, on_delete=models.PROTECT, related_name="identity_verifications"
    )
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    nationality = models.ForeignKey("core.Country", on_delete=models.PROTECT, related_name="+")
    route = models.CharField(max_length=16, choices=IdentityRoute.choices)
    status = models.CharField(
        max_length=32, choices=IdentityStatus.choices, default=IdentityStatus.PENDING
    )
    reason_code = models.CharField(
        max_length=40, choices=IdentityReasonCode.choices, blank=True, default=""
    )
    verification_source = models.CharField(
        max_length=32, choices=VerificationSource.choices, blank=True, default=""
    )
    current_revision = models.ForeignKey(
        "people.IdentityRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    submitted_at = models.DateTimeField()
    status_changed_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "people_identity_verification"
        # Dedicated, separately grantable permissions (A13-05). The default
        # `view_identityverification` opens the queue and the review screen
        # with masked identifiers; every other capability is its own grant.
        permissions = [
            ("view_identity_evidence", "Can view identity evidence and full identifiers"),
            ("verify_identity_manually", "Can verify an identity manually from evidence"),
            ("correct_identity_nin", "Can correct a NIN from evidence and recheck it"),
            ("return_identity_for_correction", "Can return an identity for correction"),
            ("reject_identity", "Can finally reject an identity"),
            ("apply_identity_exception", "Can verify an identity by staff-assisted exception"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status__in=["API_VERIFIED", "MANUALLY_VERIFIED"],
                        verification_source__gt="",
                    )
                    | (
                        ~models.Q(status__in=["API_VERIFIED", "MANUALLY_VERIFIED"])
                        & models.Q(verification_source="")
                    )
                ),
                name="ppl_idv_source_iff_verified",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "status", "status_changed_at"],
                name="ppl_idv_event_status_idx",
            ),
            models.Index(fields=["organization", "status"], name="ppl_idv_org_status_idx"),
            models.Index(fields=["status", "status_changed_at"], name="ppl_idv_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never an identity value
        return f"identity-verification:{self.pk}:{self.status}"

    @property
    def is_verified(self) -> bool:
        return self.status in IdentityStatus.verified_statuses()


class IdentityRevisionSource(models.TextChoices):
    SUBMISSION = "SUBMISSION", _("Final submission")
    PARTICIPANT_RESUBMISSION = "PARTICIPANT_RESUBMISSION", _("Participant correction")
    STAFF_NIN_CORRECTION = "STAFF_NIN_CORRECTION", _("Staff NIN correction")
    STAFF_RECHECK = "STAFF_RECHECK", _("Staff recheck")
    # IDV-C1 (R-IDV-01, R-IDV-02).
    LINKED_IDENTITY_CHANGE = (
        "LINKED_IDENTITY_CHANGE",
        _("Identifier corrected in another registration of the same person"),
    )
    OFFICIAL_NAME_NORMALIZATION = (
        "OFFICIAL_NAME_NORMALIZATION",
        _("Official name form applied (case or spacing only)"),
    )


class IdentityRevision(UUIDPrimaryKeyModel):
    """One submitted identity snapshot (append-only).

    Holds no clear identity value: `fingerprint` is a keyed HMAC of the
    canonical identity (route, identifier, names, birth date), so a worker can
    prove that the identity it checked is still the current one without a
    second copy of personal data. `changed_fields` names the fields that
    changed from the previous revision, never their values.
    """

    verification = models.ForeignKey(
        IdentityVerification, on_delete=models.PROTECT, related_name="revisions"
    )
    number = models.PositiveIntegerField()
    source = models.CharField(max_length=32, choices=IdentityRevisionSource.choices)
    identifier = models.ForeignKey(
        IdentityIdentifier, on_delete=models.PROTECT, related_name="identity_revisions"
    )
    fingerprint = models.CharField(max_length=64)
    fingerprint_key_version = models.SmallIntegerField()
    changed_fields = models.JSONField(default=list, blank=True)
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by_person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "people_identity_revision"
        constraints = [
            models.UniqueConstraint(
                fields=["verification", "number"], name="ppl_idrev_verification_number_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"identity-revision:{self.verification_id}:{self.number}"


class IdentityJobStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    COMPLETED = "COMPLETED", _("Completed")
    DISCARDED_STALE = "DISCARDED_STALE", _("Discarded (stale)")
    EXHAUSTED = "EXHAUSTED", _("Retries exhausted")


class IdentityVerificationJob(UUIDPrimaryKeyModel):
    """The durable work item of one provider check (outbox pattern, A13-14).

    Inserted in the same transaction as the submission or correction that
    needs it, so a broker outage never loses the work: a sweeper recovers any
    PENDING job whose time has come, and any IN_PROGRESS job whose lease has
    expired. One job per revision makes enqueueing idempotent; the lease token
    makes duplicate deliveries harmless.
    """

    verification = models.ForeignKey(
        IdentityVerification, on_delete=models.PROTECT, related_name="jobs"
    )
    revision = models.OneToOneField(IdentityRevision, on_delete=models.PROTECT, related_name="job")
    status = models.CharField(
        max_length=16, choices=IdentityJobStatus.choices, default=IdentityJobStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_outcome = models.CharField(max_length=24, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "people_identity_verification_job"
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="ppl_idjob_due_idx"),
            models.Index(fields=["status", "lease_expires_at"], name="ppl_idjob_lease_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"identity-job:{self.pk}:{self.status}"


class IdentityDecisionAction(models.TextChoices):
    MANUAL_VERIFY = "MANUAL_VERIFY", _("Verified manually")
    EXCEPTION_VERIFY = "EXCEPTION_VERIFY", _("Verified by staff-assisted exception")
    RETURN_FOR_CORRECTION = "RETURN_FOR_CORRECTION", _("Returned for correction")
    NIN_CORRECTION = "NIN_CORRECTION", _("NIN corrected and rechecked")
    RECHECK = "RECHECK", _("Rechecked with the ministry service")
    REJECT = "REJECT", _("Rejected")
    PARTICIPANT_RESUBMISSION = "PARTICIPANT_RESUBMISSION", _("Participant resubmitted")
    # IDV-C1: consequences recorded by the system (no actor, or the actor of
    # the originating command).
    LINKED_IDENTITY_CHANGE = (
        "LINKED_IDENTITY_CHANGE",
        _("Identifier changed in another registration of the same person"),
    )
    OFFICIAL_NAME_APPLIED = "OFFICIAL_NAME_APPLIED", _("Official name form applied")
    REGISTRATION_CLOSED = "REGISTRATION_CLOSED", _("Registration closed")


class IdentityDecision(UUIDPrimaryKeyModel):
    """Append-only history of identity actions by staff and the participant,
    and of the consequences the system records for them (IDV-C1).

    `reason_code` is a preset code; `note_encrypted` is an optional internal
    note, never shown to the participant. `requested_items` lists the preset
    correction items of a return. `evidence_document` is the reviewed document
    for a manual verification or a NIN correction.
    """

    verification = models.ForeignKey(
        IdentityVerification, on_delete=models.PROTECT, related_name="decisions"
    )
    revision = models.ForeignKey(
        IdentityRevision, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=32, choices=IdentityDecisionAction.choices)
    from_status = models.CharField(max_length=32, choices=IdentityStatus.choices)
    to_status = models.CharField(max_length=32, choices=IdentityStatus.choices)
    reason_code = models.CharField(max_length=64, blank=True, default="")
    note_encrypted = EncryptedTextField(blank=True, default="")
    requested_items = models.JSONField(default=list, blank=True)
    evidence_document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "people_identity_decision"
        indexes = [
            models.Index(fields=["verification", "created_at"], name="ppl_iddecision_ver_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"identity-decision:{self.verification_id}:{self.action}"


# ---------------------------------------------------------------------------
# Owner decision IDV-Q3 (2026-10-02): the NIN exemption route.
# ---------------------------------------------------------------------------


class NinExemptionStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active (draft may use the documentary route)")
    USED = "USED", _("Used by the final submission")
    REVOKED = "REVOKED", _("Revoked")


class NinExemptionReason(models.TextChoices):
    """Preset grant reasons. Internal; never shown to the participant."""

    NIN_NOT_ON_DOCUMENT = (
        "NIN_NOT_ON_DOCUMENT",
        _("The participant's official Algerian document shows no NIN"),
    )
    NIN_NOT_AVAILABLE = (
        "NIN_NOT_AVAILABLE",
        _("The participant has no NIN or cannot obtain it in time"),
    )
    OTHER_DOCUMENTED = "OTHER_DOCUMENTED", _("Other documented reason (explain below)")


class NinExemption(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """A staff grant that lets ONE Algerian draft registration complete without
    a NIN, on the documentary route (IDV-Q3, ADR-0026 decision 21).

    It is never a NIN and never a passport bypass for anyone else: it binds one
    Registration, needs `people.grant_nin_exemption` in that registration's
    exact scope, a preset reason and a written explanation, and is audited.
    The participant then declares an Algerian national identity card or an
    Algerian passport (`document_identifier`) with a reviewable image; the
    submission routes the identity to manual documentary review
    (`IdentityRoute.NIN_EXEMPTION`) and never schedules a ministry check.
    ACTIVE until the final submission makes it USED; staff may revoke it only
    while ACTIVE. `event_edition` and `organization` are copied from the
    Registration for scope filtering.
    """

    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="nin_exemptions"
    )
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="nin_exemptions")
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    status = models.CharField(
        max_length=16, choices=NinExemptionStatus.choices, default=NinExemptionStatus.ACTIVE
    )
    reason_code = models.CharField(max_length=40, choices=NinExemptionReason.choices)
    explanation_encrypted = EncryptedTextField()
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    granted_at = models.DateTimeField()
    document_identifier = models.ForeignKey(
        IdentityIdentifier,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_note_encrypted = EncryptedTextField(blank=True, default="")

    class Meta:
        db_table = "people_nin_exemption"
        permissions = [
            ("grant_nin_exemption", "Can grant or revoke a NIN exemption for one draft"),
        ]
        constraints = [
            # At most one live (active or used) exemption per registration.
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status__in=["ACTIVE", "USED"]),
                name="ppl_ninexempt_one_live_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["event_edition", "status"], name="ppl_ninexempt_event_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"nin-exemption:{self.registration_id}:{self.status}"
