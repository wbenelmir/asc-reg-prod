"""Accreditation assignment views (Phase 2 Prompt 4). Views only coordinate
HTTP -- every business transition lives in `apps.accreditation.services`.

Every mutation view resolves its TARGET via a selector scoped by the EXACT
action permission it requires (never the view-only default), mirroring
`apps.invitations`/`apps.reviews`'s "action-specific object scope on every
mutation" pattern -- write permission in one scope can never combine with
view permission in another to authorize a mutation.
"""

from __future__ import annotations

import secrets
import uuid

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.policies import (
    deny_operational_access,
    has_scoped_permission,
    operational_permission_required,
    scoped_permission_codenames,
)
from apps.accounts.selectors import registrations_visible_to

from .forms import BulkPreviewForm
from .models import (
    AccessProfile,
    AccessProfileAssignment,
    AccessRule,
    AccessRuleAssignment,
    BadgeType,
    BulkAssignmentKind,
    ParticipantRole,
    ParticipantRoleAssignment,
)
from .selectors import (
    access_profile_assignments_visible_to,
    access_rule_assignments_visible_to,
    badge_assignments_visible_to,
    bulk_assignable_references,
    current_access_profile_assignments,
    current_access_rule_assignments,
    current_badge_assignment,
    current_role_assignments,
    registration_references,
    registrations_visible_for_accreditation_action,
    role_assignments_visible_to,
)
from .services import (
    AssignmentAlreadyCurrentError,
    AssignmentNotFoundError,
    InvalidEffectiveRangeError,
    InvalidReferenceError,
    StaleVersionError,
    assign,
    change,
    execute_bulk_assignment,
    preview_bulk_assignment,
    revoke,
)

_KIND_REFERENCE_MODEL = {
    BulkAssignmentKind.PARTICIPANT_ROLE: ParticipantRole,
    BulkAssignmentKind.BADGE_TYPE: BadgeType,
    BulkAssignmentKind.ACCESS_PROFILE: AccessProfile,
    BulkAssignmentKind.ACCESS_RULE: AccessRule,
}
_KIND_ADD_CODENAME = {
    BulkAssignmentKind.PARTICIPANT_ROLE: "add_participantroleassignment",
    BulkAssignmentKind.BADGE_TYPE: "add_badgetypeassignment",
    BulkAssignmentKind.ACCESS_PROFILE: "add_accessprofileassignment",
    BulkAssignmentKind.ACCESS_RULE: "add_accessruleassignment",
}
_KIND_CHANGE_CODENAME = {
    BulkAssignmentKind.PARTICIPANT_ROLE: "change_participantroleassignment",
    BulkAssignmentKind.BADGE_TYPE: "change_badgetypeassignment",
    BulkAssignmentKind.ACCESS_PROFILE: "change_accessprofileassignment",
    BulkAssignmentKind.ACCESS_RULE: "change_accessruleassignment",
}


def _active_reference_or_404(kind: str, raw_id, event_edition_id):
    """The active reference object of `kind` in the registration's own event.
    A malformed, unknown, inactive or other-event id is one indistinguishable
    404 (UI/UX Completion Gate F8: forged identifiers are rejected, never a
    server error)."""
    try:
        reference_id = uuid.UUID(str(raw_id))
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        _KIND_REFERENCE_MODEL[kind],
        pk=reference_id,
        event_edition_id=event_edition_id,
        is_active=True,
    )


def _conflict_response(request, message: str) -> HttpResponse:
    messages.error(request, message)
    match = getattr(request, "resolver_match", None)
    registration_pk = match.kwargs.get("pk") if match is not None else None
    return render(
        request,
        "accreditation/conflict.html",
        {"message": message, "registration_pk": registration_pk},
        status=409,
    )


