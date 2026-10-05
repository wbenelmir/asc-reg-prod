"""Operational identity and authorization models (Schema §12).

`OperationalUser` is the project's custom Django user, introduced alone by
`accounts.0001` -- the first project migration in the whole graph
(ADR-0011). `AuthenticationChallenge` and `ScopedGroupMembership` are added
later by `accounts.0002`, once `EventEdition` and `Organization` exist to
scope against.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import Group, PermissionsMixin
from django.db import models
from django.utils import timezone


class OperationalUserAccountType(models.TextChoices):
    INTERNAL = "INTERNAL", "Internal"
    EXTERNAL_SECURITY = "EXTERNAL_SECURITY", "External security"
    ORGANIZATION = "ORGANIZATION", "Organization"
    SUPPORT = "SUPPORT", "Support"


class OperationalUserStatus(models.TextChoices):
    INVITED = "INVITED", "Invited"
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    EXPIRED = "EXPIRED", "Expired"
    DISABLED = "DISABLED", "Disabled"


class OperationalAuthenticationSource(models.TextChoices):
    LOCAL = "LOCAL", "Local"
    SSO = "SSO", "SSO"


class OperationalMfaState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ACTIVE = "ACTIVE", "Active"
    RESET_REQUIRED = "RESET_REQUIRED", "Reset required"


class OperationalUserManager(BaseUserManager):
    """Email-based manager. Normalization (lower-casing) happens here, not in `save()`."""

    use_in_migrations = True

    def _create_user(
        self, email: str, password: str | None = None, **extra_fields
    ) -> OperationalUser:
        if not email:
            raise ValueError("An email address is required.")
        email_normalized = self.normalize_email(email).lower()
        user = self.model(email_normalized=email_normalized, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self, email: str, password: str | None = None, **extra_fields
    ) -> OperationalUser:
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("account_type", OperationalUserAccountType.INTERNAL)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields
    ) -> OperationalUser:
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("status", OperationalUserStatus.ACTIVE)
        extra_fields.setdefault("account_type", OperationalUserAccountType.INTERNAL)
        extra_fields.setdefault("display_name", email)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class OperationalUser(AbstractBaseUser, PermissionsMixin):
    """Custom operational (staff/security/organization/support) user (Schema §12.1).

    Deliberately NOT a `people.Person`/`people.ParticipantAccount` -- those
    model the passwordless participant identity; this models the
    password+MFA operational identity used to run the platform.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    email_normalized = models.EmailField(unique=True, max_length=254)
    display_name = models.CharField(max_length=200)
    account_type = models.CharField(
        max_length=32,
        choices=OperationalUserAccountType.choices,
        default=OperationalUserAccountType.INTERNAL,
    )
    status = models.CharField(
        max_length=16, choices=OperationalUserStatus.choices, default=OperationalUserStatus.INVITED
    )
    authentication_source = models.CharField(
        max_length=8,
        choices=OperationalAuthenticationSource.choices,
        default=OperationalAuthenticationSource.LOCAL,
    )
    mfa_state = models.CharField(
        max_length=16, choices=OperationalMfaState.choices, default=OperationalMfaState.PENDING
    )
    active_from = models.DateTimeField(null=True, blank=True)
    active_until = models.DateTimeField(null=True, blank=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OperationalUserManager()

    USERNAME_FIELD = "email_normalized"
    REQUIRED_FIELDS = ["display_name"]

    class Meta:
        db_table = "accounts_operational_user"
        indexes = [models.Index(fields=["status"], name="accounts_user_status_idx")]

    def __str__(self) -> str:
        return self.email_normalized

    @property
    def is_active(self) -> bool:
        """Derived from `status` and the account's own `active_from`/
        `active_until` window (Phase 2 Prompt 4 "temporary external-security
        accounts": "expired accounts denied server-side") -- not a separate
        stored flag. These two columns existed since `accounts.0001` but
        were never enforced anywhere before this prompt; every account
        (not only `EXTERNAL_SECURITY`) now fails closed once its own
        window has not yet started or has already ended, exactly mirroring
        `ScopedGroupMembership`'s identical `active_from`/`active_until`
        enforcement in `effective_scoped_memberships`.
        """
        if self.status != OperationalUserStatus.ACTIVE:
            return False
        now = timezone.now()
        if self.active_from is not None and now < self.active_from:
            return False
        if self.active_until is not None and now >= self.active_until:
            return False
        return True


class AuthenticationChallengeChannel(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"


class AuthenticationChallengePurpose(models.TextChoices):
    PARTICIPANT_LOGIN = "PARTICIPANT_LOGIN", "Participant login"


class AuthenticationChallenge(models.Model):
    """Passwordless OTP challenge (Schema §12.4, ADR-0007).

    Never stores the OTP value or an unrestricted raw destination -- only a
    salted/hashed OTP (`otp_hash`, via Django's own password hasher) and
    versioned HMAC fingerprints from the SEPARATE rate-limit key family
    (ADR-0007), never the identity blind-index family used by
    `people.ContactPoint`/`people.IdentityIdentifier` (ADR-0006). Mixing
    those two families would silently reset rate limits on identity-key
    rotation and vice versa.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    channel = models.CharField(max_length=8, choices=AuthenticationChallengeChannel.choices)
    purpose = models.CharField(
        max_length=32,
        choices=AuthenticationChallengePurpose.choices,
        default=AuthenticationChallengePurpose.PARTICIPANT_LOGIN,
    )
    person = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    contact_point = models.ForeignKey(
        "people.ContactPoint", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Recipient/network fingerprints: rate-limit HMAC family only (ADR-0007).
    recipient_fingerprint = models.CharField(max_length=64)
    recipient_fingerprint_key_version = models.SmallIntegerField()
    network_fingerprint = models.CharField(max_length=64, blank=True, default="")
    network_fingerprint_key_version = models.SmallIntegerField(null=True, blank=True)

    otp_hash = models.CharField(max_length=200)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    max_attempts = models.PositiveSmallIntegerField(default=5)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    resend_available_at = models.DateTimeField(null=True, blank=True)
    request_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Bounded, non-identifying request context only -- never a plaintext "
        "recipient address, OTP value, or IP address.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_authentication_challenge"
        indexes = [
            models.Index(
                fields=[
                    "recipient_fingerprint",
                    "recipient_fingerprint_key_version",
                    "issued_at",
                ],
                name="acc_challenge_recipient_v_idx",
            ),
            models.Index(
                fields=[
                    "network_fingerprint",
                    "network_fingerprint_key_version",
                    "issued_at",
                ],
                name="acc_challenge_network_v_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the raw recipient value
        return f"{self.channel}:{self.purpose}:{self.id}"


class ScopedGroupMembershipStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    EXPIRED = "EXPIRED", "Expired"


class ScopedGroupMembership(models.Model):
    """Event/organization/venue/gate/time/status-scoped Django Group membership
    (Schema §12.3).

    `venue` and `gate` were added by `accounts.0004` (Phase 3 Prompt 4,
    ADR-0021), exactly as ADR-0010 anticipated. Null event or organization
    scope means broad scope -- application policies decide whether the
    granting user was allowed to grant that breadth; navigation is never the
    authorization boundary.

    Venue/gate scope is deliberately NOT treated as "broad" by callers that
    do not know about checkpoints: a membership narrowed to a venue or gate
    contributes a permission ONLY to a check that names that exact venue or
    gate (`apps.accounts.policies.effective_scoped_memberships`). A
    gate-scoped entry operator therefore never gains event-wide capability
    through a non-checkpoint screen.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scoped_memberships"
    )
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="scoped_memberships")
    event_edition = models.ForeignKey(
        "events.EventEdition", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    venue = models.ForeignKey(
        "events.Venue", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    gate = models.ForeignKey(
        "events.Gate", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    active_from = models.DateTimeField(null=True, blank=True)
    active_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=ScopedGroupMembershipStatus.choices,
        default=ScopedGroupMembershipStatus.ACTIVE,
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_scoped_group_membership"
        indexes = [
            models.Index(fields=["user", "status"], name="acc_scopedmembership_user_idx"),
            models.Index(
                fields=["event_edition", "organization"], name="acc_scopedmembership_scope_idx"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user_id}:{self.group_id}:{self.status}"
