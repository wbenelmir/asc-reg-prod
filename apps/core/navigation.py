"""Operations section navigation (UI/UX Completion Gate, Checkpoint 1).

A read-only presentation selector: it lists the back-office areas a signed-in
operational user may open, so the operations shell shows only those links.

This module never authorizes anything. Each entry names the exact permission
its view's `operational_permission_required` decorator enforces, and
visibility uses the same unscoped `ScopedGroupMembership` rule as that
decorator (`scoped_permission_codenames` without a scope). A hidden link
only means "this area would refuse you"; the view still enforces its own
permission and scope on every request (`apps.accounts.policies` module
docstring: authorization is never decided by navigation structure).

Event-scoped areas (badge stock, entry devices, observability) need an event
in their URL and are reached from their own pages, so they are not listed.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class OperationsSection:
    key: str
    label: Promise
    url_name: str
    icon: str
    permission: str
    #: Resolver namespaces / `namespace:name` values that mark this section
    #: current, so detail pages highlight their parent area.
    matches: tuple[str, ...]


OPERATIONS_SECTIONS: tuple[OperationsSection, ...] = (
    OperationsSection(
        key="registrations",
        label=_("Registrations"),
        url_name="registrations:ops-intake-list",
        icon="list",
        permission="registrations.view_registration",
        matches=(
            "registrations:ops-intake-list",
            "registrations:ops-intake-detail",
            "accreditation:registration-detail",
        ),
    ),
    OperationsSection(
        key="reviews",
        label=_("Reviews"),
        url_name="reviews:queue-list",
        icon="user-check",
        permission="reviews.view_reviewcase",
        matches=("reviews",),
    ),
    OperationsSection(
        key="identity",
        label=_("Identity review"),
        url_name="identity:queue",
        icon="id-card",
        permission="people.view_identityverification",
        matches=("identity",),
    ),
    OperationsSection(
        key="organizations",
        label=_("Organizations"),
        url_name="invitations:workspace-dashboard",
        icon="building",
        permission="invitations.view_invitationcampaign",
        matches=("invitations",),
    ),
    OperationsSection(
        key="accreditation",
        label=_("Bulk accreditation"),
        url_name="accreditation:bulk-preview",
        icon="layers",
        permission="accreditation.add_bulkassignmentoperation",
        matches=("accreditation:bulk-preview", "accreditation:bulk-execute"),
    ),
    OperationsSection(
        key="attendance",
        label=_("Attendance days"),
        url_name="accreditation:attendance-overview",
        icon="clock",
        permission="accreditation.view_attendanceentitlement",
        matches=(
            "accreditation:attendance-overview",
            "accreditation:attendance-policy",
            "accreditation:attendance-worklist",
        ),
    ),
    OperationsSection(
        key="passes",
        label=_("Pass lookup"),
        url_name="badges:fallback-reference-lookup",
        icon="search",
        permission="badges.view_digitalentrypass",
        matches=(
            "badges:fallback-reference-lookup",
            "badges:registration-credential",
            "badges:registration-badge-issuance",
        ),
    ),
    OperationsSection(
        key="keys",
        label=_("Verification keys"),
        url_name="badges:verification-keys",
        icon="shield",
        permission="badges.manage_verificationkey",
        matches=("badges:verification-keys",),
    ),
    OperationsSection(
        key="accommodation",
        label=_("Accessibility support"),
        url_name="registrations:ops-accommodation-list",
        icon="user-check",
        permission="registrations.coordinate_accommodation_support",
        matches=("registrations:ops-accommodation-list",),
    ),
    OperationsSection(
        key="registration-channels",
        label=_("Registration channels"),
        url_name="events:channel-list",
        icon="gate",
        permission="events.manage_registration_channels",
        matches=("events",),
    ),
    OperationsSection(
        key="communications",
        label=_("Communications"),
        url_name="communications:operations-messages",
        icon="mail",
        permission="communications.view_communicationmessage",
        matches=("communications",),
    ),
    OperationsSection(
        key="staff-accounts",
        label=_("Staff accounts"),
        url_name="staff_accounts:list",
        icon="user",
        permission="accounts.manage_operational_accounts",
        matches=("staff_accounts",),
    ),
    OperationsSection(
        key="integrations",
        label=_("Ministry NIN service"),
        url_name="identity:nin-diagnostics",
        icon="online",
        permission="people.run_ministry_nin_diagnostics",
        matches=(
            "identity:nin-diagnostics",
            "identity:nin-diagnostics-authentication",
            "identity:nin-diagnostics-lookup",
        ),
    ),
    OperationsSection(
        key="exports",
        label=_("Exports"),
        url_name="exports:workspace",
        icon="download",
        permission="exports.add_exportrequest",
        matches=("exports",),
    ),
)


@dataclass(frozen=True)
class NavigationItem:
    key: str
    label: str
    url: str
    icon: str
    current: bool


def _is_current(section: OperationsSection, view_name: str, namespace: str) -> bool:
    return any(match in (view_name, namespace) for match in section.matches)


def operations_navigation(
    user, *, view_name: str = "", namespace: str = ""
) -> list[NavigationItem]:
    """Return the sections `user` may open, in display order.

    One permission query per call. Anonymous or inactive users get an empty
    list, as `scoped_permission_codenames` returns no codename for them.
    """
    from apps.accounts.policies import scoped_permission_codenames

    granted = scoped_permission_codenames(user)
    return _items_for(granted, view_name=view_name, namespace=namespace)


def operations_home_url(user, items: list[NavigationItem] | None = None) -> str:
    """The first area `user` may open, or the intake list when there is none.

    Used for the shell's brand link and as the default after operational
    sign-in. A user with no area at all reaches the intake list, which
    answers with the 403 page (never a redirect back to sign-in, F3).
    """
    if items is None:
        items = operations_navigation(user)
    return items[0].url if items else reverse("registrations:ops-intake-list")


def _items_for(granted: set[str], *, view_name: str, namespace: str) -> list[NavigationItem]:
    return [
        NavigationItem(
            key=section.key,
            label=str(section.label),
            url=reverse(section.url_name),
            icon=section.icon,
            current=_is_current(section, view_name, namespace),
        )
        for section in OPERATIONS_SECTIONS
        if section.permission in granted
    ]
