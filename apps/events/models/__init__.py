"""`EventEdition` (Schema §5.1) and the venue structure (Schema §5.2).

`Venue`, `Zone`, and `Gate` were deferred by ADR-0010 until entry work
began; Phase 3 Prompt 4 (online entry) is that point, and ADR-0021 records
the additive introduction.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel

SUPPORTED_LANGUAGE_CODES: tuple[str, ...] = ("en", "fr", "ar")


class EventEditionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    REGISTRATION_OPEN = "REGISTRATION_OPEN", "Registration open"
    REGISTRATION_CLOSED = "REGISTRATION_CLOSED", "Registration closed"
    EVENT_OPERATIONS = "EVENT_OPERATIONS", "Event operations"
    COMPLETED = "COMPLETED", "Completed"
    ARCHIVED = "ARCHIVED", "Archived"


class PublicRegistrationMode(models.TextChoices):
    """Per-event public registration channel (UX-4, decision D-12).

    OPEN: public sign-up is allowed inside the registration window (when set).
    INVITATION_ONLY: public sign-up is closed; valid invitation, delegation and
    on-behalf paths continue under their own rules.
    CLOSED: no participant-initiated draft creation, claim or submission on
    any channel. Sign-in, passes, the workspace and authorized operations
    continue in every mode.
    """

    OPEN = "OPEN", "Open"
    INVITATION_ONLY = "INVITATION_ONLY", "Invitation only"
    CLOSED = "CLOSED", "Closed"


class EventEdition(UUIDPrimaryKeyModel, TimestampedModel):
    """One edition of the event (Schema §5.1). `version` supports optimistic
    concurrency on operational configuration changes (Schema §16.3)."""

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, help_text="IANA timezone name.")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    registration_opens_at = models.DateTimeField(null=True, blank=True)
    registration_closes_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=32, choices=EventEditionStatus.choices, default=EventEditionStatus.DRAFT
    )
    default_language = models.CharField(max_length=8, default="en")
    supported_languages = models.JSONField(
        default=list,
        help_text=f"BCP-47 codes; app-validated against {SUPPORTED_LANGUAGE_CODES!r}.",
    )
    settings_version = models.PositiveIntegerField(default=1)

    # Prompt 5 correction pass §2 (REG-03 "passport path with policy-driven
    # identity-page upload"): fail-closed, disabled by default. When False,
    # the passport identity-page upload field is completely absent from the
    # identity-step form and its rendered HTML, in every language -- never
    # merely hidden or disabled client-side.
    passport_identity_page_upload_enabled = models.BooleanField(default=False)

    # Concurrency-safe event-scoped human reference generation (Schema
    # §3.1/§20 item "Registration human references are event-scoped").
    # Allocation locks this row with SELECT ... FOR UPDATE inside the same
    # transaction as Registration creation
    # (`apps.registrations.services.allocate_registration_reference`).
    next_registration_sequence = models.PositiveIntegerField(default=1)

    # UX-4 (M24, D-12): the manual channel mode. The default equals the
    # behaviour before UX-4. The effective state also depends on
    # `registration_opens_at` / `registration_closes_at` and is computed by
    # `apps.events.policies.registration_channels`; the most restrictive wins.
    # Changes go through `apps.events.services.change_registration_channel`,
    # which requires a scoped permission and a reason, and is audited.
    public_registration_mode = models.CharField(
        max_length=16,
        choices=PublicRegistrationMode.choices,
        default=PublicRegistrationMode.OPEN,
    )

    # UX-2 (M12, D-09): participants must be at least this old on the event's
    # first local day (`starts_at` in `timezone`). One threshold for every
    # channel; not a claim about any legal age of majority.
    minimum_participant_age = models.PositiveSmallIntegerField(default=18)

    # UX-2 (M04 follow-up, delegated decision): the event name shown to a
    # French or Arabic reader, for example in e-mails. Empty means the English
    # `name` is used.
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        db_table = "events_event_edition"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gte=models.F("starts_at")),
                name="events_edition_ends_at_after_starts_at",
            ),
            models.CheckConstraint(
                condition=models.Q(registration_opens_at__isnull=True)
                | models.Q(registration_closes_at__isnull=True)
                | models.Q(registration_closes_at__gt=models.F("registration_opens_at")),
                name="events_edition_registration_window_order",
            ),
        ]
        permissions = [
            (
                "manage_registration_channels",
                "Can open, restrict or close registration channels",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code

    def display_name(self, language: str | None = None) -> str:
        """The event name for `language` ("en", "fr" or "ar"), falling back to
        the English `name`."""
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


# ---------------------------------------------------------------------------
# Venue structure (Schema §5.2, ADR-0021)
# ---------------------------------------------------------------------------


class _LocalizedNameMixin(models.Model):
    """English name plus optional French/Arabic names with an explicit
    fallback, the same shape `accreditation` reference data already uses."""

    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        abstract = True

    @property
    def localized_name(self) -> str:
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


class Venue(UUIDPrimaryKeyModel, TimestampedModel, _LocalizedNameMixin):
    """A physical site of one Event Edition (Schema §5.2)."""

    event_edition = models.ForeignKey(EventEdition, on_delete=models.PROTECT, related_name="venues")
    code = models.CharField(max_length=32)
    address_summary = models.CharField(max_length=300, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "events_venue"
        constraints = [
            models.UniqueConstraint(fields=["event_edition", "code"], name="events_venue_code_uq"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


class ZoneSensitivity(models.TextChoices):
    STANDARD = "STANDARD", "Standard"
    RESTRICTED = "RESTRICTED", "Restricted"
    HIGH_SECURITY = "HIGH_SECURITY", "High security"


class Zone(UUIDPrimaryKeyModel, TimestampedModel, _LocalizedNameMixin):
    """An access area inside a Venue (Schema §5.2).

    `parent` is optional for hierarchical zones; an access rule granted on
    a parent zone covers its descendants (`apps.entry.services.access`).
    Parent and child must belong to the same Venue -- enforced by the
    configuration service, since a cross-row check is not expressible as a
    CHECK constraint.
    """

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="zones")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    code = models.CharField(max_length=32)
    sensitivity = models.CharField(
        max_length=16, choices=ZoneSensitivity.choices, default=ZoneSensitivity.STANDARD
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "events_zone"
        constraints = [
            models.UniqueConstraint(fields=["venue", "code"], name="events_zone_code_uq"),
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")), name="events_zone_no_self_parent"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


class GateOfflinePolicy(models.TextChoices):
    """LEGACY and NON-AUTHORITATIVE since Phase 4 (ADR-0023, "Gate offline
    policy"). Phase 3 pinned every Gate to `NOT_PERMITTED` before offline
    continuity existed. Offline continuity is governed ONLY by the global
    `ENTRY_OFFLINE_ENABLED` flag, the per-event `OfflineEventSetting` and the
    CURRENT versioned `DeviceScope` (`offline_capable`, `offline_sensitivity`)
    of the device. No code reads this value and it grants or denies nothing;
    removing the field and its constraint is a separately authorized future
    migration."""

    NOT_PERMITTED = "NOT_PERMITTED", "Not permitted"


class Gate(UUIDPrimaryKeyModel, TimestampedModel, _LocalizedNameMixin):
    """A checkpoint of a Venue (Schema §5.2)."""

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="gates")
    code = models.CharField(max_length=32)
    default_zone = models.ForeignKey(Zone, on_delete=models.PROTECT, related_name="+")
    supports_entry = models.BooleanField(default=True)
    supports_exit = models.BooleanField(default=False)
    # Deprecated, non-authoritative: see GateOfflinePolicy. Never read.
    offline_policy = models.CharField(
        max_length=16, choices=GateOfflinePolicy.choices, default=GateOfflinePolicy.NOT_PERMITTED
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "events_gate"
        constraints = [
            models.UniqueConstraint(fields=["venue", "code"], name="events_gate_code_uq"),
            models.CheckConstraint(
                condition=models.Q(offline_policy="NOT_PERMITTED"),
                name="events_gate_offline_not_permitted",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code
