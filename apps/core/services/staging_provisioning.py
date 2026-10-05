"""Staging-only provisioning of the manual UAT accounts, roles and test data.

Used by `manage.py provision_staging_uat` (see `docs/deployment/README.md` and
the account matrix in `docs/testing/phase_04_staging_uat_handoff.md` §4).

What it creates, all clearly synthetic and never authoritative:

* no country. The UAT scenarios use Algeria and France from the
  owner-approved catalog (C-01), which migration `core.0005` installs and
  `manage.py reconcile_country_catalog` keeps in line. Provisioning only
  checks that both are selectable and stops (a conflict) when they are not;
  it never creates, renames or retires a country, so it can never replace
  the catalog with a synthetic subset;
* a second, invitation-only event edition `ASC2026-UAT2` (E2) for the
  cross-event cases. The migration-seeded `ASC2026` edition (E1) is used as is
  and never modified. E2 is never REGISTRATION_OPEN, because exactly one
  edition may be open for public registration;
* one synthetic organization, one active invitation campaign per edition with
  a usable link (the link is shown once, when it is created);
* the operational accounts of the UAT matrix, each with only its documented
  group in its documented scope, granted through the existing scoped
  membership model. No superuser, no staff flag, no blanket permission.

Ownership. Every account, membership, organization, event edition, campaign
and link the run creates is recorded as owned by its provisioning set
(`apps.core.models.StagingProvisioningSet`, one per `--email-pattern`). An
existing record is reused only when this set owns it. A record that carries
one of the plan's addresses or fixed identifiers but was not created by this
set -- an ordinary or privileged account, another set's record, an
unrelated campaign, event or organization -- is a conflict, and the run
stops before writing anything.

Safeguards:

* runs only where `STAGING_PROVISIONING_ENABLED` is True (the staging runtime
  settings and the test settings; never production) and only when the
  operator types the deployment's own host name (`confirm_host`);
* preview by default; `apply=True` plans again and writes in one
  transaction, and only when the plan found no conflict;
* no password is ever set or printed. Accounts are created with an unusable
  password; the operator sets each one interactively with Django's
  `manage.py changepassword <email>` (or the account stays unusable);
* `retire()` changes only owned records: it disables the owned accounts,
  suspends the owned memberships, revokes the owned links (or a link rotated
  from one), closes the owned campaigns and archives an owned E2. A
  membership granted by someone else, even to a UAT account, is left as it
  is. It stops on an unsafe state, such as an owned account that was given
  staff or superuser rights. Nothing is deleted: audit history and any test
  registration stay. A retired set is not reopened.
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

PROVISIONING_REASON = "Staging UAT provisioning (synthetic test account)"

E1_CODE = "ASC2026"
E2_CODE = "ASC2026-UAT2"
E2_NAME = "ASC 2026 UAT second edition (synthetic, staging only)"
ORGANIZATION_NAME = "ASC UAT Synthetic Organization"

#: The countries the UAT scenarios use, from the approved catalog (C-01).
#: Checked, never created or changed.
REQUIRED_COUNTRY_CODES = ("DZ", "FR")

CAMPAIGNS = (
    ("UAT-E1-INVITATION", "E1", "UAT invitation campaign (E1, synthetic)"),
    ("UAT-E2-INVITATION", "E2", "UAT invitation campaign (E2, synthetic)"),
)
CAMPAIGN_CAPACITY = 50

#: How far `retire` follows a link's rotation history back to an owned link.
MAX_ROTATIONS_FOLLOWED = 1000


@dataclass(frozen=True)
class MembershipSpec:
    group: str
    event: str  # "E1" or "E2"
    organization: bool = False


@dataclass(frozen=True)
class AccountSpec:
    key: str
    display_name: str
    memberships: tuple[MembershipSpec, ...] = ()
    temporary: bool = False
    external_security: bool = False


#: The UAT account matrix (handoff §4). `provisioning` is the non-login record
#: in whose name the memberships are granted.
PROVISIONING_KEY = "provisioning"
ACCOUNTS: tuple[AccountSpec, ...] = (
    AccountSpec(
        "reviewer",
        "UAT Reviewer R",
        (
            MembershipSpec("Registration Reviewers", "E1"),
            MembershipSpec("Registration Reviewers", "E2"),
        ),
    ),
    AccountSpec(
        "manager",
        "UAT Manager M",
        (
            MembershipSpec("Accreditation Managers", "E1"),
            MembershipSpec("Accreditation Managers", "E2"),
        ),
    ),
    AccountSpec("intake", "UAT Intake I", (MembershipSpec("Registration Intake", "E1"),)),
    AccountSpec("outsider", "UAT Outsider O", (MembershipSpec("Registration Reviewers", "E2"),)),
    AccountSpec(
        "coordinator", "UAT Coordinator A", (MembershipSpec("Accreditation Coordinators", "E1"),)
    ),
    AccountSpec(
        "comms", "UAT Communications CO", (MembershipSpec("Communication Operators", "E1"),)
    ),
    AccountSpec(
        "channels",
        "UAT Channel Manager CM",
        (MembershipSpec("Registration Channel Manager", "E1"),),
    ),
    AccountSpec(
        "invitations",
        "UAT Invitation Manager IM",
        (MembershipSpec("Invitation Manager", "E1", organization=True),),
    ),
    AccountSpec(
        "onbehalf",
        "UAT On-Behalf Registrar OB",
        (MembershipSpec("On-Behalf Registrar", "E1", organization=True),),
    ),
    AccountSpec(
        "orguser",
        "UAT Organization User OU",
        (MembershipSpec("Organization User", "E1", organization=True),),
    ),
    AccountSpec("admin", "UAT Administrator T-ADMIN"),
    AccountSpec("temp", "UAT Temporary Operator T-TEMP", temporary=True),
    AccountSpec("external", "UAT External Security T-EXT", external_security=True),
    AccountSpec(
        "srm",
        "UAT Security Restriction Manager T-SRM",
        (MembershipSpec("Security Restriction Managers", "E1"),),
    ),
)


class ProvisioningRefused(Exception):
    """A safeguard refused the run. The message names the safeguard, never a secret."""


class _OwnershipChanged(Exception):
    """Raised inside the write transaction when a planned reuse is no longer owned."""


@dataclass
class PlanLine:
    kind: str
    key: str
    action: str  # create | exists | conflict | retire | unchanged
    detail: str = ""


@dataclass
class ProvisioningReport:
    lines: list[PlanLine] = field(default_factory=list)
    invitation_urls: dict[str, str] = field(default_factory=dict)
    created_account_emails: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def conflicts(self) -> list[PlanLine]:
        return [line for line in self.lines if line.action == "conflict"]

    def add(self, kind: str, key: str, action: str, detail: str = "") -> None:
        self.lines.append(PlanLine(kind, key, action, detail))


# -- safeguards ---------------------------------------------------------------


def check_safeguards(*, confirm_host: str) -> None:
    if not getattr(settings, "STAGING_PROVISIONING_ENABLED", False):
        raise ProvisioningRefused(
            "Staging provisioning is disabled in this settings module; it runs only with "
            "config.settings.staging."
        )
    if getattr(settings, "DATABASE_PROCESS_ROLE", None) == "migration":
        raise ProvisioningRefused(
            "Run provisioning with the runtime settings, not the migration settings."
        )
    expected = (urlsplit(getattr(settings, "PUBLIC_BASE_URL", "")).hostname or "").lower()
    if not expected or (confirm_host or "").strip().lower() != expected:
        raise ProvisioningRefused(
            "--confirm-host must be exactly the host name of DJANGO_PUBLIC_BASE_URL."
        )


def account_email(email_pattern: str, key: str) -> str:
    fields = [name for _text, name, _spec, _conv in string.Formatter().parse(email_pattern) if name]
    if fields != ["key"]:
        raise ProvisioningRefused(
            "--email-pattern must contain the placeholder {key} exactly once."
        )
    email = email_pattern.format(key=key).strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ProvisioningRefused(
            "--email-pattern does not produce valid email addresses."
        ) from None
    return email


def _validate_pattern(email_pattern: str) -> dict[str, str]:
    keys = [PROVISIONING_KEY, *(spec.key for spec in ACCOUNTS)]
    emails = {key: account_email(email_pattern, key) for key in keys}
    if len(set(emails.values())) != len(emails):
        raise ProvisioningRefused("--email-pattern must give every account its own address.")
    return emails


def pattern_digest(email_pattern: str) -> str:
    """The provisioning set's identity: SHA-256 of the normalized pattern."""
    return hashlib.sha256(email_pattern.strip().lower().encode()).hexdigest()


