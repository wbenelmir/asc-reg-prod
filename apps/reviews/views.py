"""Operations review views (Phase 2 Prompt 3 §6, §14) and the participant
response view. Views only coordinate HTTP -- every business transition
lives in `apps.reviews.services`.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts import participant_auth
from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.registrations.models import Registration

from .forms import (
    ApprovedDecisionForm,
    AssignmentForm,
    ChecklistResultForm,
    DuplicateResolutionForm,
    InformationRequestForm,
    InternalNoteForm,
    NotApprovedDecisionForm,
    ReasonForm,
    RequestItemFormSet,
)
from .models import (
    DuplicateOutcome,
    InformationRequest,
    InformationRequestStatus,
    ReviewCase,
    ReviewCaseStatus,
)
from .selectors import (
    checklist_results_for,
    decisions_visible_to,
    duplicate_candidates_for,
    duplicate_resolutions_visible_to,
    eligible_review_assignees,
    information_requests_visible_to,
    review_cases_visible_to,
)
from .services import (
    ActiveInformationRequestExistsError,
    ApprovalRequiresAssignmentsError,
    ApprovalRequiresVerifiedIdentityError,
    InvalidInformationResponseError,
    InvalidStateTransitionError,
    StaleVersionError,
    add_internal_note,
    assign_review_case,
    cancel_information_request,
    cancel_registration_operationally,
    change_review_case_status,
    close_information_request,
    create_information_request,
    record_approved_decision,
    record_not_approved_decision,
    reopen_registration,
    resolve_duplicate_candidate,
    send_information_request,
)

PAGE_SIZE = 25


def _conflict_response(request, message: str) -> HttpResponse:
    messages.error(request, message)
    # Participant commands (the `my-*` routes) answer in the participant
    # workspace shell; every staff command in the operations shell.
    url_name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    participant = url_name.startswith("my-")
    context = {
        "message": message,
        "participant_conflict": participant,
        "conflict_layout": (
            "layouts/base_workspace.html" if participant else "layouts/base_operations.html"
        ),
    }
    return render(request, "reviews/conflict.html", context, status=409)


def _get_scoped_case_or_404(request, pk, *, codename: str = "view_reviewcase") -> ReviewCase:
    return get_object_or_404(
        review_cases_visible_to(request.user, codename=codename).select_related(
            "registration", "registration__event_edition"
        ),
        pk=pk,
    )


def _get_scoped_registration_or_404(request, pk, *, codename: str) -> Registration:
    from apps.accounts.selectors import scope_filtered_queryset

    queryset = scope_filtered_queryset(
        request.user,
        Registration.objects,
        app_label="reviews",
        codename=codename,
        organization_field="source_organization_id",
    )
    return get_object_or_404(queryset, pk=pk)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@operational_permission_required(
    "reviews.view_reviewcase", login_url="accounts:operational-sign-in"
)
def queue_list(request):
    """Scoped, paginated, filtered review-case queue (Phase 2 Prompt 3 §6).

    Every filter is bounded and applied at the queryset level; the search
    box requires a minimum length and matches only the Registration
    Reference or a name field, never an unbounded table scan.
    """
    queryset = (
        review_cases_visible_to(request.user)
        .select_related("registration", "registration__profile", "event_edition", "organization")
        .prefetch_related("assignments")
        .order_by("-priority", "opened_at")
    )

    query = request.GET.get("q", "").strip()
    if len(query) >= 2:
        from django.db.models import Q

        queryset = queryset.filter(
            Q(registration__public_reference__istartswith=query)
            | Q(registration__profile__submitted_given_names__istartswith=query)
            | Q(registration__profile__submitted_family_name__istartswith=query)
        )
    case_type = request.GET.get("case_type", "")
    if case_type:
        queryset = queryset.filter(case_type=case_type)
    queue_code = request.GET.get("queue_code", "")
    if queue_code:
        queryset = queryset.filter(queue_code=queue_code)
    status = request.GET.get("status", "")
    if status:
        queryset = queryset.filter(status=status)
    public_status = request.GET.get("public_status", "")
    if public_status:
        queryset = queryset.filter(registration__public_status=public_status)
    internal_status = request.GET.get("internal_status", "")
    if internal_status:
        queryset = queryset.filter(registration__internal_status=internal_status)
    organization_id = request.GET.get("organization", "")
    if organization_id:
        queryset = queryset.filter(organization_id=organization_id)
    assignment = request.GET.get("assignment", "")
    if assignment == "unassigned":
        queryset = queryset.exclude(assignments__is_current=True)
    elif assignment == "mine":
        queryset = queryset.filter(
            assignments__is_current=True, assignments__assigned_user=request.user
        )

    page_number = request.GET.get("page", 1)
    paginator = Paginator(queryset, PAGE_SIZE)
    page = paginator.get_page(page_number)

    querystring = request.GET.copy()
    querystring.pop("page", None)

    return render(
        request,
        "reviews/queue_list.html",
        {
            "page": page,
            "querystring": querystring.urlencode(),
            "case_types": ReviewCase._meta.get_field("case_type").choices,
            "queue_codes": ReviewCase._meta.get_field("queue_code").choices,
            "statuses": ReviewCaseStatus.choices,
        },
    )


# ---------------------------------------------------------------------------
# Case detail
# ---------------------------------------------------------------------------


@operational_permission_required(
    "reviews.view_reviewcase", login_url="accounts:operational-sign-in"
)
def case_detail(request, pk):
    case = _get_scoped_case_or_404(request, pk)
    registration = case.registration
    scope = {"event_edition_id": case.event_edition_id, "organization_id": case.organization_id}
    can_view_checklist = has_scoped_permission(
        request.user, "reviews.view_checklistresult", **scope
    )
    can_view_notes = has_scoped_permission(request.user, "reviews.view_internalreviewnote", **scope)
    can_view_duplicates = has_scoped_permission(
        request.user, "reviews.view_duplicatecandidates", **scope
    )
    current_assignment = (
        case.assignments.filter(is_current=True).select_related("assigned_user").first()
    )
    duplicate_candidates = (
        list(duplicate_candidates_for(case))
        if case.case_type == "DUPLICATE" and can_view_duplicates
        else []
    )
    duplicate_resolutions = (
        duplicate_resolutions_visible_to(request.user).filter(review_case=case)
        if can_view_duplicates
        else []
    )
    information_requests = information_requests_visible_to(request.user).filter(
        registration=registration
    )
    decisions = (
        decisions_visible_to(request.user).filter(registration=registration).order_by("-sequence")
    )
    context = {
        "case": case,
        "registration": registration,
        "current_assignment": current_assignment,
        "checklist_results": checklist_results_for(case) if can_view_checklist else [],
        "internal_notes": (
            case.internal_notes.select_related("created_by").order_by("-created_at")
            if can_view_notes
            else []
        ),
        "duplicate_candidates": duplicate_candidates,
        "duplicate_resolutions": duplicate_resolutions,
        "information_requests": information_requests,
        "decisions": decisions,
        "can_assign": has_scoped_permission(request.user, "reviews.assign_reviewcase", **scope),
        "can_change_status": has_scoped_permission(
            request.user, "reviews.change_reviewcase_status", **scope
        ),
        "can_add_checklist": has_scoped_permission(
            request.user, "reviews.add_checklistresult", **scope
        ),
        "can_view_checklist": can_view_checklist,
        "can_view_notes": can_view_notes,
        "can_add_notes": has_scoped_permission(
            request.user, "reviews.add_internalreviewnote", **scope
        ),
        "can_view_duplicates": can_view_duplicates,
        "can_resolve_duplicates": has_scoped_permission(
            request.user, "reviews.add_duplicatecandidateresolution", **scope
        ),
        "can_create_request": has_scoped_permission(
            request.user, "reviews.add_informationrequest", **scope
        ),
        "can_change_request": has_scoped_permission(
            request.user, "reviews.change_informationrequest", **scope
        ),
        "can_decide": has_scoped_permission(
            request.user, "reviews.add_registrationdecision", **scope
        ),
        "can_reopen": has_scoped_permission(request.user, "reviews.reopen_reviewcase", **scope),
        "can_cancel": has_scoped_permission(request.user, "reviews.cancel_registration", **scope),
        "duplicate_outcomes": DuplicateOutcome.choices,
    }
    # IDV-3 (IDV-10): the identity status is shown beside the participation
    # case for readers who may see it; the two decisions stay separate.
    from apps.people.policies import identity_verifications_visible_to

    context["identity_case"] = (
        identity_verifications_visible_to(request.user).filter(registration=registration).first()
    )
    # IDV-Q1: whether approval is possible on identity grounds, shown to
    # deciders as a coded, staff-only explanation (the service decides).
    if context["can_decide"]:
        from apps.people.selectors.clearance import identity_clearance

        context["identity_clearance"] = identity_clearance(registration.pk)
        context["attendance_choice"] = _attendance_choice_context(registration)
    # The current attendance days of an approved registration, beside its
    # status (the decision panel above chooses them for a new approval).
    from apps.accreditation import attendance

    context["attendance_entitlement"] = attendance.current_entitlement(registration.pk)
    if context["can_assign"]:
        context["assignment_form"] = AssignmentForm(
            assignees=eligible_review_assignees(case),
            initial={"assigned_user_id": getattr(current_assignment, "assigned_user_id", None)},
            auto_id="id_assign_%s",
        )
    if context["can_resolve_duplicates"] and case.case_type == "DUPLICATE":
        context["duplicate_form"] = DuplicateResolutionForm(
            outcome_choices=DuplicateOutcome.choices,
            candidates=duplicate_candidates_for(case),
            auto_id="id_dup_%s",
        )
    if context["can_add_checklist"]:
        context["checklist_item_choices"] = _checklist_item_choices(case)
    return render(request, "reviews/case_detail.html", context)


def _attendance_choice_context(registration) -> dict:
    """What the approval panel needs to explain both attendance choices with
    the real dates and the opening-day places left. Display only: the
    service re-checks everything under the policy lock."""
    from apps.accreditation import attendance

    policy = attendance.policy_for(registration.event_edition_id)
    if not attendance.is_configured(policy):
        return {"configured": False}
    counts = attendance.attendance_counts(registration.event_edition_id, policy)
    opening, second, third = (
        attendance.format_day(day) for day in attendance.conference_days(policy)
    )
    return {
        "configured": True,
        "opening_day": opening,
        "second_day": second,
        "third_day": third,
        "capacity": policy.opening_day_capacity,
        "allocated": counts.all_days,
        "remaining": counts.opening_remaining,
    }


def _checklist_item_choices(case) -> list[tuple[str, str]]:
    """The active checklist's own item codes for this case, with their
    translated labels (the free-text "Item code" box is gone; the service
    still rejects any code outside the active checklist)."""
    from apps.core.display_labels import code_choices

    from .models import ChecklistDefinition

    definition = (
        ChecklistDefinition.objects.filter(
            event_edition=case.event_edition, case_type=case.case_type, is_active=True
        )
        .order_by("-created_at")
        .first()
    )
    if definition is None:
        return []
    return code_choices("checklist_item", [item.get("code") for item in definition.items])


@operational_permission_required(
    "reviews.assign_reviewcase", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def case_assign(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="assign_reviewcase")
    # Only people who may open this case are valid choices (F8): a forged,
    # stale or out-of-scope user id fails validation exactly like a missing
    # one, so the response never tells whether that id exists.
    form = AssignmentForm(request.POST, assignees=eligible_review_assignees(case))
    if not form.is_valid():
        messages.error(request, _("Select a valid user to assign."))
        return redirect("reviews:case-detail", pk=case.pk)
    try:
        assign_review_case(
            review_case=case,
            expected_version=int(request.POST.get("expected_version", case.version)),
            assigned_by=request.user,
            assigned_user=form.cleaned_data["assigned_user_id"],
            reason=form.cleaned_data.get("reason", ""),
        )
    except (StaleVersionError, InvalidStateTransitionError) as _exc:
        return _conflict_response(
            request, _("This case changed since you loaded it. Reload and retry.")
        )
    return redirect("reviews:case-detail", pk=case.pk)


@operational_permission_required(
    "reviews.change_reviewcase_status", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def case_change_status(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="change_reviewcase_status")
    new_status = request.POST.get("new_status", "")
    if new_status not in ReviewCaseStatus.values:
        messages.error(request, _("That status is not valid."))
        return redirect("reviews:case-detail", pk=case.pk)
    try:
        change_review_case_status(
            review_case=case,
            new_status=new_status,
            expected_version=int(request.POST.get("expected_version", case.version)),
            actor=request.user,
        )
    except (StaleVersionError, InvalidStateTransitionError) as _exc:
        return _conflict_response(
            request, _("This case changed since you loaded it. Reload and retry.")
        )
    return redirect("reviews:case-detail", pk=case.pk)


@operational_permission_required(
    "reviews.add_checklistresult", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def case_checklist_result(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="add_checklistresult")
    form = ChecklistResultForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Please correct the checklist entry."))
        return redirect("reviews:case-detail", pk=case.pk)
    from .models import ChecklistDefinition

    definition = (
        ChecklistDefinition.objects.filter(
            event_edition=case.event_edition, case_type=case.case_type, is_active=True
        )
        .order_by("-created_at")
        .first()
    )
    if definition is None:
        messages.error(request, _("No active checklist is defined for this case type."))
        return redirect("reviews:case-detail", pk=case.pk)
    from .services import InvalidChecklistItemError, record_checklist_result

    try:
        record_checklist_result(
            review_case=case,
            checklist_definition=definition,
            item_code=form.cleaned_data["item_code"],
            result=form.cleaned_data["result"],
            actor=request.user,
            notes=form.cleaned_data.get("notes", ""),
        )
    except InvalidChecklistItemError:
        messages.error(request, _("That checklist item is not part of the active checklist."))
    return redirect("reviews:case-detail", pk=case.pk)


@operational_permission_required(
    "reviews.add_internalreviewnote", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def case_add_note(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="add_internalreviewnote")
    form = InternalNoteForm(request.POST)
    if form.is_valid():
        add_internal_note(
            review_case=case, note_text=form.cleaned_data["note_text"], created_by=request.user
        )
    else:
        messages.error(request, _("The note could not be saved."))
    return redirect("reviews:case-detail", pk=case.pk)


@operational_permission_required(
    "reviews.add_duplicatecandidateresolution", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def case_resolve_duplicate(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="add_duplicatecandidateresolution")
    # The candidate, when given, must be one of THIS case's own candidate
    # records (F8); anything else fails validation without a lookup.
    form = DuplicateResolutionForm(
        request.POST,
        outcome_choices=DuplicateOutcome.choices,
        candidates=duplicate_candidates_for(case),
    )
    if not form.is_valid() or form.cleaned_data["outcome"] not in DuplicateOutcome.values:
        messages.error(request, _("Select a valid outcome."))
        return redirect("reviews:case-detail", pk=case.pk)
    identifier = form.cleaned_data.get("candidate_identifier_id")
    if identifier is not None:
        resolve_duplicate_candidate(
            review_case=case,
            outcome=form.cleaned_data["outcome"],
            reviewed_by=request.user,
            candidate_person=identifier.person,
            candidate_identity_identifier=identifier,
            reason=form.cleaned_data.get("reason", ""),
        )
    else:
        resolve_duplicate_candidate(
            review_case=case,
            outcome=form.cleaned_data["outcome"],
            reviewed_by=request.user,
            reason=form.cleaned_data.get("reason", ""),
        )
    return redirect("reviews:case-detail", pk=case.pk)


@operational_permission_required(
    "reviews.add_informationrequest", login_url="accounts:operational-sign-in"
)
def case_create_information_request(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="add_informationrequest")
    form = InformationRequestForm(request.POST or None)
    formset = RequestItemFormSet(request.POST or None)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        items = [
            {
                "kind": item_form.cleaned_data["kind"],
                "field_code": item_form.cleaned_data.get("field_code", ""),
                "document_type": item_form.cleaned_data.get("document_type", ""),
                "is_required": item_form.cleaned_data.get("is_required", True),
                "instructions_en": item_form.cleaned_data.get("instructions_en", ""),
                "instructions_fr": item_form.cleaned_data.get("instructions_fr", ""),
                "instructions_ar": item_form.cleaned_data.get("instructions_ar", ""),
            }
            for item_form in formset
            if item_form.cleaned_data
        ]
        try:
            information_request = create_information_request(
                registration=case.registration,
                review_case=case,
                purpose=form.cleaned_data["purpose"],
                message_en=form.cleaned_data["message_en"],
                message_fr=form.cleaned_data.get("message_fr", ""),
                message_ar=form.cleaned_data.get("message_ar", ""),
                deadline_at=form.cleaned_data.get("deadline_at"),
                items=items,
                created_by=request.user,
            )
        except ActiveInformationRequestExistsError:
            messages.error(
                request, _("This Registration already has an active Information Request.")
            )
            return redirect("reviews:case-detail", pk=case.pk)
        return redirect("reviews:information-request-detail", pk=information_request.pk)
    return render(
        request,
        "reviews/information_request_create.html",
        {"case": case, "form": form, "formset": formset},
    )


@operational_permission_required(
    "reviews.view_informationrequest", login_url="accounts:operational-sign-in"
)
def information_request_detail(request, pk):
    information_request = get_object_or_404(
        information_requests_visible_to(request.user).select_related("registration"), pk=pk
    )
    scope = {
        "event_edition_id": information_request.registration.event_edition_id,
        "organization_id": information_request.registration.source_organization_id,
    }
    return render(
        request,
        "reviews/information_request_detail.html",
        {
            "information_request": information_request,
            "items": information_request.items.all(),
            "response": getattr(information_request, "response", None),
            "can_send": has_scoped_permission(
                request.user, "reviews.change_informationrequest", **scope
            ),
        },
    )


@operational_permission_required(
    "reviews.change_informationrequest", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def information_request_send(request, pk):
    information_request = get_object_or_404(
        information_requests_visible_to(request.user, codename="change_informationrequest"), pk=pk
    )
    try:
        send_information_request(
            information_request=information_request,
            expected_version=int(request.POST.get("expected_version", information_request.version)),
            actor=request.user,
        )
    except (StaleVersionError, InvalidStateTransitionError) as _exc:
        return _conflict_response(request, _("This request changed since you loaded it."))
    return redirect("reviews:information-request-detail", pk=information_request.pk)


@operational_permission_required(
    "reviews.change_informationrequest", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def information_request_cancel(request, pk):
    information_request = get_object_or_404(
        information_requests_visible_to(request.user, codename="change_informationrequest"), pk=pk
    )
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("A reason is required to cancel this request."))
        return redirect("reviews:information-request-detail", pk=information_request.pk)
    try:
        cancel_information_request(
            information_request=information_request,
            expected_version=int(request.POST.get("expected_version", information_request.version)),
            actor=request.user,
            reason=form.cleaned_data["reason"],
        )
    except (StaleVersionError, InvalidStateTransitionError) as _exc:
        return _conflict_response(request, _("This request changed since you loaded it."))
    return redirect("reviews:information-request-detail", pk=information_request.pk)


@operational_permission_required(
    "reviews.change_informationrequest", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def information_request_close(request, pk):
    information_request = get_object_or_404(
        information_requests_visible_to(request.user, codename="change_informationrequest"), pk=pk
    )
    try:
        close_information_request(
            information_request=information_request,
            expected_version=int(request.POST.get("expected_version", information_request.version)),
            actor=request.user,
        )
    except (StaleVersionError, InvalidStateTransitionError) as _exc:
        return _conflict_response(request, _("This request changed since you loaded it."))
    return redirect("reviews:information-request-detail", pk=information_request.pk)


@operational_permission_required(
    "reviews.add_registrationdecision", login_url="accounts:operational-sign-in"
)
def case_decision(request, pk):
    case = _get_scoped_case_or_404(request, pk, codename="add_registrationdecision")
    form = NotApprovedDecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            record_not_approved_decision(
                registration=case.registration,
                expected_version=int(
                    request.POST.get("expected_version", case.registration.version)
                ),
                internal_reason_code=form.cleaned_data["internal_reason_code"],
                decided_by=request.user,
                internal_note=form.cleaned_data.get("internal_note", ""),
                participant_reason_code=form.cleaned_data.get("participant_reason_code", ""),
            )
        except (StaleVersionError, InvalidStateTransitionError) as _exc:
            return _conflict_response(request, _("This registration changed since you loaded it."))
        return redirect("reviews:case-detail", pk=case.pk)
    return render(request, "reviews/decision_not_approved.html", {"case": case, "form": form})


@operational_permission_required(
    "reviews.add_registrationdecision", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def case_decision_approve(request, pk):
    """Record an APPROVED decision for `pk`'s Registration Context (Phase 2
    Prompt 8 §4, closing P7-H-01). POST-only -- unlike `case_decision`,
    approval collects no free-text reason, so there is no separate GET
    confirmation page: the case-detail control posts directly here.

    Mirrors `case_decision`'s exact-permission, scoped-case-lookup, and
    optimistic-version pattern precisely, and reuses
    `apps.reviews.services.record_approved_decision` UNCHANGED -- this view
    adds no eligibility logic of its own; Prompt 4's Participant Role/Badge
    Type/Access Profile prerequisites are enforced entirely inside the
    service (Prompt 3/4 boundary).
    """
    case = _get_scoped_case_or_404(request, pk, codename="add_registrationdecision")
    form = ApprovedDecisionForm(request.POST)
    if not form.is_valid() and "expected_version" in form.errors:
        return _conflict_response(
            request, _("Invalid approval request. Reload the case and try again.")
        )
    # An absent or unknown attendance choice reaches the service as None and
    # is refused there (audited), never defaulted.
    attendance_category = (form.cleaned_data or {}).get("attendance_category") or None
    from apps.accreditation.attendance import AttendanceError

    try:
        record_approved_decision(
            registration=case.registration,
            expected_version=form.cleaned_data["expected_version"],
            decided_by=request.user,
            attendance_category=attendance_category,
            participant_reason_code="",
        )
    except StaleVersionError, InvalidStateTransitionError:
        return _conflict_response(request, _("This registration changed since you loaded it."))
    except AttendanceError as refusal:
        from apps.accreditation.presentation import attendance_error_message

        messages.error(request, attendance_error_message(refusal))
        return redirect("reviews:case-detail", pk=case.pk)
    except ApprovalRequiresAssignmentsError:
        messages.error(
            request,
            _(
                "This registration cannot be approved yet: a current Participant Role, "
                "Badge Type, and Access Profile assignment are all required first."
            ),
        )
        return redirect("reviews:case-detail", pk=case.pk)
    except ApprovalRequiresVerifiedIdentityError as refusal:
        # IDV-Q1: approval needs a current, valid, verified identity.
        from apps.people.selectors.clearance import CLEARANCE_MESSAGES

        messages.error(
            request,
            CLEARANCE_MESSAGES.get(
                refusal.code, _("This registration cannot be approved: identity not verified.")
            ),
        )
        return redirect("reviews:case-detail", pk=case.pk)
    return redirect("reviews:case-detail", pk=case.pk)


@operational_permission_required(
    "reviews.reopen_reviewcase", login_url="accounts:operational-sign-in"
)
def registration_reopen(request, pk):
    registration = _get_scoped_registration_or_404(request, pk, codename="reopen_reviewcase")
    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            new_case = reopen_registration(
                registration=registration,
                expected_version=int(request.POST.get("expected_version", registration.version)),
                reason=form.cleaned_data["reason"],
                actor=request.user,
            )
        except (StaleVersionError, ValueError) as exc:
            return _conflict_response(request, str(exc))
        return redirect("reviews:case-detail", pk=new_case.pk)
    return render(
        request, "reviews/reopen_registration.html", {"registration": registration, "form": form}
    )


@operational_permission_required(
    "reviews.cancel_registration", login_url="accounts:operational-sign-in"
)
def registration_cancel(request, pk):
    registration = _get_scoped_registration_or_404(request, pk, codename="cancel_registration")
    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            cancel_registration_operationally(
                registration=registration,
                expected_version=int(request.POST.get("expected_version", registration.version)),
                reason=form.cleaned_data["reason"],
                actor=request.user,
            )
        except StaleVersionError as exc:
            return _conflict_response(request, str(exc))
        return redirect("registrations:ops-intake-detail", pk=registration.pk)
    return render(
        request, "reviews/cancel_registration.html", {"registration": registration, "form": form}
    )


# ---------------------------------------------------------------------------
# Participant response (workspace)
# ---------------------------------------------------------------------------


@participant_auth.participant_required
def my_information_request(request, pk):
    """The participant's OWN active Information Request for one Registration
    Context (Phase 2 Prompt 3 §7.3) -- ownership-checked, never another
    context's request, never internal notes or unrelated candidates."""
    person_id = participant_auth.get_valid_participant_person_id(request)
    information_request = get_object_or_404(
        InformationRequest.objects.filter(registration__person_id=person_id).exclude(
            status=InformationRequestStatus.DRAFT
        ),
        pk=pk,
    )
    if request.method == "POST":
        return _handle_information_response_submission(request, information_request)
    draft = information_request.draft_snapshot_json or {}
    items_with_drafts = [
        (item, draft.get(str(item.pk), "")) for item in information_request.items.all()
    ]
    return render(
        request,
        "reviews/participant_information_request.html",
        {
            "information_request": information_request,
            "items_with_drafts": items_with_drafts,
            "can_respond": information_request.status
            in InformationRequestStatus.participant_action_required_statuses(),
            "already_submitted": information_request.status
            in (InformationRequestStatus.SUBMITTED, InformationRequestStatus.CLOSED),
        },
    )