@operational_permission_required(
    "registrations.view_registration", login_url="accounts:operational-sign-in"
)
def registration_accreditation_detail(request, pk):
    """Read-only accreditation summary for one Registration Context --
    current role/badge/access-profile/access-rule assignments, plus
    authorization-aware assign/revoke controls."""
    registration = get_object_or_404(registrations_visible_to(request.user), pk=pk)
    scope = {
        "event_edition_id": registration.event_edition_id,
        "organization_id": registration.source_organization_id,
    }
    permissions = scoped_permission_codenames(request.user, **scope)
    permission_flags = {
        "role": "accreditation.view_participantroleassignment" in permissions,
        "badge": "accreditation.view_badgetypeassignment" in permissions,
        "profile": "accreditation.view_accessprofileassignment" in permissions,
        "rule": "accreditation.view_accessruleassignment" in permissions,
    }
    if not any(permission_flags.values()):
        raise Http404
    can_assign_role = "accreditation.add_participantroleassignment" in permissions
    can_assign_badge = "accreditation.add_badgetypeassignment" in permissions
    can_assign_profile = "accreditation.add_accessprofileassignment" in permissions
    can_assign_rule = "accreditation.add_accessruleassignment" in permissions
    can_change_role = "accreditation.change_participantroleassignment" in permissions
    can_change_badge = "accreditation.change_badgetypeassignment" in permissions
    can_change_profile = "accreditation.change_accessprofileassignment" in permissions
    can_change_rule = "accreditation.change_accessruleassignment" in permissions
    context = {
        "registration": registration,
        "can_view_role": permission_flags["role"],
        "can_view_badge": permission_flags["badge"],
        "can_view_profile": permission_flags["profile"],
        "can_view_rule": permission_flags["rule"],
        "role_assignments": current_role_assignments(registration)
        if permission_flags["role"]
        else ParticipantRoleAssignment.objects.none(),
        "badge_assignment": current_badge_assignment(registration)
        if permission_flags["badge"]
        else None,
        "access_profile_assignments": current_access_profile_assignments(registration)
        if permission_flags["profile"]
        else AccessProfileAssignment.objects.none(),
        "access_rule_assignments": current_access_rule_assignments(registration)
        if permission_flags["rule"]
        else AccessRuleAssignment.objects.none(),
        "can_assign_role": can_assign_role,
        "can_assign_badge": can_assign_badge,
        "can_assign_profile": can_assign_profile,
        "can_assign_rule": can_assign_rule,
        "can_change_role": can_change_role,
        "can_change_badge": can_change_badge,
        "can_change_profile": can_change_profile,
        "can_change_rule": can_change_rule,
        "available_roles": ParticipantRole.objects.filter(
            event_edition_id=registration.event_edition_id, is_active=True
        )
        if can_assign_role or can_change_role
        else ParticipantRole.objects.none(),
        "available_badge_types": BadgeType.objects.filter(
            event_edition_id=registration.event_edition_id, is_active=True
        )
        if can_assign_badge or can_change_badge
        else BadgeType.objects.none(),
        "available_access_profiles": AccessProfile.objects.filter(
            event_edition_id=registration.event_edition_id, is_active=True
        )
        if can_assign_profile or can_change_profile
        else AccessProfile.objects.none(),
        "available_access_rules": AccessRule.objects.filter(
            event_edition_id=registration.event_edition_id, is_active=True
        )
        if can_assign_rule or can_change_rule
        else AccessRule.objects.none(),
    }
    return render(request, "accreditation/registration_detail.html", context)


@require_http_methods(["POST"])
def assignment_assign(request, pk, kind):
    """Assign one reference object to `pk` (a Registration). `kind` is one
    of the `BulkAssignmentKind` values, matched against the URL. The
    caller MUST hold the exact `add_*` permission for THIS assignment
    kind, scoped to the registration's OWN event/organization -- a view
    permission, or a write permission scoped to a DIFFERENT registration's
    event/organization, is never sufficient (Phase 2 Prompt 4 §3)."""
    if kind not in _KIND_ADD_CODENAME:
        # P4-4: a generic 404 before any authorization check or state change,
        # as in `assignment_change`; this used to store a flash message for
        # anyone, signed in or not, and redirect.
        raise Http404
    codename = _KIND_ADD_CODENAME[kind]
    if not has_scoped_permission(request.user, f"accreditation.{codename}"):
        return deny_operational_access(request)

    registration = get_object_or_404(
        registrations_visible_for_accreditation_action(request.user, codename=codename), pk=pk
    )
    if not has_scoped_permission(
        request.user,
        f"accreditation.{codename}",
        event_edition_id=registration.event_edition_id,
        organization_id=registration.source_organization_id,
    ):
        raise Http404

    reference_obj = _active_reference_or_404(
        kind, request.POST.get("reference_object_id"), registration.event_edition_id
    )
    try:
        assign(
            kind=kind,
            registration=registration,
            reference_obj=reference_obj,
            actor=request.user,
            reason=request.POST.get("reason", ""),
        )
    except AssignmentAlreadyCurrentError:
        messages.error(request, _("A current assignment already exists for this scope."))
    except InvalidEffectiveRangeError:
        messages.error(request, _("The effective date range is invalid."))
    except InvalidReferenceError:
        messages.error(request, _("The effective date range is invalid."))
    return redirect("accreditation:registration-detail", pk=registration.pk)