# -- ownership -----------------------------------------------------------------


def _owned_ids(provisioning_set, object_type: str) -> set:
    if provisioning_set is None:
        return set()
    return set(
        provisioning_set.owned_objects.filter(object_type=object_type).values_list(
            "object_id", flat=True
        )
    )


def _ownership(provisioning_set, object_type: str, object_id) -> str:
    """`owned` (by this set), `other-set`, or `unowned`."""
    from apps.core.models import StagingProvisionedObject

    row = StagingProvisionedObject.objects.filter(
        object_type=object_type, object_id=object_id
    ).first()
    if row is None:
        return "unowned"
    if provisioning_set is not None and row.provisioning_set_id == provisioning_set.pk:
        return "owned"
    return "other-set"


def _ownership_conflict(ownership: str, what: str) -> str:
    if ownership == "other-set":
        return f"{what} belongs to another provisioning set"
    return f"{what} exists and was not created by this provisioning set"


def _own(provisioning_set, object_type: str, obj, key: str) -> None:
    from apps.core.models import StagingProvisionedObject

    StagingProvisionedObject.objects.create(
        provisioning_set=provisioning_set, object_type=object_type, object_id=obj.pk, key=key
    )


def _require_owned(provisioning_set, object_type: str, obj):
    if _ownership(provisioning_set, object_type, obj.pk) != "owned":
        raise _OwnershipChanged(object_type)
    return obj


