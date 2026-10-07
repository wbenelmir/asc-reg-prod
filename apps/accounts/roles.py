"""The controlled catalogue of staff roles an account administrator may grant.

Each role is one of the project's existing Django groups, created by the
`post_migrate` bootstrap of its own app with a fixed, least-privilege
permission set. The administration area grants a role as a
`ScopedGroupMembership` -- it never edits a group's permissions, never sets
`is_staff` or `is_superuser`, and never grants a bare permission. A group that
is not listed here cannot be granted from the administration area.

`scopes` says which narrowing a role accepts, besides the event edition
(a `global_only` role accepts none at all, not even an event edition):

* `ORGANIZATION` -- the role may be limited to one organization;
* `CHECKPOINT` -- the role may be limited to one venue or one gate (the entry
  checkpoint roles; such a membership only counts at that exact checkpoint).

Edition-wide roles accept neither: an organization-narrowed membership of
such a role would never count for its edition-wide pages.

The External Security (Temporary) group is deliberately absent: temporary
external-security accounts keep their own service and expiry semantics
(`apps.accounts.services`); the administration area grants them ordinary roles
only within their account expiry.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

ORGANIZATION = "ORGANIZATION"
CHECKPOINT = "CHECKPOINT"


@dataclass(frozen=True)
class StaffRole:
    key: str
    group_name: str
    label: Promise
    description: Promise
    scopes: frozenset[str] = frozenset()
    #: Granted only for every event edition and every organization (a
    #: platform-wide technical role); `grant_role` refuses any narrower scope.
    global_only: bool = False


STAFF_ROLES: tuple[StaffRole, ...] = (
    StaffRole(
        "account-administrator",
        "Account Administrators",
        _("Account administrator"),
        _("Creates staff accounts, sends setup links and grants roles within their own scope."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "registration-intake",
        "Registration Intake",
        _("Registration intake (read only)"),
        _("Reads registrations in scope."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "registration-reviewer",
        "Registration Reviewers",
        _("Registration reviewer"),
        _("Reviews registrations and identity documents; no final decision."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "accreditation-manager",
        "Accreditation Managers",
        _("Accreditation manager"),
        _("Records decisions, chooses attendance days, reopens and cancels registrations."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "accreditation-coordinator",
        "Accreditation Coordinators",
        _("Accreditation coordinator"),
        _("Assigns participant roles, badge types, access profiles and access rules."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "attendance-policy-manager",
        "Attendance Policy Managers",
        _("Attendance policy manager"),
        _("Sets the conference days and opening-day capacity and switches enforcement."),
    ),
    StaffRole(
        "pass-administrator",
        "Pass Administrators",
        _("Entry pass administrator"),
        _("Generates, activates, suspends and replaces digital entry passes."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "pass-viewer",
        "Pass Viewers",
        _("Entry pass viewer"),
        _("Reads digital entry passes."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "credential-key-custodian",
        "Credential Key Custodians",
        _("Credential key custodian"),
        _("Publishes, promotes and retires entry pass verification keys."),
    ),
    StaffRole(
        "badge-stock-administrator",
        "Badge Stock Administrators",
        _("Badge stock administrator"),
        _("Manages badge stock locations, print batches and transfers."),
    ),
    StaffRole(
        "badge-stock-issuer",
        "Badge Stock Issuers",
        _("Badge issuer"),
        _("Hands over physical badges and records their attendance marking."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "entry-operator",
        "Entry Operators",
        _("Entry operator"),
        _("Verifies participants and records admissions at a checkpoint."),
        frozenset({CHECKPOINT}),
    ),
    StaffRole(
        "entry-supervisor",
        "Entry Supervisors",
        _("Entry supervisor"),
        _("Supervises checkpoints and approves overrides."),
        frozenset({CHECKPOINT}),
    ),
    StaffRole(
        "entry-device-administrator",
        "Entry Device Administrators",
        _("Entry device administrator"),
        _("Enrolls and manages entry devices."),
    ),
    StaffRole(
        "security-restriction-manager",
        "Security Restriction Managers",
        _("Security restriction manager"),
        _("Manages security restrictions."),
    ),
    StaffRole(
        "registration-channel-manager",
        "Registration Channel Manager",
        _("Registration channel manager"),
        _("Opens, restricts or closes registration channels."),
    ),
    StaffRole(
        "communication-operator",
        "Communication Operators",
        _("Communication operator"),
        _("Reads and follows up outgoing messages."),
    ),
    StaffRole(
        "export-administrator",
        "Export Administrators",
        _("Export administrator"),
        _("Prepares purpose-bound exports."),
    ),
    StaffRole(
        "invitation-manager",
        "Invitation Manager",
        _("Invitation manager"),
        _("Manages invitation campaigns."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "organization-user",
        "Organization User",
        _("Organization user"),
        _("Follows an organization's invitations and registrations."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "delegation-coordinator",
        "Delegation Coordinator",
        _("Delegation coordinator"),
        _("Imports delegation lists for an organization."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "on-behalf-registrar",
        "On-Behalf Registrar",
        _("On-behalf registrar"),
        _("Registers participants on their behalf."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "privacy-administrator",
        "Privacy Administrators",
        _("Privacy administrator"),
        _("Manages legal notices and privacy operations."),
    ),
    StaffRole(
        "accommodation-coordinator",
        "Accommodation Support Coordinator",
        _("Accessibility support coordinator"),
        _("Reads accessibility support requests in scope."),
        frozenset({ORGANIZATION}),
    ),
    StaffRole(
        "integration-diagnostics",
        "Integration Diagnostics Operators",
        _("Integration diagnostics operator"),
        _(
            "Tests the Ministry NIN service configuration, authentication and lookup. "
            "Platform-wide only: every event edition and every organization."
        ),
        global_only=True,
    ),
)

ROLES_BY_KEY: dict[str, StaffRole] = {role.key: role for role in STAFF_ROLES}
ROLES_BY_GROUP: dict[str, StaffRole] = {role.group_name: role for role in STAFF_ROLES}


def role_for_group_name(group_name: str) -> StaffRole | None:
    return ROLES_BY_GROUP.get(group_name)