@require_http_methods(["POST"])
def assignment_revoke(request, pk, kind, assignment_pk):
    if kind not in _KIND_CHANGE_CODENAME:
        # P4-4: a generic 404 before any authorization check or state change,
        # as in `assignment_change`; this used to store a flash message for
        # anyone, signed in or not, and redirect.
        raise Http404
    codename = _KIND_CHANGE_CODENAME[kind]
    if not has_scoped_permission(request.user, f"accreditation.{codename}"):
        return deny_operational_access(request)

    registration = get_object_or_404(
        registrations_visible_for_accreditation_action(request.user, codename=codename), pk=pk
    )
    if not has_scoped_permission(
        request.user,
        f"accreditation.{codename}",
        event_edition_id=registration.event_edition_id,
        organization_id=registration.source_organization_id,
    ):
        raise Http404
    assignment_selector = {
        BulkAssignmentKind.PARTICIPANT_ROLE: role_assignments_visible_to,
        BulkAssignmentKind.BADGE_TYPE: badge_assignments_visible_to,
        BulkAssignmentKind.ACCESS_PROFILE: access_profile_assignments_visible_to,
        BulkAssignmentKind.ACCESS_RULE: access_rule_assignments_visible_to,
    }[kind]
    assignment = get_object_or_404(
        assignment_selector(request.user, codename=codename),
        pk=assignment_pk,
        registration=registration,
    )
    reason = request.POST.get("reason", "")
    if not reason:
        messages.error(request, _("Revocation requires a reason."))
        return redirect("accreditation:registration-detail", pk=registration.pk)
    try:
        revoke(
            kind=kind,
            current_assignment=assignment,
            expected_version=int(request.POST.get("expected_version", assignment.version)),
            actor=request.user,
            reason=reason,
        )
    except StaleVersionError:
        return _conflict_response(
            request, _("This assignment changed since you loaded it. Reload and retry.")
        )
    except AssignmentNotFoundError:
        messages.error(request, _("This assignment is no longer active."))
    return redirect("accreditation:registration-detail", pk=registration.pk)


@require_http_methods(["POST"])
def assignment_change(request, pk, kind, assignment_pk):
    if kind not in _KIND_CHANGE_CODENAME:
        raise Http404
    codename = _KIND_CHANGE_CODENAME[kind]
    registration = get_object_or_404(
        registrations_visible_for_accreditation_action(request.user, codename=codename), pk=pk
    )
    assignment_selector = {
        BulkAssignmentKind.PARTICIPANT_ROLE: role_assignments_visible_to,
        BulkAssignmentKind.BADGE_TYPE: badge_assignments_visible_to,
        BulkAssignmentKind.ACCESS_PROFILE: access_profile_assignments_visible_to,
        BulkAssignmentKind.ACCESS_RULE: access_rule_assignments_visible_to,
    }[kind]
    assignment = get_object_or_404(
        assignment_selector(request.user, codename=codename),
        pk=assignment_pk,
        registration=registration,
    )
    new_reference = _active_reference_or_404(
        kind, request.POST.get("reference_object_id"), registration.event_edition_id
    )
    try:
        change(
            kind=kind,
            current_assignment=assignment,
            new_reference_obj=new_reference,
            expected_version=int(request.POST.get("expected_version", assignment.version)),
            actor=request.user,
            reason=request.POST.get("reason", ""),
        )
    except StaleVersionError:
        return _conflict_response(
            request, _("This assignment changed since you loaded it. Reload and retry.")
        )
    except (AssignmentNotFoundError, AssignmentAlreadyCurrentError, InvalidReferenceError) as exc:
        return _conflict_response(request, str(exc))
    return redirect("accreditation:registration-detail", pk=registration.pk)