# -- planning (read-only) -------------------------------------------------------


def _plan_set(report: ProvisioningReport, email_pattern: str):
    from apps.core.models import StagingProvisioningSet

    provisioning_set = StagingProvisioningSet.objects.filter(
        pattern_digest=pattern_digest(email_pattern)
    ).first()
    if provisioning_set is None:
        report.add("set", "uat", "create", "new provisioning set for this --email-pattern")
    elif provisioning_set.retired_at is not None:
        report.add(
            "set",
            "uat",
            "conflict",
            f"retired on {provisioning_set.retired_at:%Y-%m-%d}; a retired set is not reopened",
        )
    else:
        report.add("set", "uat", "exists", f"created {provisioning_set.created_at:%Y-%m-%d}")
    return provisioning_set


def _plan_events(report: ProvisioningReport, provisioning_set):
    from apps.core.models import StagingProvisionedObjectType
    from apps.events.models import EventEdition, EventEditionStatus

    e1 = EventEdition.objects.filter(code=E1_CODE).first()
    if e1 is None:
        report.add("event", "E1", "conflict", f"{E1_CODE} is missing: apply the migrations first")
    else:
        detail = (
            f"{E1_CODE}, status {e1.status}, public mode {e1.public_registration_mode} (used as is)"
        )
        report.add("event", "E1", "exists", detail)
        others_open = EventEdition.objects.filter(
            status=EventEditionStatus.REGISTRATION_OPEN
        ).exclude(pk=e1.pk)
        if others_open.exists():
            report.add(
                "event",
                "E1",
                "conflict",
                "another edition is open for registration; exactly one may be open",
            )
    e2 = EventEdition.objects.filter(code=E2_CODE).first()
    if e2 is None:
        report.add("event", "E2", "create", f"{E2_CODE}, invitation only, never open")
        return e1, None
    ownership = _ownership(provisioning_set, StagingProvisionedObjectType.EVENT, e2.pk)
    if ownership != "owned":
        report.add("event", "E2", "conflict", _ownership_conflict(ownership, E2_CODE))
    elif e2.status == EventEditionStatus.REGISTRATION_OPEN:
        report.add("event", "E2", "conflict", f"{E2_CODE} is open for registration")
    else:
        report.add("event", "E2", "exists", f"{E2_CODE}, status {e2.status} (unchanged)")
    return e1, e2


