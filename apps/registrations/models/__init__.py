"""Registration aggregate, profile, interests and immutable submission (Schema §5.3-§5.8).

`invitation_campaign`, `created_on_behalf_by`, and `claimed_at` were added by
`registrations.0005` (Phase 2 Prompt 2), once the `invitations` app exists.
Every Phase 1 registration remained `OPEN` source kind; the full source-kind
enum was already declared in Phase 1 so this did not require a destructive
column-value migration.

`RegistrationProfile`'s "professional profile" field group (Schema §5.5) is
deliberately NOT duplicated here -- it is owned entirely by
`organizations.ProfessionalAffiliation` (Schema §5.6), added in
`organizations.0002` once this migration's `Registration` exists. Selectors
join the two rather than storing the same submitted organization/job-title
data in two tables.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models.functions import Upper
from django.utils.translation import gettext_lazy as _

from apps.core.fields import EncryptedTextField
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel


class RegistrationSourceKind(models.TextChoices):
    """Labels are translated (Prompt 5 correction pass §5) since
    `get_source_kind_display()` renders one in the operations UI; the
    stored enum VALUE is never translated."""

    OPEN = "OPEN", _("Open")
    INVITATION = "INVITATION", _("Invitation")
    DELEGATION = "DELEGATION", _("Delegation")
    ON_BEHALF = "ON_BEHALF", _("On behalf")
    ON_SITE = "ON_SITE", _("On site")


class RegistrationPublicStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    SUBMITTED = "SUBMITTED", _("Submitted")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    ADDITIONAL_INFORMATION_REQUIRED = (
        "ADDITIONAL_INFORMATION_REQUIRED",
        _("Additional information required"),
    )
    APPROVED = "APPROVED", _("Approved")
    NOT_APPROVED = "NOT_APPROVED", _("Not approved")
    WITHDRAWN = "WITHDRAWN", _("Withdrawn")


class RegistrationInternalStatus(models.TextChoices):
    """Operational/internal status labels are translated (Prompt 5 correction
    pass §5) since `get_internal_status_display()` renders one in the
    operations UI; the stored enum VALUE is never translated."""

    PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT", _("Pending assignment")
    ASSIGNED = "ASSIGNED", _("Assigned")
    VERIFICATION_PENDING = "VERIFICATION_PENDING", _("Verification pending")
    DUPLICATE_REVIEW = "DUPLICATE_REVIEW", _("Duplicate review")
    REVIEW_IN_PROGRESS = "REVIEW_IN_PROGRESS", _("Review in progress")
    AWAITING_APPLICANT = "AWAITING_APPLICANT", _("Awaiting applicant")
    QUALIFICATION_COMPLETE = "QUALIFICATION_COMPLETE", _("Qualification complete")
    CLOSED = "CLOSED", _("Closed")


class Registration(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """The aggregate root for one participation context (Schema §5.3).

    Multiple rows may exist for the same `(person, event_edition)` pair by
    design (different source contexts) -- there is deliberately no
    uniqueness constraint on that pair. `deduplication_key` plus
    `is_current_context` is the accidental-duplicate control instead
    (Schema §5.4); an operator may mark one row non-current without
    deleting it.
    """

    public_reference = models.CharField(max_length=32, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="registrations"
    )
    person = models.ForeignKey(
        "people.Person",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="registrations",
    )
    source_kind = models.CharField(max_length=16, choices=RegistrationSourceKind.choices)
    source_context_key = models.CharField(max_length=200)
    source_organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    # null=True is deliberate here (not the usual "use ''" CharField
    # convention): the partial UniqueConstraint below relies on SQL's
    # multi-NULL-does-not-conflict semantics for an unclaimed Draft that
    # has no dedup key yet. Multiple empty strings WOULD collide; multiple
    # NULLs never do.
    deduplication_key = models.CharField(max_length=300, null=True, blank=True)  # noqa: DJ001
    # Invitation provenance (Schema §6.3/§6.4, Phase 2 Prompt 2, INV-002):
    # PROTECT preserves this Registration's evidence even if a campaign were
    # ever deleted (campaigns are otherwise never hard-deleted -- only
    # closed/expired). Never inferred from "the currently active link" --
    # persisted once, on the exact Registration Context, at creation time.
    invitation_campaign = models.ForeignKey(
        "invitations.InvitationCampaign",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="registrations",
    )
    # On-behalf provenance (Schema UJ-10, Phase 2 Prompt 2). SET_NULL: the
    # authorized creator's own account lifecycle must never block deleting
    # or evidencing this Registration; the creation fact itself is preserved
    # by the OnBehalfClaim row and the REGISTRATION_ON_BEHALF_CREATED audit
    # event regardless of whether this FK still resolves.
    created_on_behalf_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    public_status = models.CharField(
        max_length=32,
        choices=RegistrationPublicStatus.choices,
        default=RegistrationPublicStatus.DRAFT,
    )
    internal_status = models.CharField(
        max_length=32,
        choices=RegistrationInternalStatus.choices,
        default=RegistrationInternalStatus.PENDING_ASSIGNMENT,
    )
    is_current_context = models.BooleanField(default=True)
    preferred_language = models.CharField(max_length=8, default="en")
    current_step = models.CharField(max_length=100, blank=True, default="")
    submitted_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "registrations_registration"
        # Phase 2 Prompt 2: a distinct, explicitly-named permission for
        # authorized on-behalf draft creation (AF-ORG-03) -- deny by
        # default, granted only through an explicit `ScopedGroupMembership`
        # scoped to the acting organization, never implied by
        # `add_registration` alone.
        permissions = [("register_on_behalf", "Can create an on-behalf draft registration")]
        constraints = [
            # Schema §5.4: a partial unique index over CURRENT contexts only
            # -- a row marked non-current (RegistrationDuplicateResolution,
            # Phase 2+) never blocks a fresh registration reusing the key.
            models.UniqueConstraint(
                fields=["deduplication_key"],
                condition=models.Q(is_current_context=True),
                name="reg_registration_current_dedup_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "public_status", "created_at"],
                name="reg_regn_pub_status_idx",
            ),
            models.Index(
                fields=["event_edition", "internal_status", "updated_at"],
                name="reg_regn_int_status_idx",
            ),
            models.Index(fields=["person", "event_edition"], name="reg_registration_person_idx"),
            models.Index(
                fields=["source_organization", "event_edition"], name="reg_registration_org_idx"
            ),
            models.Index(
                fields=["invitation_campaign", "submitted_at"], name="reg_registration_camp_idx"
            ),
            # Phase 3 Prompt 5 (ADR-0022): the checkpoint reference lookup is
            # case-insensitive (`public_reference__iexact`, i.e. UPPER(col) =
            # UPPER(value)), which the plain unique btree cannot serve. This
            # expression index keeps that lookup indexed.
            models.Index(Upper("public_reference"), name="reg_public_ref_upper_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.public_reference


class RegistrationProfile(UUIDPrimaryKeyModel, TimestampedModel):
    """Mutable, queryable current data for a Registration (Schema §5.5)."""

    registration = models.OneToOneField(
        Registration, on_delete=models.CASCADE, related_name="profile"
    )
    submitted_given_names = models.CharField(max_length=200, blank=True, default="")
    submitted_family_name = models.CharField(max_length=200, blank=True, default="")
    submitted_full_name = models.CharField(max_length=400, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    nationality_code = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    country_of_residence = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    verified_email_contact = models.ForeignKey(
        "people.ContactPoint", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # `verified_mobile_contact` is set ONLY when the underlying ContactPoint
    # is actually verified (Prompt 4 final closure pass §3) -- Phase 1 has
    # no mobile/SMS OTP flow, so it stays NULL for essentially every V1
    # registration. `declared_mobile_contact` holds the participant's
    # self-declared, not-yet-verified mobile so the wizard, snapshot and
    # completeness guard still have a place to read it from without
    # mislabeling it as verified.
    verified_mobile_contact = models.ForeignKey(
        "people.ContactPoint", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    declared_mobile_contact = models.ForeignKey(
        "people.ContactPoint",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    objectives_text = models.CharField(max_length=2000, blank=True, default="")
    accessibility_needs_text = models.CharField(max_length=2000, blank=True, default="")

    class Meta:
        db_table = "registrations_profile"


class InterestTopic(UUIDPrimaryKeyModel, TimestampedModel):
    """Event-configurable, translatable interest topic (Schema §5.7)."""

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.CASCADE, related_name="interest_topics"
    )
    code = models.CharField(max_length=64)
    label = models.CharField(max_length=200)
    label_fr = models.CharField(max_length=200, blank=True, default="")
    label_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    # UX-2 (M20, C-06): the display group of the topic; empty for topics that
    # predate the grouping (shown under "Other topics").
    group_code = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "registrations_interest_topic"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="reg_interesttopic_event_code_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code

    @property
    def localized_label(self) -> str:
        """The label in the current active language, falling back to English (Schema §3.4,
        Prompt 4 final closure pass §9: "interest/reference labels must use the active
        language with an explicit fallback")."""
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.label_fr:
            return self.label_fr
        if language == "ar" and self.label_ar:
            return self.label_ar
        return self.label


class RegistrationInterest(UUIDPrimaryKeyModel, TimestampedModel):
    """Join of Registration to InterestTopic (Schema §5.7).

    Never used to auto-assign a Badge Type (that assignment logic belongs
    to a later, out-of-Phase-1-scope accreditation phase).
    """

    registration = models.ForeignKey(
        Registration, on_delete=models.CASCADE, related_name="interests"
    )
    interest_topic = models.ForeignKey(InterestTopic, on_delete=models.PROTECT, related_name="+")
    priority = models.PositiveIntegerField(null=True, blank=True)
    notes = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "registrations_interest"
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "interest_topic"], name="reg_interest_reg_topic_uq"
            ),
        ]