# ---------------------------------------------------------------------------
# Bulk workflow
# ---------------------------------------------------------------------------


@operational_permission_required(
    "accreditation.add_bulkassignmentoperation", login_url="accounts:operational-sign-in"
)
def bulk_preview(request):
    """Step 1-4 of the bulk workflow: scoped target selection + preview.
    Targets are restricted to the caller's OWN scope regardless of what is
    posted -- never a raw, unchecked id list (Phase 2 Prompt 4 §5). The
    item to assign is chosen from the objects the caller may bulk-assign
    (UI/UX Completion Gate F8), and the chosen object is still re-checked
    against the caller's scoped permission before any preview is created."""
    token = request.session.get("accreditation_bulk_preview_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["accreditation_bulk_preview_token"] = token
    references_by_kind = {
        kind: list(
            bulk_assignable_references(
                request.user,
                model=_KIND_REFERENCE_MODEL[kind],
                codename=_KIND_ADD_CODENAME[kind],
            )
        )
        for kind in _KIND_REFERENCE_MODEL
    }
    form = BulkPreviewForm(
        request.POST if request.method == "POST" else None,
        references_by_kind=references_by_kind,
    )
    context = {
        "form": form,
        "idempotency_key": token,
        "has_references": any(references_by_kind.values()),
    }
    if request.method != "POST" or not form.is_valid():
        return render(request, "accreditation/bulk_preview.html", context)

    kind = form.cleaned_data["kind"]
    reference_obj = form.cleaned_data["reference_obj"]
    add_codename = _KIND_ADD_CODENAME[kind]
    if not has_scoped_permission(
        request.user,
        f"accreditation.{add_codename}",
        event_edition_id=reference_obj.event_edition_id,
    ):
        messages.error(request, _("You are not authorized to bulk-assign this."))
        return render(request, "accreditation/bulk_preview.html", context)

    scoped_registration_ids = list(
        registrations_visible_for_accreditation_action(request.user, codename=add_codename)
        .filter(event_edition_id=reference_obj.event_edition_id, public_status="APPROVED")
        .values_list("pk", flat=True)
    )
    operation = preview_bulk_assignment(
        kind=kind,
        registration_ids=scoped_registration_ids,
        reference_obj=reference_obj,
        requested_by=request.user,
        reason=form.cleaned_data.get("reason", ""),
        idempotency_key=request.POST.get("idempotency_key") or token,
    )
    request.session["accreditation_bulk_preview_token"] = secrets.token_urlsafe(32)
    context.update(
        {
            "operation": operation,
            "reference_obj": reference_obj,
            "kind": kind,
            "kind_label": BulkAssignmentKind(kind).label,
            "references": registration_references(
                row["registration_id"] for row in operation.preview_results
            ),
            "eligible_count": len(operation.target_registration_ids),
        }
    )
    return render(request, "accreditation/bulk_preview.html", context)


@operational_permission_required(
    "accreditation.add_bulkassignmentoperation", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def bulk_execute(request, pk):
    from .models import BulkAssignmentOperation

    operation = get_object_or_404(BulkAssignmentOperation, pk=pk)
    add_codename = _KIND_ADD_CODENAME[operation.kind]
    if not has_scoped_permission(
        request.user, f"accreditation.{add_codename}", event_edition_id=operation.event_edition_id
    ):
        messages.error(request, _("You are not authorized to execute this bulk operation."))
        return redirect("accreditation:bulk-preview")
    authorized_target_ids = {
        str(value)
        for value in registrations_visible_for_accreditation_action(
            request.user, codename=add_codename
        )
        .filter(pk__in=operation.target_registration_ids)
        .values_list("pk", flat=True)
    }
    if authorized_target_ids != set(operation.target_registration_ids):
        raise Http404
    executed = execute_bulk_assignment(operation=operation, actor=request.user)
    rows = executed.execution_results or []
    return render(
        request,
        "accreditation/bulk_result.html",
        {
            "operation": executed,
            "kind_label": BulkAssignmentKind(executed.kind).label,
            "references": registration_references(row["registration_id"] for row in rows),
            "success_count": sum(1 for row in rows if row.get("success")),
            "failure_count": sum(1 for row in rows if not row.get("success")),
        },
    )
