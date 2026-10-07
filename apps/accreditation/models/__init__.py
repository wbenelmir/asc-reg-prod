"""Accreditation assignment domain (Phase 2 Prompt 4).

New app (TRD §13.1 later-phase application; ADR-0001). Role, badge, and
access assignments are kept as DISTINCT concepts -- never merged into one
ambiguous field on `Registration` -- each with its own reference-data
catalogue, its own assignment history table, and its own exclusivity rule:

* `BadgeTypeAssignment` is exclusive PER REGISTRATION (one current badge at
  a time -- a participant holds one physical badge per context).
* `ParticipantRoleAssignment`/`AccessProfileAssignment`/
  `AccessRuleAssignment` are exclusive PER (registration, reference object)
  -- a registration may hold several different current roles/profiles/
  rules simultaneously, but never two CURRENT assignments of the SAME one.

No Digital Entry Pass, QR payload, physical badge stock, printing,
issuance, device enrollment, entry scanning, or Event Edge model exists
here. Phase 3 Prompt 4 adds only the CONFIGURATION an entry decision reads
-- the Access Profile validity window and re-entry policy, and the Access
Rule zone/gate/effect dimension (Schema §9.1). The decision itself lives in
`apps.entry`.
"""

from __future__ import annotations

import base64
import secrets

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel


