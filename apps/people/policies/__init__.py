"""Identity verification authorization policies (IDV-3, amendment A-13, A13-05).

Every identity command checks its own dedicated permission against the exact
scope of the case it acts on, inside the service, so a hidden button, a
forged POST or a direct service call is refused the same way. A permission
held through one membership never combines with the scope of another
(`apps.accounts.selectors.scope_filtered_queryset`).

Permissions (`docs/security/identity_review_permissions.md`):

* `people.view_identityverification` -- queue and review screen, masked NIN;
* `people.view_identity_evidence` -- evidence previews and the full NIN;
* `people.verify_identity_manually`;
* `people.correct_identity_nin` -- NIN correction and recheck;
* `people.return_identity_for_correction`;
* `people.reject_identity` -- the final rejection;
* `people.apply_identity_exception` -- the Algerian staff-assisted exception;
* `people.grant_nin_exemption` -- grant or revoke the NIN exemption route for
  one Algerian draft without a usable NIN (owner decision IDV-Q3), checked in
  the Registration's exact scope.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied

IDENTITY_CODENAMES = (
    "view_identityverification",
    "view_identity_evidence",
    "verify_identity_manually",
    "correct_identity_nin",
    "return_identity_for_correction",
    "reject_identity",
    "apply_identity_exception",
)


class IdentityPermissionDenied(PermissionDenied):
    """The actor lacks the action's permission in the case's scope."""


def identity_verifications_visible_to(user, *, codename: str = "view_identityverification"):
    """The identity cases `user` holds `people.<codename>` for, by exact scope."""
    from apps.accounts.selectors import scope_filtered_queryset
    from apps.people.models import IdentityVerification

    return scope_filtered_queryset(
        user,
        IdentityVerification.objects,
        app_label="people",
        codename=codename,
        organization_field="organization_id",
    )


def can_act(user, codename: str, verification) -> bool:
    if codename not in IDENTITY_CODENAMES:
        return False
    return (
        identity_verifications_visible_to(user, codename=codename)
        .filter(pk=verification.pk)
        .exists()
    )


def require_identity_permission(user, codename: str, verification) -> None:
    if not can_act(user, codename, verification):
        raise IdentityPermissionDenied(codename)


def registrations_for_nin_exemption(user):
    """Registrations `user` may grant or revoke a NIN exemption for (IDV-Q3):
    `people.grant_nin_exemption` in the registration's exact scope. The grant
    exists before any identity case, so the scope is the Registration's."""
    from apps.accounts.selectors import scope_filtered_queryset
    from apps.registrations.models import Registration

    return scope_filtered_queryset(
        user,
        Registration.objects,
        app_label="people",
        codename="grant_nin_exemption",
        organization_field="source_organization_id",
    )


def can_grant_nin_exemption(user, registration) -> bool:
    return registrations_for_nin_exemption(user).filter(pk=registration.pk).exists()


def require_nin_exemption_permission(user, registration) -> None:
    if not can_grant_nin_exemption(user, registration):
        raise IdentityPermissionDenied("grant_nin_exemption")


def granted_identity_codenames(user, verification) -> set[str]:
    """Every identity codename `user` may use on this case (batched for the
    review screen; still exact-scope per codename)."""
    return {codename for codename in IDENTITY_CODENAMES if can_act(user, codename, verification)}