def _plan_organization(report: ProvisioningReport, provisioning_set):
    from apps.core.models import StagingProvisionedObjectType
    from apps.organizations.models import Organization
    from apps.organizations.services import normalize_organization_name

    matches = list(
        Organization.objects.filter(normalized_name=normalize_organization_name(ORGANIZATION_NAME))
    )
    if not matches:
        report.add("organization", "uat", "create", ORGANIZATION_NAME)
        return None
    if len(matches) > 1:
        report.add("organization", "uat", "conflict", "more than one synthetic UAT organization")
        return None
    ownership = _ownership(
        provisioning_set, StagingProvisionedObjectType.ORGANIZATION, matches[0].pk
    )
    if ownership != "owned":
        report.add(
            "organization", "uat", "conflict", _ownership_conflict(ownership, ORGANIZATION_NAME)
        )
        return None
    report.add("organization", "uat", "exists", ORGANIZATION_NAME)
    return matches[0]


def _plan_campaigns(report: ProvisioningReport, provisioning_set, e1, e2, organization) -> None:
    from apps.core.models import StagingProvisionedObjectType
    from apps.invitations.models import InvitationCampaign

    for reference, event_key, _name in CAMPAIGNS:
        campaign = InvitationCampaign.objects.filter(public_reference=reference).first()
        event = e1 if event_key == "E1" else e2
        if campaign is None:
            report.add(
                "campaign", reference, "create", f"{event_key}, capacity {CAMPAIGN_CAPACITY}"
            )
            continue
        ownership = _ownership(provisioning_set, StagingProvisionedObjectType.CAMPAIGN, campaign.pk)
        if ownership != "owned":
            report.add("campaign", reference, "conflict", _ownership_conflict(ownership, reference))
        elif (event is None or campaign.event_edition_id != event.pk) or (
            organization is None or campaign.organization_id != organization.pk
        ):
            report.add(
                "campaign", reference, "conflict", "belongs to another event or organization"
            )
        else:
            report.add("campaign", reference, "exists", f"status {campaign.status} (unchanged)")


def _plan_accounts(
    report: ProvisioningReport, provisioning_set, emails: dict[str, str], events, organization
) -> None:
    from django.contrib.auth.models import Group

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserAccountType,
        OperationalUserStatus,
        ScopedGroupMembership,
        ScopedGroupMembershipStatus,
    )
    from apps.core.models import StagingProvisionedObjectType

    owned_users = {}
    refused_keys = set()
    for key in (PROVISIONING_KEY, *(spec.key for spec in ACCOUNTS)):
        user = OperationalUser.objects.filter(email_normalized=emails[key]).first()
        spec = next((item for item in ACCOUNTS if item.key == key), None)
        if user is None:
            report.add("account", key, "create", emails[key])
            continue
        refused_keys.add(key)
        ownership = _ownership(provisioning_set, StagingProvisionedObjectType.ACCOUNT, user.pk)
        expected_type = (
            OperationalUserAccountType.EXTERNAL_SECURITY
            if spec is not None and spec.external_security
            else OperationalUserAccountType.INTERNAL
        )
        if ownership != "owned":
            report.add("account", key, "conflict", _ownership_conflict(ownership, emails[key]))
        elif user.is_superuser or user.is_staff:
            report.add("account", key, "conflict", f"{emails[key]} is a privileged account")
        elif user.account_type != expected_type:
            report.add("account", key, "conflict", f"{emails[key]} has another account type")
        elif key == PROVISIONING_KEY and user.status != OperationalUserStatus.SUSPENDED:
            report.add(
                "account", key, "conflict", f"{emails[key]} must stay suspended (cannot sign in)"
            )
        elif key != PROVISIONING_KEY and user.status != OperationalUserStatus.ACTIVE:
            report.add(
                "account",
                key,
                "conflict",
                f"{emails[key]} exists with status {user.status}; use a new --email-pattern",
            )
        else:
            report.add("account", key, "exists", f"{emails[key]} (unchanged)")
            owned_users[key] = user
            refused_keys.discard(key)

    owned_memberships = _owned_ids(provisioning_set, StagingProvisionedObjectType.MEMBERSHIP)
    for spec in ACCOUNTS:
        if spec.key in refused_keys:
            continue  # the account line already reports the conflict
        user = owned_users.get(spec.key)
        for membership in spec.memberships:
            label = f"{membership.group} @{membership.event}" + (
                " + organization" if membership.organization else ""
            )
            if not Group.objects.filter(name=membership.group).exists():
                report.add(
                    "membership",
                    spec.key,
                    "conflict",
                    f"group {membership.group!r} is missing: apply the migrations first",
                )
                continue
            event = events[membership.event]
            granted = None
            if user is not None and event is not None:
                scope = (
                    {"organization": organization}
                    if membership.organization
                    else {"organization__isnull": True}
                )
                granted = ScopedGroupMembership.objects.filter(
                    id__in=owned_memberships,
                    user=user,
                    group__name=membership.group,
                    event_edition=event,
                    venue__isnull=True,
                    gate__isnull=True,
                    **scope,
                ).first()
            if granted is None:
                report.add("membership", spec.key, "create", label)
            elif granted.status != ScopedGroupMembershipStatus.ACTIVE:
                report.add(
                    "membership",
                    spec.key,
                    "conflict",
                    f"{label} is {granted.status}; provisioning does not grant it again",
                )
            else:
                report.add("membership", spec.key, "exists", label)