def generate_assignment_public_reference() -> str:
    """A random 128-bit base64url reference for external exposure.

    Module-level (not a lambda) so Django migrations can serialize it as a
    field default, and so every newly created assignment receives its own
    distinct value without the service layer having to remember.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")[:22]


# ---------------------------------------------------------------------------
# Configurable reference data (event-scoped, controlled codes)
# ---------------------------------------------------------------------------


class ParticipantRole(UUIDPrimaryKeyModel, TimestampedModel):
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="participant_roles"
    )
    code = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "accreditation_participant_role"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="acc_role_event_code_uq"
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


class BadgeType(UUIDPrimaryKeyModel, TimestampedModel):
    """`participant_visible` is the CONSERVATIVE visibility switch (Phase 2
    Prompt 4 §6): defaults to `False` (hidden) -- a Registration's assigned
    badge type is never shown to the participant unless an administrator
    explicitly flips this to `True` for this exact badge type."""

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="badge_types"
    )
    code = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    participant_visible = models.BooleanField(default=False)

    class Meta:
        db_table = "accreditation_badge_type"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="acc_badgetype_event_code_uq"
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


class ReentryPolicy(models.TextChoices):
    """Re-entry behaviour of an Access Profile (Schema §9.1, Flow §10.8).

    Re-entry is ALLOWED by default (OD-AF-12): repeated scanning is not
    fraud, and the operator only sees an advisory with the previous entry.
    `SINGLE_ENTRY` is the explicit strict rule; it must be configured
    deliberately and is never inferred.
    """

    REENTRY_ALLOWED = "REENTRY_ALLOWED", _("Re-entry allowed")
    SINGLE_ENTRY = "SINGLE_ENTRY", _("Single entry only")


class AccessProfile(UUIDPrimaryKeyModel, TimestampedModel):
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="access_profiles"
    )
    code = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    # Phase 3 Prompt 4 (Schema §9.1 "validity window, re-entry policy").
    # A NULL bound means "no bound on that side".
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    reentry_policy = models.CharField(
        max_length=16, choices=ReentryPolicy.choices, default=ReentryPolicy.REENTRY_ALLOWED
    )

    class Meta:
        db_table = "accreditation_access_profile"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="acc_accessprofile_event_code_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True)
                | models.Q(valid_from__isnull=True)
                | models.Q(valid_until__gt=models.F("valid_from")),
                name="acc_accessprofile_valid_range",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code

    @property
    def localized_name(self) -> str:
        """Display name in the active language (UI/UX Completion Gate: the
        stored code is never the only label shown)."""
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


class AccessRuleEffect(models.TextChoices):
    """V1 uses allow rules plus explicit restrictions (Schema §9.1), not a
    policy language. `DENY` exists so a specific Gate inside an otherwise
    allowed Zone can be closed to a profile without restructuring zones."""

    ALLOW = "ALLOW", _("Allow")
    DENY = "DENY", _("Deny")


class AccessRuleEventType(models.TextChoices):
    ENTRY = "ENTRY", _("Entry")
    EXIT = "EXIT", _("Exit")
    ANY = "ANY", _("Entry or exit")


class AccessRule(UUIDPrimaryKeyModel, TimestampedModel):
    """`access_profile` is an OPTIONAL categorization -- a rule may be
    assigned to a registration directly, independent of any profile
    (Phase 2 Prompt 4 "keep... as distinct domain concepts").

    Phase 3 Prompt 4 adds the checkpoint dimension Schema §9.1 describes:
    a rule joins a profile (or a direct assignment) to a `Zone` and an
    optional `Gate`, with an event type, a validity window, and an effect.
    A rule with NO zone is a Phase 2 categorization rule and never
    authorizes entry anywhere -- the entry evaluator fails closed on it.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="access_rules"
    )
    access_profile = models.ForeignKey(
        AccessProfile, null=True, blank=True, on_delete=models.PROTECT, related_name="rules"
    )
    code = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    zone = models.ForeignKey(
        "events.Zone", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    gate = models.ForeignKey(
        "events.Gate", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    event_type = models.CharField(
        max_length=8, choices=AccessRuleEventType.choices, default=AccessRuleEventType.ENTRY
    )
    effect = models.CharField(
        max_length=8, choices=AccessRuleEffect.choices, default=AccessRuleEffect.ALLOW
    )
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accreditation_access_rule"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="acc_accessrule_event_code_uq"
            ),
            # A gate-specific rule must still say which zone it concerns.
            models.CheckConstraint(
                condition=models.Q(gate__isnull=True) | models.Q(zone__isnull=False),
                name="acc_accessrule_gate_requires_zone",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True)
                | models.Q(valid_from__isnull=True)
                | models.Q(valid_until__gt=models.F("valid_from")),
                name="acc_accessrule_valid_range",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code

    @property
    def localized_name(self) -> str:
        """Display name in the active language (UI/UX Completion Gate: the
        stored code is never the only label shown)."""
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


# ---------------------------------------------------------------------------
# Assignment history (distinct table per concept)
# ---------------------------------------------------------------------------


class AssignmentStatus(models.TextChoices):
    CURRENT = "CURRENT", _("Current")
    SUPERSEDED = "SUPERSEDED", _("Superseded")
    REVOKED = "REVOKED", _("Revoked")
    EXPIRED = "EXPIRED", _("Expired")


class BaseAssignment(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """Shared shape for every accreditation assignment table (Phase 2
    Prompt 4 §4). Never mutated destructively: `change`/`revoke` supersede
    the current row (`status`, `effective_until`) and -- for a change --
    create a NEW current row referencing it via `supersedes`; the prior
    row is never overwritten or deleted."""

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
        max_length=16, choices=AssignmentStatus.choices, default=AssignmentStatus.CURRENT
    )
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        abstract = True


class ParticipantRoleAssignment(BaseAssignment):
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="role_assignments"
    )
    role = models.ForeignKey(ParticipantRole, on_delete=models.PROTECT, related_name="+")
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )

    class Meta:
        db_table = "accreditation_participant_role_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status=AssignmentStatus.CURRENT),
                name="acc_role_assignment_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gt=models.F("effective_from")),
                name="acc_role_assignment_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "status"], name="acc_role_assign_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"role-assignment:{self.registration_id}:{self.role_id}:{self.status}"


