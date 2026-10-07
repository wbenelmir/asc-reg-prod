"""Phase 2 Prompt 4 operational group (least-privilege, mirrors
`apps.invitations.apps`/`apps.reviews.apps`'s bootstrap idiom exactly).

A NEW, distinct group -- Prompt 3's own "Accreditation Managers" group
(decision/reopen/cancellation) deliberately never receives any permission
declared here (Prompt 3 rule "Do not give these groups Prompt 4
assignment permissions").
"""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

ACCREDITATION_COORDINATORS_GROUP_NAME = "Accreditation Coordinators"

ACCREDITATION_COORDINATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("accreditation", "view_participantrole"),
    ("accreditation", "view_badgetype"),
    ("accreditation", "view_accessprofile"),
    ("accreditation", "view_accessrule"),
    ("accreditation", "view_participantroleassignment"),
    ("accreditation", "add_participantroleassignment"),
    ("accreditation", "change_participantroleassignment"),
    ("accreditation", "view_badgetypeassignment"),
    ("accreditation", "add_badgetypeassignment"),
    ("accreditation", "change_badgetypeassignment"),
    ("accreditation", "view_accessprofileassignment"),
    ("accreditation", "add_accessprofileassignment"),
    ("accreditation", "change_accessprofileassignment"),
    ("accreditation", "view_accessruleassignment"),
    ("accreditation", "add_accessruleassignment"),
    ("accreditation", "change_accessruleassignment"),
    ("accreditation", "view_bulkassignmentoperation"),
    ("accreditation", "add_bulkassignmentoperation"),
    ("registrations", "view_registration"),
    # Attendance days are read-only for coordinators (they prepare badges
    # and passes); classifying or changing them is a decision-maker action.
    ("accreditation", "view_attendanceentitlement"),
)

#: Attendance configuration (the three days, the opening-day capacity and the
#: enforcement switch) is its own capability. No existing group receives it.
ATTENDANCE_POLICY_MANAGERS_GROUP_NAME = "Attendance Policy Managers"
ATTENDANCE_POLICY_MANAGERS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("accreditation", "view_attendancepolicy"),
    ("accreditation", "manage_attendancepolicy"),
    ("accreditation", "view_attendanceentitlement"),
    ("registrations", "view_registration"),
)

#: The decision makers (`apps.reviews.apps`' Accreditation Managers, who hold
#: `reviews.add_registrationdecision`) choose the attendance days at approval,
#: so they also classify earlier approvals and change the days later. Added
#: here, after this app's own permissions exist in a fresh `migrate`.
ATTENDANCE_DECISION_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("accreditation", "view_attendancepolicy"),
    ("accreditation", "view_attendanceentitlement"),
    ("accreditation", "change_attendanceentitlement"),
)


def _add_permissions(group, permission_pairs) -> None:
    from django.contrib.auth.models import Permission

    for app_label, codename in permission_pairs:
        try:
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist:
            continue
        group.permissions.add(permission)


def _ensure_accreditation_group(sender, **kwargs):
    from django.contrib.auth.models import Group

    from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME

    group, _ = Group.objects.get_or_create(name=ACCREDITATION_COORDINATORS_GROUP_NAME)
    _add_permissions(group, ACCREDITATION_COORDINATORS_PERMISSIONS)
    group, _ = Group.objects.get_or_create(name=ATTENDANCE_POLICY_MANAGERS_GROUP_NAME)
    _add_permissions(group, ATTENDANCE_POLICY_MANAGERS_PERMISSIONS)
    group, _ = Group.objects.get_or_create(name=ACCREDITATION_MANAGERS_GROUP_NAME)
    _add_permissions(group, ATTENDANCE_DECISION_PERMISSIONS)


class AccreditationConfig(AppConfig):
    """Configurable participant-role/badge-type/access-profile/access-rule
    reference data and registration-scoped assignment history (Phase 2
    Prompt 4; TRD §13.1)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accreditation"
    label = "accreditation"

    def ready(self) -> None:
        post_migrate.connect(_ensure_accreditation_group, sender=self)
