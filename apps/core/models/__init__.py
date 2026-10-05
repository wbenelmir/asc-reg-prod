"""Shared abstract bases, reference values, the persistent Outbox, and the
ownership records of the staging UAT provisioning.

Schema §2.1/§3.1/§3.2: business entities use an application-generated UUID
primary key (UUIDv7, ADR-0002); mutable aggregate roots carry created_at/
updated_at/version/created_by/updated_by; append-only tables never carry a
generic updated_at.

Reference values (`Country`, `Sector`) are configuration lookups, not
business entities in the ADR-0002 sense -- they use their stable natural
code as primary key, consistent with ordinary reference-table practice, and
are never deleted once referenced (Schema §3.3: "used rows are not
deleted").
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class UUIDPrimaryKeyModel(models.Model):
    """Business-entity primary key: application-generated UUIDv7 (ADR-0002)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(models.Model):
    """`created_at`/`updated_at` for mutable aggregates (Schema §3.2)."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ActorTrackedModel(models.Model):
    """Optional operational-actor attribution (Schema §3.2).

    Nullable: many mutations are participant-initiated, not operational-user
    initiated, and a swappable-user FK must never force an actor to exist.
    """

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True


class VersionedModel(models.Model):
    """Optimistic-concurrency `version` counter (Schema §16.3).

    Incrementing on update is a service-layer responsibility (an atomic
    `UPDATE ... SET version = version + 1 WHERE version = %(expected)s`),
    never implicit model `save()` behavior.
    """

    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True


class AggregateRoot(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel, ActorTrackedModel):
    """Convenience base for a full mutable aggregate root."""

    class Meta:
        abstract = True


class ReferenceValue(models.Model):
    """Shared shape for small controlled lookup tables.

    Localization-ready (English/French/Arabic language scope)
    without implementing Prompt 4 presentation. `code` is the stable natural
    key referenced by foreign keys elsewhere.
    """

    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code  # type: ignore[attr-defined]

    @property
    def localized_name(self) -> str:
        """The display name in the current active language, with an explicit
        English fallback (Prompt 5 correction pass §5: "Country and Sector
        ModelChoice labels use name/name_fr/name_ar with an explicit English
        fallback instead of raw codes"). Mirrors
        `InterestTopic.localized_label`."""
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


class Country(ReferenceValue):
    """ISO 3166-1 alpha-2 country reference (Schema: `country_code` fields throughout)."""

    code = models.CharField(max_length=2, primary_key=True)

    class Meta:
        db_table = "core_country"
        ordering = ["code"]
        verbose_name_plural = "countries"


#: UX-2 (M07): the two entries the developer's remark excludes from every
#: selectable country list. Kosovo's "XK" is a user-assigned code outside ISO
#: 3166-1 and is simply never added; Israel's "IL" is refused if it exists.
#: The owner-approved catalog (C-01, `apps.core.reference_data.countries_v1`,
#: installed by `core.0005`) excludes both as well and makes them inactive;
#: this filter stays as a second guard.
EXCLUDED_COUNTRY_CODES: frozenset[str] = frozenset({"IL", "XK"})


def selectable_countries():
    """Countries a participant may choose: active and not excluded (M07).
    Which rows are active is set by the approved catalog (C-01). Historical
    rows referencing any country stay valid (FK `PROTECT`)."""
    return Country.objects.filter(is_active=True).exclude(code__in=EXCLUDED_COUNTRY_CODES)


class Sector(ReferenceValue):
    """Professional/organization sector reference (Schema §5.6 `sector_id`)."""

    code = models.CharField(max_length=32, primary_key=True)

    class Meta:
        db_table = "core_sector"
        ordering = ["code"]


class OutboxEventStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PUBLISHED = "PUBLISHED", "Published"
    FAILED = "FAILED", "Failed"


class OutboxEvent(UUIDPrimaryKeyModel):
    """Persistent `core.OutboxEvent` (Schema §16.5).

    `id` (UUIDv7) doubles as the global event identifier a consumer keys
    off for idempotent processing -- there is no separate `event_uuid`
    column, unlike `audit.AuditEvent`'s bigint-plus-uuid exception (that
    exception exists for partition-friendly index locality on a
    high-volume append-only table; `OutboxEvent` has no such need).

    Inserted in the SAME transaction as the domain change it describes
    (`apps.core.outbox.persistent.PersistentOutboxPublisher`); publication
    (marking PUBLISHED/FAILED, incrementing `attempts`) happens strictly
    after that transaction commits, from a `transaction.on_commit` hook or a
    later consumer pass -- never inside the same transaction as the
    publish attempt it is recording.
    """

    event_type = models.CharField(max_length=200)
    aggregate_type = models.CharField(max_length=100)
    aggregate_id = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    status = models.CharField(
        max_length=16, choices=OutboxEventStatus.choices, default=OutboxEventStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_outbox_event"
        indexes = [
            models.Index(fields=["status", "occurred_at"], name="core_outbox_status_occurred"),
            models.Index(fields=["aggregate_type", "aggregate_id"], name="core_outbox_aggregate"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.event_type}:{self.id}"


class StagingProvisioningSet(UUIDPrimaryKeyModel):
    """The ownership anchor of one staging UAT provisioning set.

    `manage.py provision_staging_uat` records every account, membership,
    organization, event edition, campaign and link it creates as a
    `StagingProvisionedObject` of its set. A repeat run reuses only those
    records and `--retire` changes only them
    (`apps.core.services.staging_provisioning`). `pattern_digest` is the
    SHA-256 of the normalized `--email-pattern`; the pattern itself is not
    stored. Staging only: the command never runs in production.
    """

    pattern_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_staging_provisioning_set"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"staging-provisioning:{self.id}"


class StagingProvisionedObjectType(models.TextChoices):
    ACCOUNT = "ACCOUNT", "Operational account"
    MEMBERSHIP = "MEMBERSHIP", "Scoped group membership"
    ORGANIZATION = "ORGANIZATION", "Organization"
    EVENT = "EVENT", "Event edition"
    CAMPAIGN = "CAMPAIGN", "Invitation campaign"
    LINK = "LINK", "Invitation link"


class StagingProvisionedObject(UUIDPrimaryKeyModel):
    """One record a staging provisioning set created (append-only).

    An object has at most one owner (`core_staging_object_uq`). `key` names
    the record in the provisioning plan (an account key, a campaign
    reference, `E2`); it is never an address.
    """

    provisioning_set = models.ForeignKey(
        StagingProvisioningSet, on_delete=models.PROTECT, related_name="owned_objects"
    )
    object_type = models.CharField(max_length=16, choices=StagingProvisionedObjectType.choices)
    object_id = models.UUIDField()
    key = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_staging_provisioned_object"
        constraints = [
            models.UniqueConstraint(
                fields=["object_type", "object_id"], name="core_staging_object_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.object_type}:{self.object_id}"


class HumanChallengeUse(models.Model):
    """One consumed human-check solution (UX-4, M01, decision S-16).

    The replay store: a solution is accepted once, because its signature
    digest is unique here. The row holds no personal data, no email, no
    network address and no hidden challenge number. It keeps only a SHA-256
    digest of the signed challenge, the action it was bound to and its expiry. Rows past
    `expires_at` are purged; a replay of an expired solution already fails the
    expiry check, so purging never reopens a replay window.
    """

    signature_digest = models.CharField(max_length=64, unique=True)
    action = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_human_challenge_use"
        indexes = [models.Index(fields=["expires_at"], name="core_humanchk_expires")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.action}:{self.signature_digest[:8]}"