class BadgeTypeAssignment(BaseAssignment):
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="badge_assignments"
    )
    badge_type = models.ForeignKey(BadgeType, on_delete=models.PROTECT, related_name="+")
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    # Phase 3 Prompt 2: the ONLY externally exposed reference for this
    # assignment, carried as the `bai` claim in a signed Digital Entry Pass
    # QR. Deliberately a separate random value rather than the UUIDv7
    # primary key: `FR-BDG-005` forbids a direct internal record identifier
    # in the QR, and a UUIDv7 is time-ordered, so publishing it would leak
    # assignment creation ordering. Populated for every row by the
    # accompanying backfill migration. It is NOT nullable and NOT blankable:
    # a blank reference would produce an unusable `bai` claim in a signed
    # credential, so the database refuses one outright.
    public_reference = models.CharField(max_length=22, default=generate_assignment_public_reference)

    class Meta:
        db_table = "accreditation_badge_type_assignment"
        constraints = [
            # Unconditional uniqueness. The earlier partial form existed only
            # to survive the instant between adding the column and running the
            # backfill; now that every row carries a value, a partial index
            # would leave "several rows share the empty string" representable.
            models.UniqueConstraint(
                fields=["public_reference"],
                name="acc_badge_assignment_public_ref_uq",
            ),
            models.CheckConstraint(
                condition=~models.Q(public_reference=""),
                name="acc_badge_assignment_public_ref_not_blank",
            ),
            # Exclusive PER REGISTRATION (not per badge type): one current
            # physical badge slot at a time.
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status=AssignmentStatus.CURRENT),
                name="acc_badge_assignment_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gt=models.F("effective_from")),
                name="acc_badge_assignment_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "status"], name="acc_badge_assign_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"badge-assignment:{self.registration_id}:{self.status}"


class AccessProfileAssignment(BaseAssignment):
    registration = models.ForeignKey(
        "registrations.Registration",
        on_delete=models.PROTECT,
        related_name="access_profile_assignments",
    )
    access_profile = models.ForeignKey(AccessProfile, on_delete=models.PROTECT, related_name="+")
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )

    class Meta:
        db_table = "accreditation_access_profile_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status=AssignmentStatus.CURRENT),
                name="acc_profile_assignment_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gt=models.F("effective_from")),
                name="acc_profile_assignment_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "status"], name="acc_profile_assign_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"access-profile-assignment:{self.registration_id}:"
            f"{self.access_profile_id}:{self.status}"
        )


class AccessRuleAssignment(BaseAssignment):
    registration = models.ForeignKey(
        "registrations.Registration",
        on_delete=models.PROTECT,
        related_name="access_rule_assignments",
    )
    access_rule = models.ForeignKey(AccessRule, on_delete=models.PROTECT, related_name="+")
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )

    class Meta:
        db_table = "accreditation_access_rule_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "access_rule"],
                condition=models.Q(status=AssignmentStatus.CURRENT),
                name="acc_rule_assignment_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gt=models.F("effective_from")),
                name="acc_rule_assignment_valid_range",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "status"], name="acc_rule_assign_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"access-rule-assignment:{self.registration_id}:{self.access_rule_id}:{self.status}"


# ---------------------------------------------------------------------------
# Attendance entitlement (which conference days an approval covers)
# ---------------------------------------------------------------------------


class AttendanceCategory(models.TextChoices):
    """The two attendance scopes an approval may grant.

    There is deliberately no third member: an approved registration WITHOUT a
    current `AttendanceEntitlement` is "not yet classified", a state that
    exists only for registrations approved before entitlements existed and
    is resolved by an authorized operator. It is never offered as a choice.
    """

    ALL_CONFERENCE_DAYS = "ALL_CONFERENCE_DAYS", _("All three conference days")
    FOLLOWING_TWO_DAYS = "FOLLOWING_TWO_DAYS", _("The two days after the opening day")