def _handle_information_response_submission(request, information_request: InformationRequest):
    from apps.accounts.participant_auth import get_participant_person_id
    from apps.documents.services import RequestedEvidenceValidationError, save_requested_evidence
    from apps.people.models import Person

    from .services import RegistrationOwnershipError, submit_information_response

    person = get_object_or_404(Person, pk=get_participant_person_id(request))
    is_draft = request.POST.get("action") == "draft"
    if (
        information_request.status
        not in InformationRequestStatus.participant_action_required_statuses()
    ):
        return _conflict_response(request, _("This request is no longer accepting a response."))

    items = list(information_request.items.all())
    if not is_draft:
        for item in items:
            if not item.is_required:
                continue
            if item.kind == "DOCUMENT_UPLOAD":
                is_missing = request.FILES.get(f"document_{item.pk}") is None
            else:
                is_missing = not request.POST.get(f"value_{item.pk}", "").strip()
            if is_missing:
                messages.error(request, _("Please complete every required requested item."))
                return redirect("reviews:my-information-request", pk=information_request.pk)

    answers = []
    draft_answers = {}
    for item in items:
        text_value = request.POST.get(f"value_{item.pk}", "")
        uploaded_file = request.FILES.get(f"document_{item.pk}")
        document = None
        if uploaded_file is not None and not is_draft:
            try:
                document = save_requested_evidence(
                    registration=information_request.registration,
                    person=person,
                    request_item=item,
                    uploaded_file=uploaded_file,
                )
            except RequestedEvidenceValidationError as exc:
                from apps.documents.services import SCAN_UNAVAILABLE
                from apps.documents.views import scan_unavailable_message

                if exc.reason_code == SCAN_UNAVAILABLE:
                    messages.error(request, scan_unavailable_message())
                else:
                    messages.error(request, _("One of the uploaded files could not be accepted."))
                return redirect("reviews:my-information-request", pk=information_request.pk)
        answers.append({"request_item_id": item.pk, "value": text_value, "document": document})
        draft_answers[str(item.pk)] = text_value

    from .services import save_information_response_draft

    if is_draft:
        try:
            save_information_response_draft(
                information_request=information_request, person=person, answers=draft_answers
            )
        except (RegistrationOwnershipError, InvalidStateTransitionError) as _exc:
            return _conflict_response(
                request, _("This request changed since you loaded it. Reload and retry.")
            )
        messages.info(request, _("Your draft has been saved."))
        return redirect("reviews:my-information-request", pk=information_request.pk)

    try:
        submit_information_response(
            information_request=information_request, person=person, answers=answers
        )
    except (RegistrationOwnershipError, InvalidInformationResponseError, ValueError) as _exc:
        # ValueError: the request was closed meanwhile (for example by a final
        # identity rejection, IDV-Q-C1); a visible message, never a 500.
        messages.error(request, _("The response is incomplete or no longer valid."))
    return redirect("reviews:my-information-request", pk=information_request.pk)


@participant_auth.participant_required
@require_http_methods(["POST"])
def my_registration_withdraw(request, pk):
    from .services import withdraw_registration

    person_id = participant_auth.get_valid_participant_person_id(request)
    from apps.people.models import Person

    person = get_object_or_404(Person, pk=person_id)
    registration = get_object_or_404(Registration, pk=pk, person=person)
    try:
        withdraw_registration(
            registration=registration,
            person=person,
            expected_version=int(request.POST.get("expected_version", registration.version)),
        )
    except StaleVersionError:
        return _conflict_response(
            request, _("This registration changed since you loaded it. Reload and retry.")
        )
    except InvalidStateTransitionError:
        messages.info(request, _("This registration cannot be withdrawn."))
        return redirect("registrations:workspace")
    messages.info(request, _("Your registration has been withdrawn."))
    return redirect("registrations:workspace")
