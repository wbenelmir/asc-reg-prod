"""Phase 2 Prompt 3 operational groups (least-privilege, mirrors
`apps.invitations.apps`'s bootstrap idiom exactly).

Two distinct functional groups, never merged: `Registration Reviewers`
(scoped review/checklist/information-request casework) and `Accreditation
Managers` (scoped decision/reopen/cancellation commands). Assignment is
treated as a coordinating action and granted only to managers -- an
ordinary reviewer works the case they are assigned, never reassigns it
away. Neither group receives any Prompt 4 assignment permission, and
holding a review permission never implies protected-document access
(`apps.documents.views.document_stream` remains scoped by
`registrations.view_registration` alone, independent of these groups).

IDV-3 (amendment A-13, A13-05) maps the dedicated identity permissions onto
these two existing groups instead of granting them to every staff account:
reviewers do the document review (view, evidence, manual verification, NIN
correction, return for correction); managers additionally hold the final
rejection and the Algerian staff-assisted exception. Identity evidence
(national identity card, passport identity page) now also needs
`people.view_identity_evidence` in scope, on top of seeing the registration.
`Registration Intake` (read-only) receives no identity permission. See
`docs/security/identity_review_permissions.md`.
"""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

REGISTRATION_REVIEWERS_GROUP_NAME = "Registration Reviewers"
ACCREDITATION_MANAGERS_GROUP_NAME = "Accreditation Managers"

REGISTRATION_REVIEWERS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("reviews", "view_reviewcase"),
    ("reviews", "change_reviewcase_status"),
    ("reviews", "add_checklistresult"),
    ("reviews", "view_checklistresult"),
    ("reviews", "view_internalreviewnote"),
    ("reviews", "add_internalreviewnote"),
    ("reviews", "add_informationrequest"),
    ("reviews", "change_informationrequest"),
    ("reviews", "view_informationrequest"),
    ("reviews", "view_informationresponse"),
    ("reviews", "view_duplicatecandidates"),
    ("registrations", "view_registration"),
    # IDV-3 (amendment A-13, A13-05; docs/security/identity_review_permissions.md):
    # document review casework. No final rejection and no staff exception.
    *(
        ("people", codename)
        for codename in (
            "view_identityverification",
            "view_identity_evidence",
            "verify_identity_manually",
            "correct_identity_nin",
            "return_identity_for_correction",
        )
    ),
)

ACCREDITATION_MANAGERS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("reviews", "view_reviewcase"),
    ("reviews", "assign_reviewcase"),
    ("reviews", "view_duplicatecandidates"),
    ("reviews", "add_duplicatecandidateresolution"),
    ("reviews", "view_duplicatecandidateresolution"),
    ("reviews", "add_registrationdecision"),
    ("reviews", "view_registrationdecision"),
    ("reviews", "reopen_reviewcase"),
    ("reviews", "cancel_registration"),
    ("registrations", "view_registration"),
    # IDV-3 (A13-05): every identity capability, including the final rejection
    # and the Algerian staff-assisted exception.
    *(
        ("people", codename)
        for codename in (
            "view_identityverification",
            "view_identity_evidence",
            "verify_identity_manually",
            "correct_identity_nin",
            "return_identity_for_correction",
            "reject_identity",
            "apply_identity_exception",
        )
    ),
    # Owner decision IDV-Q3: the NIN exemption grant for one Algerian draft,
    # like the staff exception a manager-only capability.
    ("people", "grant_nin_exemption"),
)

#: The identity permissions, for the mapping test and documentation.
IDENTITY_PERMISSION_CODENAMES: tuple[str, ...] = (
    "view_identityverification",
    "view_identity_evidence",
    "verify_identity_manually",
    "correct_identity_nin",
    "return_identity_for_correction",
    "reject_identity",
    "apply_identity_exception",
)

#: The decision workbook (Excel export, import preview, all-or-nothing final
#: validation) is its own capability, ON TOP of the ordinary decision
#: permission that every row still requires in its own scope. No existing
#: group receives it and nobody is granted it automatically: an account
#: administrator grants this role explicitly, normally to an Accreditation
#: Manager, with the same event/organization scope
#: (docs/operations/review_decision_workbook.md).
REVIEW_DECISION_WORKBOOK_GROUP_NAME = "Review Decision Workbook Operators"
REVIEW_DECISION_WORKBOOK_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("reviews", "bulk_registrationdecision"),
)

_ALL_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (REGISTRATION_REVIEWERS_GROUP_NAME, REGISTRATION_REVIEWERS_PERMISSIONS),
    (ACCREDITATION_MANAGERS_GROUP_NAME, ACCREDITATION_MANAGERS_PERMISSIONS),
    (REVIEW_DECISION_WORKBOOK_GROUP_NAME, REVIEW_DECISION_WORKBOOK_PERMISSIONS),
)


def _ensure_review_groups(sender, **kwargs):
    """Idempotently ensure Phase 2 Prompt 3's operational groups exist.

    Same established `post_migrate` idiom as
    `apps.invitations.apps._ensure_invitation_groups`. Only ADDS
    permissions listed above; never removes a permission an administrator
    may have separately granted.
    """
    from django.contrib.auth.models import Group, Permission

    for group_name, permission_pairs in _ALL_GROUPS:
        group, _ = Group.objects.get_or_create(name=group_name)
        for app_label, codename in permission_pairs:
            try:
                permission = Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            except Permission.DoesNotExist:
                continue
            group.permissions.add(permission)


class ReviewsConfig(AppConfig):
    """Operations Back Office review, additional-information requests,
    duplicate-candidate review, and decision history (Phase 2 Prompt 3;
    TRD §13.1 names `reviews` as a distinct later-phase application)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reviews"
    label = "reviews"

    def ready(self) -> None:
        post_migrate.connect(_ensure_review_groups, sender=self)