class AttendancePolicy(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """The attendance configuration of one Event Edition.

    The three conference days are local calendar dates in the edition's own
    timezone (`EventEdition.timezone`). The day boundary is the local
    midnight, the same boundary the offline packages use for their hard
    expiry; the hours inside a day stay governed by the existing Access
    Profile and Access Rule windows. Nothing here is guessed: every value is
    entered by an authorized operator (`manage_attendancepolicy`).

    The opening-day capacity counts approved registrations that hold an
    `ALL_CONFERENCE_DAYS` entitlement; the count is derived from the
    entitlement rows, never kept in a counter, so a withdrawal or a change
    can never release the same place twice. Every command that could add an
    opening-day place locks this row first (`SELECT ... FOR UPDATE`).

    `enforcement_active` is the controlled switch that makes admission
    enforce the attendance days. It stays off after deployment and is
    switched on only through `apps.accreditation.attendance.activate_enforcement`
    once its prerequisites hold.
    """

    event_edition = models.OneToOneField(
        "events.EventEdition", on_delete=models.PROTECT, related_name="attendance_policy"
    )
    opening_date = models.DateField(null=True, blank=True)
    second_date = models.DateField(null=True, blank=True)
    third_date = models.DateField(null=True, blank=True)
    opening_day_capacity = models.PositiveIntegerField(null=True, blank=True)
    enforcement_active = models.BooleanField(default=False)
    enforcement_activated_at = models.DateTimeField(null=True, blank=True)
    enforcement_activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    enforcement_deactivated_at = models.DateTimeField(null=True, blank=True)
    enforcement_deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "accreditation_attendance_policy"
        permissions = [
            (
                "manage_attendancepolicy",
                "Can configure attendance days, opening-day capacity and enforcement",
            ),
        ]
        constraints = [
            # All three dates or none, strictly increasing.
            models.CheckConstraint(
                condition=models.Q(
                    opening_date__isnull=True, second_date__isnull=True, third_date__isnull=True
                )
                | models.Q(
                    opening_date__isnull=False,
                    second_date__isnull=False,
                    third_date__isnull=False,
                    second_date__gt=models.F("opening_date"),
                    third_date__gt=models.F("second_date"),
                ),
                name="acc_attpolicy_dates_ordered",
            ),
            # Enforcement needs the days and the capacity.
            models.CheckConstraint(
                condition=models.Q(enforcement_active=False)
                | models.Q(
                    opening_date__isnull=False,
                    opening_day_capacity__isnull=False,
                    enforcement_activated_at__isnull=False,
                ),
                name="acc_attpolicy_active_requires_configuration",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"attendance-policy:{self.event_edition_id}"


class AttendanceEnforcementIntervalSource(models.TextChoices):
    RECORDED = "RECORDED", _("Recorded when enforcement was switched on")
    RECONSTRUCTED = "RECONSTRUCTED", _("Rebuilt from the last recorded switch")
    UNCERTAIN = "UNCERTAIN", _("Earlier history not recorded (treated as enforced)")


class AttendanceEnforcementInterval(UUIDPrimaryKeyModel, TimestampedModel):
    """One period during which admission enforced the attendance days.

    `started_at` is included, `ended_at` excluded; an open interval has no
    end. Activation opens one and deactivation closes it, both under the
    policy row lock, so at most one interval per policy is open. The
    synchronization service judges an offline operation by the interval that
    covered its occurrence time, so a later deactivation or reactivation
    never changes how an earlier operation is judged.

    `UNCERTAIN` rows come only from the migration that introduced this
    table: when the earlier cycles of a policy had already been overwritten,
    the unknown period counts as enforced (an operation from that period is
    judged by the stricter rule, never silently accepted)."""

    policy = models.ForeignKey(
        AttendancePolicy, on_delete=models.PROTECT, related_name="enforcement_intervals"
    )
    started_at = models.DateTimeField()
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source = models.CharField(
        max_length=16,
        choices=AttendanceEnforcementIntervalSource.choices,
        default=AttendanceEnforcementIntervalSource.RECORDED,
    )

    class Meta:
        db_table = "accreditation_attendance_enforcement_interval"
        ordering = ["started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy"],
                condition=models.Q(ended_at__isnull=True),
                name="acc_attenf_one_open_per_policy",
            ),
            models.CheckConstraint(
                condition=models.Q(ended_at__isnull=True)
                | models.Q(ended_at__gt=models.F("started_at")),
                name="acc_attenf_end_after_start",
            ),
        ]
        indexes = [
            models.Index(fields=["policy", "started_at"], name="acc_attenf_policy_start_idx")
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"attendance-enforcement:{self.policy_id}:{self.started_at:%Y-%m-%dT%H:%M}"


class AttendanceEntitlementStatus(models.TextChoices):
    CURRENT = "CURRENT", _("Current")
    SUPERSEDED = "SUPERSEDED", _("Superseded")


class AttendanceEntitlementOrigin(models.TextChoices):
    APPROVAL = "APPROVAL", _("Chosen at approval")
    LEGACY_CLASSIFICATION = "LEGACY_CLASSIFICATION", _("Classification of an earlier approval")
    CHANGE = "CHANGE", _("Change after approval")


class AttendanceEntitlement(UUIDPrimaryKeyModel, TimestampedModel):
    """The attendance days one Registration Context is authorized for.

    Kept distinct from the participant role, the badge type, the access
    profile and staff permissions: it narrows WHICH DAYS the existing
    accreditation may be used, and never widens it. Append-only history: a
    change supersedes the current row (`status`, `effective_until`) and
    creates a new one pointing back to it; nothing is overwritten or deleted.
    One CURRENT row per registration at most (partial unique constraint).
    """

    registration = models.ForeignKey(
        "registrations.Registration",
        on_delete=models.PROTECT,
        related_name="attendance_entitlements",
    )
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    category = models.CharField(max_length=24, choices=AttendanceCategory.choices)
    status = models.CharField(
        max_length=16,
        choices=AttendanceEntitlementStatus.choices,
        default=AttendanceEntitlementStatus.CURRENT,
    )
    origin = models.CharField(max_length=24, choices=AttendanceEntitlementOrigin.choices)
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The `reviews.RegistrationDecision` an approval-time entitlement was
    #: recorded with (an id, not a foreign key: `reviews` already depends on
    #: this app, and the decision history is never deleted).
    decision_id = models.UUIDField(null=True, blank=True)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )

    class Meta:
        db_table = "accreditation_attendance_entitlement"
        constraints = [
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status=AttendanceEntitlementStatus.CURRENT),
                name="acc_attendance_one_current_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gte=models.F("effective_from")),
                name="acc_attendance_valid_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "status", "category"], name="acc_attendance_event_idx"
            ),
            models.Index(fields=["registration", "status"], name="acc_attendance_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"attendance:{self.registration_id}:{self.category}:{self.status}"


# ---------------------------------------------------------------------------
# Bulk operations (preview -> execute, idempotent, fully audited)
# ---------------------------------------------------------------------------


class BulkAssignmentKind(models.TextChoices):
    PARTICIPANT_ROLE = "PARTICIPANT_ROLE", _("Participant role")
    BADGE_TYPE = "BADGE_TYPE", _("Badge type")
    ACCESS_PROFILE = "ACCESS_PROFILE", _("Access profile")
    ACCESS_RULE = "ACCESS_RULE", _("Access rule")


class BulkOperationStatus(models.TextChoices):
    PREVIEWED = "PREVIEWED", _("Previewed")
    EXECUTED = "EXECUTED", _("Executed")
    CANCELLED = "CANCELLED", _("Cancelled")


class BulkAssignmentOperation(UUIDPrimaryKeyModel, TimestampedModel):
    """Durable evidence of one bulk-assignment workflow (Phase 2 Prompt 4
    §5). `target_registration_ids` is the EXACT scope computed at preview
    time -- execution reads THIS list, never a freshly re-run filter, so
    execution can never silently expand beyond what was previewed."""

    kind = models.CharField(max_length=24, choices=BulkAssignmentKind.choices)
    reference_object_id = models.UUIDField()
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
    idempotency_key = models.CharField(max_length=100, unique=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=300, blank=True, default="")
    target_registration_ids = models.JSONField(default=list)
    preview_results = models.JSONField(default=list, blank=True)
    execution_results = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=BulkOperationStatus.choices, default=BulkOperationStatus.PREVIEWED
    )
    previewed_at = models.DateTimeField()
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accreditation_bulk_assignment_operation"
        indexes = [
            models.Index(fields=["event_edition", "status"], name="acc_bulkop_event_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"bulk-op:{self.kind}:{self.status}"