def _plan(report: ProvisioningReport, email_pattern: str, emails: dict[str, str]):
    from apps.core.models import selectable_countries

    for code in REQUIRED_COUNTRY_CODES:
        if selectable_countries().filter(code=code).exists():
            report.add("country", code, "exists", "approved catalog (C-01)")
        else:
            report.add(
                "country",
                code,
                "conflict",
                "not selectable: install the approved catalog (migrate, then "
                "reconcile_country_catalog --apply)",
            )
    provisioning_set = _plan_set(report, email_pattern)
    e1, e2 = _plan_events(report, provisioning_set)
    organization = _plan_organization(report, provisioning_set)
    _plan_campaigns(report, provisioning_set, e1, e2, organization)
    _plan_accounts(report, provisioning_set, emails, {"E1": e1, "E2": e2}, organization)
    return provisioning_set


def build_plan(*, email_pattern: str, confirm_host: str) -> ProvisioningReport:
    """The preview: what `apply_plan` would create or reuse. Writes nothing."""
    check_safeguards(confirm_host=confirm_host)
    emails = _validate_pattern(email_pattern)
    report = ProvisioningReport()
    _plan(report, email_pattern, emails)
    return report


# -- apply ---------------------------------------------------------------------


def _audit(
    action_code: str, *, target_type: str, target_uuid=None, event_edition_id=None, after=None
):
    from apps.audit.contracts import AuditRecord
    from apps.audit.services import PersistentAuditRecorder

    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="SYSTEM",
            action_code=action_code,
            target_type=target_type,
            target_uuid=target_uuid,
            event_edition_id=event_edition_id,
            result="SUCCESS",
            reason_code="STAGING_UAT",
            after_summary=after,
        )
    )


def apply_plan(
    *, email_pattern: str, confirm_host: str, temporary_days: int = 14
) -> ProvisioningReport:
    """Create what the plan reports as missing; reuse only owned records; refuse on conflict.

    The plan is computed again inside the write transaction, so what is
    written is what was just checked. A conflict, a record that stops being
    owned, or a concurrent insert of the same identifier rolls everything
    back: nothing is written.
    """
    if not 1 <= temporary_days <= 60:
        raise ProvisioningRefused("--temporary-days must be between 1 and 60.")
    check_safeguards(confirm_host=confirm_host)
    emails = _validate_pattern(email_pattern)
    report = ProvisioningReport()
    try:
        with transaction.atomic():
            provisioning_set = _plan(report, email_pattern, emails)
            if report.conflicts:
                return report
            _write(report, email_pattern, emails, provisioning_set, temporary_days)
    except _OwnershipChanged, IntegrityError:
        report.lines.clear()
        report.invitation_urls.clear()
        report.created_account_emails.clear()
        report.add(
            "run",
            "apply",
            "conflict",
            "a planned record changed during the run; nothing was written, run it again",
        )
        return report
    report.applied = True
    return report