class RegistrationSubmissionKind(models.TextChoices):
    INITIAL = "INITIAL", "Initial"
    ADDITIONAL_INFORMATION_RESPONSE = (
        "ADDITIONAL_INFORMATION_RESPONSE",
        "Additional information response",
    )
    AUTHORIZED_RESUBMISSION = "AUTHORIZED_RESUBMISSION", "Authorized resubmission"


class RegistrationSubmittedByType(models.TextChoices):
    PARTICIPANT = "PARTICIPANT", "Participant"
    OPERATIONAL_USER = "OPERATIONAL_USER", "Operational user"
    SYSTEM_MIGRATION = "SYSTEM_MIGRATION", "System migration"


class RegistrationSubmission(UUIDPrimaryKeyModel):
    """Immutable evidence of a formal submission or resubmission (Schema §5.8).

    No `updated_at`/`version` -- append-only evidence, never mutated after
    creation (enforced at the service layer: no update path is exposed).
    `registration` uses `on_delete=PROTECT` so this evidence can never be
    silently removed by deleting its parent Registration.
    """

    registration = models.ForeignKey(
        Registration, on_delete=models.PROTECT, related_name="submissions"
    )
    sequence = models.PositiveIntegerField()
    submission_kind = models.CharField(max_length=40, choices=RegistrationSubmissionKind.choices)
    snapshot_json = models.JSONField()
    snapshot_hash = models.CharField(max_length=64)
    submitted_by_type = models.CharField(max_length=20, choices=RegistrationSubmittedByType.choices)
    submitted_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    submitted_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = "registrations_submission"
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "sequence"], name="reg_submission_reg_sequence_uq"
            ),
            # At most one INITIAL submission per Registration (Prompt 6
            # P6-H-02 correction) -- a PostgreSQL partial unique index, the
            # database-level backstop behind the service-layer lock-and-
            # recheck dedup in `record_registration_submission`. Later
            # submission kinds (ADDITIONAL_INFORMATION_RESPONSE,
            # AUTHORIZED_RESUBMISSION) are unaffected and may repeat.
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(submission_kind=RegistrationSubmissionKind.INITIAL),
                name="reg_submission_one_initial_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "submitted_at"], name="reg_submission_reg_idx")
        ]


