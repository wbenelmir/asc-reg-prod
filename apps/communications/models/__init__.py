"""Templates, messages, delivery attempts and suppression (Schema §13).

Bulk-communication campaigns (§13.4) and purpose-specific preferences
(§13.5's `CommunicationPreference`) are out of Phase 1 scope; `SuppressionEntry`
(also §13.5) is included since it is a direct dependency of safe message
sending. OTP content is never persisted in rendered message data here --
the OTP domain (`apps.accounts`) never calls into this app.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.fields import BlindIndexField, EncryptedTextField
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel


class CommunicationChannel(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"


class MessageTemplate(UUIDPrimaryKeyModel, TimestampedModel):
    code = models.CharField(max_length=100, unique=True)
    channel = models.CharField(max_length=8, choices=CommunicationChannel.choices)
    purpose_code = models.CharField(max_length=100)

    class Meta:
        db_table = "communications_message_template"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


class MessageTemplateVersionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"
    RETIRED = "RETIRED", "Retired"


class MessageTemplateVersion(UUIDPrimaryKeyModel, TimestampedModel):
    """One version of a `MessageTemplate` (Schema §13.1). Sent messages always
    retain the exact version reference (`CommunicationMessage.template_version`)."""

    template = models.ForeignKey(MessageTemplate, on_delete=models.CASCADE, related_name="versions")
    language = models.CharField(max_length=8)
    version_label = models.CharField(max_length=32)
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField()
    allowed_variables = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=MessageTemplateVersionStatus.choices,
        default=MessageTemplateVersionStatus.DRAFT,
    )
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    content_hash = models.CharField(max_length=64)

    class Meta:
        db_table = "communications_message_template_version"
        constraints = [
            models.UniqueConstraint(
                fields=["template", "language", "version_label"],
                name="comm_tmplversion_tmpl_lang_label_uq",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            persisted = type(self).objects.filter(pk=self.pk).first()
            if persisted is not None and (
                persisted.status != MessageTemplateVersionStatus.DRAFT
                or persisted.messages.exists()
            ):
                immutable_fields = (
                    "template_id",
                    "language",
                    "version_label",
                    "subject",
                    "body",
                    "allowed_variables",
                    "effective_from",
                    "effective_until",
                    "content_hash",
                )
                changed = any(
                    getattr(self, field) != getattr(persisted, field) for field in immutable_fields
                )
                if changed:
                    raise ValidationError("Published or used template versions are immutable.")
        return super().save(*args, **kwargs)


class CommunicationMessageStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENDING = "SENDING", "Sending"
    SENT = "SENT", "Sent"
    DELIVERED = "DELIVERED", "Delivered"
    DEFERRED = "DEFERRED", "Deferred"
    FAILED = "FAILED", "Failed"
    BOUNCED = "BOUNCED", "Bounced"
    UNDELIVERABLE = "UNDELIVERABLE", "Undeliverable"
    SUPPRESSED = "SUPPRESSED", "Suppressed"
    CANCELLED = "CANCELLED", "Cancelled"


class CommunicationMessage(UUIDPrimaryKeyModel, TimestampedModel):
    """One outbound message (Schema §13.2).

    `variable_snapshot` stores approved-only variables as JSON text inside
    an `EncryptedTextField` ("Encrypted JSONB" in the Schema's own words --
    PostgreSQL has no native encrypted-JSONB type, so this project encrypts
    the serialized JSON text, consistent with ADR-0006). OTP content is
    never persisted here under any circumstance.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
    )
    person = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    template_version = models.ForeignKey(
        MessageTemplateVersion, on_delete=models.PROTECT, related_name="messages"
    )
    channel = models.CharField(max_length=8, choices=CommunicationChannel.choices)
    language = models.CharField(max_length=8)
    destination_encrypted = EncryptedTextField()
    destination_hash = BlindIndexField()
    destination_hash_key_version = models.SmallIntegerField()
    variable_snapshot = EncryptedTextField(blank=True, default="")
    content_hash = models.CharField(max_length=64)
    # Rendered subject/body SNAPSHOTTED at queue time (Phase 2 Prompt 5 §4.1
    # "do not silently mutate historical message content when a template
    # changes"). A retryable delivery attempt reads THESE fields, never the
    # live `template_version.subject`/`.body` -- so a later edit to the
    # template version (however unlikely once PUBLISHED-and-used) can never
    # change what an already-queued message actually sends. Blank for
    # messages created before this field existed (Phase 1's own
    # `send_registration_confirmation` renders and delivers inline within a
    # single call and never retries, so it never depended on this).
    rendered_subject = models.CharField(max_length=300, blank=True, default="")
    rendered_body_encrypted = EncryptedTextField(blank=True, default="")
    idempotency_key = models.CharField(max_length=200, unique=True)
    status = models.CharField(
        max_length=16,
        choices=CommunicationMessageStatus.choices,
        default=CommunicationMessageStatus.QUEUED,
    )
    suppression_reason = models.CharField(max_length=100, blank=True, default="")
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "communications_message"
        indexes = [
            models.Index(fields=["status", "queued_at"], name="comm_message_status_idx"),
            models.Index(fields=["destination_hash"], name="comm_message_dest_hash_idx"),
        ]


class DeliveryAttempt(UUIDPrimaryKeyModel):
    """Append-only provider delivery attempt (Schema §13.3). No `updated_at`."""

    message = models.ForeignKey(
        CommunicationMessage, on_delete=models.PROTECT, related_name="attempts"
    )
    attempt_number = models.PositiveSmallIntegerField()
    provider_code = models.CharField(max_length=64, blank=True, default="")
    provider_reference = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=16, choices=CommunicationMessageStatus.choices)
    response_code = models.CharField(max_length=100, blank=True, default="")
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "communications_delivery_attempt"
        constraints = [
            models.UniqueConstraint(
                fields=["message", "attempt_number"], name="comm_delivery_msg_attempt_uq"
            ),
        ]
        indexes = [models.Index(fields=["message", "attempt_number"], name="comm_delivery_msg_idx")]


class SuppressionEntry(UUIDPrimaryKeyModel, TimestampedModel):
    """Channel-scoped suppression by destination blind index (Schema §13.5).

    Never requires the plaintext destination -- matching is by
    `destination_hash` only, using the identity blind-index key family
    (ADR-0006), the same family `people.ContactPoint` uses (this is about
    matching a real-world destination, not throttling, so it is NOT the
    separate rate-limit HMAC family from ADR-0007).
    """

    channel = models.CharField(max_length=8, choices=CommunicationChannel.choices)
    destination_hash = BlindIndexField()
    destination_hash_key_version = models.SmallIntegerField()
    reason = models.CharField(max_length=100)
    provider_evidence = models.CharField(max_length=300, blank=True, default="")
    active_from = models.DateTimeField()
    active_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "communications_suppression_entry"
        indexes = [
            models.Index(fields=["channel", "destination_hash"], name="comm_suppression_dest_idx"),
        ]