def _write(report, email_pattern, emails, provisioning_set, temporary_days) -> None:
    from django.contrib.auth.models import Group

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserAccountType,
        OperationalUserStatus,
        ScopedGroupMembership,
        ScopedGroupMembershipStatus,
    )
    from apps.accounts.services import create_external_security_account
    from apps.audit import action_codes
    from apps.core.models import StagingProvisionedObjectType, StagingProvisioningSet
    from apps.events.models import EventEdition, EventEditionStatus, PublicRegistrationMode
    from apps.invitations.models import InvitationCampaign, InvitationCampaignStatus
    from apps.invitations.services import (
        change_campaign_status,
        create_campaign,
        issue_initial_link,
    )
    from apps.organizations.models import Organization
    from apps.organizations.services import create_organization, normalize_organization_name
    from apps.registrations.models import InterestTopic

    kinds = StagingProvisionedObjectType
    now = timezone.now()
    memberships_created = 0
    if provisioning_set is None:
        provisioning_set = StagingProvisioningSet.objects.create(
            pattern_digest=pattern_digest(email_pattern)
        )

    e1 = EventEdition.objects.get(code=E1_CODE)
    e2 = EventEdition.objects.filter(code=E2_CODE).first()
    if e2 is None:
        e2 = EventEdition.objects.create(
            code=E2_CODE,
            name=E2_NAME,
            timezone=e1.timezone,
            starts_at=e1.starts_at,
            ends_at=e1.ends_at,
            status=EventEditionStatus.REGISTRATION_CLOSED,
            public_registration_mode=PublicRegistrationMode.INVITATION_ONLY,
            default_language=e1.default_language,
            supported_languages=list(e1.supported_languages),
            minimum_participant_age=e1.minimum_participant_age,
        )
        _own(provisioning_set, kinds.EVENT, e2, "E2")
        _audit(
            action_codes.STAGING_UAT_DATA_PROVISIONED,
            target_type="EventEdition",
            target_uuid=e2.pk,
            event_edition_id=e2.pk,
            after={"code": E2_CODE},
        )
    else:
        _require_owned(provisioning_set, kinds.EVENT, e2)
    for topic in InterestTopic.objects.filter(event_edition=e1):
        InterestTopic.objects.get_or_create(
            event_edition=e2,
            code=topic.code,
            defaults={
                "label": topic.label,
                "label_fr": topic.label_fr,
                "label_ar": topic.label_ar,
                "group_code": topic.group_code,
                "is_active": topic.is_active,
            },
        )

    organization = Organization.objects.filter(
        normalized_name=normalize_organization_name(ORGANIZATION_NAME)
    ).first()
    if organization is None:
        organization = create_organization(
            official_name=ORGANIZATION_NAME, organization_type="STARTUP", country_code_id="DZ"
        )
        _own(provisioning_set, kinds.ORGANIZATION, organization, "uat")
    else:
        _require_owned(provisioning_set, kinds.ORGANIZATION, organization)

    provisioner = OperationalUser.objects.filter(email_normalized=emails[PROVISIONING_KEY]).first()
    if provisioner is None:
        provisioner = OperationalUser.objects.create_user(
            email=emails[PROVISIONING_KEY],
            password=None,
            display_name="UAT provisioning record (cannot sign in)",
            status=OperationalUserStatus.SUSPENDED,
        )
        _own(provisioning_set, kinds.ACCOUNT, provisioner, PROVISIONING_KEY)
        _audit(
            action_codes.STAGING_UAT_ACCOUNT_CREATED,
            target_type="OperationalUser",
            target_uuid=provisioner.pk,
            after={"key": PROVISIONING_KEY},
        )
    else:
        _require_owned(provisioning_set, kinds.ACCOUNT, provisioner)

    owned_memberships = _owned_ids(provisioning_set, kinds.MEMBERSHIP)
    for spec in ACCOUNTS:
        user = OperationalUser.objects.filter(email_normalized=emails[spec.key]).first()
        if user is None:
            if spec.external_security:
                user = create_external_security_account(
                    email=emails[spec.key],
                    display_name=spec.display_name,
                    expires_at=now + timedelta(days=temporary_days),
                    created_by=provisioner,
                    event_edition=e1,
                )
                # The service grants the account its own temporary group: owned too.
                for granted in ScopedGroupMembership.objects.filter(user=user):
                    _own(provisioning_set, kinds.MEMBERSHIP, granted, spec.key)
            else:
                user = OperationalUser.objects.create_user(
                    email=emails[spec.key],
                    password=None,
                    display_name=spec.display_name,
                    account_type=OperationalUserAccountType.INTERNAL,
                    status=OperationalUserStatus.ACTIVE,
                    active_from=now,
                    active_until=(now + timedelta(days=temporary_days)) if spec.temporary else None,
                )
            _own(provisioning_set, kinds.ACCOUNT, user, spec.key)
            _audit(
                action_codes.STAGING_UAT_ACCOUNT_CREATED,
                target_type="OperationalUser",
                target_uuid=user.pk,
                after={"key": spec.key},
            )
            report.created_account_emails.append(emails[spec.key])
        else:
            _require_owned(provisioning_set, kinds.ACCOUNT, user)
        for membership in spec.memberships:
            event = e1 if membership.event == "E1" else e2
            org = organization if membership.organization else None
            group = Group.objects.get(name=membership.group)
            if ScopedGroupMembership.objects.filter(
                id__in=owned_memberships,
                user=user,
                group=group,
                event_edition=event,
                organization=org,
                venue__isnull=True,
                gate__isnull=True,
                status=ScopedGroupMembershipStatus.ACTIVE,
            ).exists():
                continue
            granted = ScopedGroupMembership.objects.create(
                user=user,
                group=group,
                event_edition=event,
                organization=org,
                active_from=now,
                active_until=user.active_until,
                granted_by=provisioner,
                reason=PROVISIONING_REASON,
            )
            _own(provisioning_set, kinds.MEMBERSHIP, granted, spec.key)
            _audit(
                action_codes.STAGING_UAT_MEMBERSHIP_GRANTED,
                target_type="ScopedGroupMembership",
                target_uuid=granted.pk,
                event_edition_id=event.pk,
                after={
                    "key": spec.key,
                    "group": membership.group,
                    "event": event.code,
                    "organization_scoped": bool(org),
                },
            )
            memberships_created += 1

    for reference, event_key, name in CAMPAIGNS:
        event = e1 if event_key == "E1" else e2
        existing = InvitationCampaign.objects.filter(public_reference=reference).first()
        if existing is not None:
            _require_owned(provisioning_set, kinds.CAMPAIGN, existing)
            continue
        campaign = create_campaign(
            event_edition=event,
            organization=organization,
            name=name,
            public_reference=reference,
            capacity=CAMPAIGN_CAPACITY,
            created_by=provisioner,
        )
        _own(provisioning_set, kinds.CAMPAIGN, campaign, reference)
        campaign = change_campaign_status(
            campaign, InvitationCampaignStatus.ACTIVE, actor=provisioner
        )
        link, raw_token = issue_initial_link(campaign, actor=provisioner)
        _own(provisioning_set, kinds.LINK, link, reference)
        path = reverse("invitations:invitation-start", kwargs={"token": raw_token})
        report.invitation_urls[reference] = f"{settings.PUBLIC_BASE_URL}{path}"

    _audit(
        action_codes.STAGING_UAT_PROVISIONED,
        target_type="EventEdition",
        target_uuid=e1.pk,
        event_edition_id=e1.pk,
        after={
            "provisioning_set": str(provisioning_set.pk),
            "accounts_created": len(report.created_account_emails),
            "memberships_created": memberships_created,
            "campaigns_created": len(report.invitation_urls),
        },
    )