# ---------------------------------------------------------------------------
# UX-3 (M21, decisions D-10, C-08, S-15, S-18): accommodation request.
# ---------------------------------------------------------------------------


class AccommodationAnswer(models.TextChoices):
    """The voluntary question "Do you need accessibility support?"."""

    NO = "NO", _("No")
    YES = "YES", _("Yes")
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY", _("Prefer not to say")


class AccommodationCategory(models.TextChoices):
    """Practical support categories, informed by the UN CRPD social model and
    "reasonable accommodation" (D-10). They describe support to arrange, never
    a diagnosis, impairment type, percentage or certificate. The sign-language
    label stays neutral while its exact naming is open (C-08)."""

    STEP_FREE_ACCESS = "STEP_FREE_ACCESS", _("Wheelchair or step-free access")
    ACCESSIBLE_SEATING = "ACCESSIBLE_SEATING", _("Accessible seating")
    SIGN_LANGUAGE = "SIGN_LANGUAGE", _("Sign language interpretation")
    CAPTIONING = "CAPTIONING", _("Live captioning")
    ACCESSIBLE_DOCUMENTS = (
        "ACCESSIBLE_DOCUMENTS",
        _("Accessible documents (large print or screen-reader friendly)"),
    )
    ASSISTANCE_ON_SITE = "ASSISTANCE_ON_SITE", _("Assistance on site")
    COMPANION_ACCESS = "COMPANION_ACCESS", _("A personal assistant accompanies me")
    SERVICE_ANIMAL = "SERVICE_ANIMAL", _("Service animal")
    QUIET_SPACE = "QUIET_SPACE", _("Access to a quiet space")
    OTHER = "OTHER", _("Other support")


class AccommodationRequest(UUIDPrimaryKeyModel, TimestampedModel):
    """One registration's voluntary accommodation request (UX-3).

    Health-adjacent sensitive data, kept apart from the registration profile:
    it never feeds admission, review, accreditation or badge decisions
    (S-15), and it is read only through
    `apps.registrations.selectors.accommodation_requests_visible_to`, which
    requires `coordinate_accommodation_support`. The optional note is
    encrypted at rest. The row is never copied into the submission snapshot,
    QR payloads, offline packages, exports, e-mails, logs or the
    language-switch `sessionStorage` (S-18); the snapshot records only the
    answer. `withdrawn_at` is set when the participant withdraws the
    sensitive-data consent: the categories and note are then cleared, unless a
    legal hold requires keeping them (they stay hidden).
    """

    registration = models.OneToOneField(
        Registration, on_delete=models.PROTECT, related_name="accommodation_request"
    )
    answer = models.CharField(max_length=24, choices=AccommodationAnswer.choices)
    categories = models.JSONField(default=list, blank=True)
    note_encrypted = EncryptedTextField(blank=True, default="")
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "registrations_accommodation_request"
        permissions = [
            (
                "coordinate_accommodation_support",
                "Can view and arrange accommodation support requests",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"accommodation:{self.registration_id}"

    @property
    def has_sensitive_data(self) -> bool:
        """True when the row holds support categories or a note."""
        return self.withdrawn_at is None and bool(self.categories or self.note_encrypted)