# -- retire --------------------------------------------------------------------


def _descends_from(link, owned_link_ids: set) -> bool:
    """True when `link` is an owned link or was rotated, directly or not, from one."""
    current = link
    for _step in range(MAX_ROTATIONS_FOLLOWED):
        if current is None:
            return False
        if current.pk in owned_link_ids:
            return True
        current = current.rotated_from
    return False


def retire(*, email_pattern: str, confirm_host: str, apply: bool = False) -> ProvisioningReport:
    """Disable the owned UAT accounts, suspend the owned memberships, revoke the
    owned links, close the owned campaigns and archive an owned E2. Records the
    set did not create are never changed. Stops, writing nothing, on any
    conflict. Deletes nothing."""
    check_safeguards(confirm_host=confirm_host)
    _validate_pattern(email_pattern)

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserStatus,
        ScopedGroupMembership,
        ScopedGroupMembershipStatus,
    )
    from apps.audit import action_codes
    from apps.core.models import StagingProvisionedObjectType, StagingProvisioningSet
    from apps.events.models import EventEdition, EventEditionStatus
    from apps.invitations.models import (
        InvitationCampaign,
        InvitationCampaignStatus,
        InvitationLink,
        InvitationLinkStatus,
    )
    from apps.invitations.services import change_campaign_status, revoke_link

    kinds = StagingProvisionedObjectType
    report = ProvisioningReport()
    with transaction.atomic():
        provisioning_set = (
            StagingProvisioningSet.objects.select_for_update()
            .filter(pattern_digest=pattern_digest(email_pattern))
            .first()
        )
        if provisioning_set is None:
            report.add(
                "set",
                "uat",
                "conflict",
                "no provisioning set was created with this --email-pattern; nothing is retired",
            )
            return report
        if provisioning_set.retired_at is not None:
            report.add(
                "set", "uat", "unchanged", f"retired on {provisioning_set.retired_at:%Y-%m-%d}"
            )
            report.applied = apply
            return report

        keys = dict(
            provisioning_set.owned_objects.values_list("object_id", "key").order_by("created_at")
        )
        owned_memberships = _owned_ids(provisioning_set, kinds.MEMBERSHIP)
        owned_links = _owned_ids(provisioning_set, kinds.LINK)

        accounts = []
        for user in OperationalUser.objects.filter(
            pk__in=_owned_ids(provisioning_set, kinds.ACCOUNT)
        ).order_by("created_at"):
            key = keys.get(user.pk, "")
            if key == PROVISIONING_KEY:
                continue  # the non-login grantor record stays suspended
            if user.is_superuser or user.is_staff:
                report.add(
                    "account",
                    key,
                    "conflict",
                    "was given staff or superuser rights after provisioning; review it first",
                )
                continue
            active = ScopedGroupMembership.objects.filter(
                user=user, status=ScopedGroupMembershipStatus.ACTIVE
            )
            mine = active.filter(id__in=owned_memberships)
            others = active.exclude(id__in=owned_memberships).count()
            detail = f"disable; suspend {mine.count()} provisioned membership(s)"
            if others:
                detail += f"; {others} membership(s) granted by others left unchanged"
            report.add("account", key, "retire", detail)
            accounts.append((user, mine))

        campaigns = []
        for campaign in InvitationCampaign.objects.filter(
            pk__in=_owned_ids(provisioning_set, kinds.CAMPAIGN)
        ).order_by("public_reference"):
            reference = campaign.public_reference
            if campaign.status == InvitationCampaignStatus.CLOSED:
                report.add("campaign", reference, "unchanged", "already closed")
                continue
            link = InvitationLink.objects.filter(
                campaign=campaign, status=InvitationLinkStatus.ACTIVE
            ).first()
            if link is not None and not _descends_from(link, owned_links):
                report.add(
                    "campaign",
                    reference,
                    "conflict",
                    "its active link was not issued by provisioning; review it first",
                )
                continue
            report.add("campaign", reference, "retire", "revoke the link; close the campaign")
            campaigns.append((campaign, link))

        e2 = EventEdition.objects.filter(pk__in=_owned_ids(provisioning_set, kinds.EVENT)).first()
        if e2 is not None and e2.status != EventEditionStatus.ARCHIVED:
            report.add("event", "E2", "retire", "archive")

        if report.conflicts or not apply:
            return report

        provisioner = OperationalUser.objects.filter(
            pk__in=[pk for pk, key in keys.items() if key == PROVISIONING_KEY]
        ).first()
        now = timezone.now()
        for user, mine in accounts:
            mine.update(status=ScopedGroupMembershipStatus.SUSPENDED, updated_at=now)
            user.status = OperationalUserStatus.DISABLED
            user.save(update_fields=["status", "updated_at"])
        for campaign, link in campaigns:
            if link is not None:
                revoke_link(campaign, actor=provisioner)
            change_campaign_status(campaign, InvitationCampaignStatus.CLOSED, actor=provisioner)
        if e2 is not None and e2.status != EventEditionStatus.ARCHIVED:
            e2.status = EventEditionStatus.ARCHIVED
            e2.save(update_fields=["status", "updated_at"])
        provisioning_set.retired_at = now
        provisioning_set.save(update_fields=["retired_at"])
        _audit(
            action_codes.STAGING_UAT_RETIRED,
            target_type="EventEdition",
            after={
                "provisioning_set": str(provisioning_set.pk),
                "retired": sum(1 for line in report.lines if line.action == "retire"),
            },
        )
    report.applied = True
    return report
