"""Identity review (operations) and identity correction (participant) views (IDV-3).

Views coordinate HTTP only. Each operational route is gated by the action's
own permission (broad check), the case is fetched through the exact-scope
selector (a generic 404 otherwise), and the service checks the action's
permission in the case's exact scope again before any write. Every page that
shows identity data is `Cache-Control: private, no-store`.
"""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts import participant_auth
from apps.accounts.policies import operational_permission_required
from apps.audit import action_codes
from apps.core.middleware.correlation import get_correlation_id
from apps.people.forms.identity import (
    CorrectNinForm,
    ExceptionVerifyForm,
    GrantNinExemptionForm,
    IdentityCorrectionForm,
    RejectIdentityForm,
    ReturnForCorrectionForm,
    RevokeNinExemptionForm,
    VerifyIdentityForm,
)
from apps.people.models import (
    IdentityReasonCode,
    IdentityRoute,
    IdentityStatus,
    IdentityVerification,
    VerificationSource,
)
from apps.people.policies import (
    IdentityPermissionDenied,
    granted_identity_codenames,
    identity_verifications_visible_to,
)
from apps.people.search_context import (
    clear_search_context,
    search_context_for,
    store_search_context,
)
from apps.people.selectors.identity import (
    ALL_STATUSES,
    QUEUE_FILTER_KEYS,
    case_history,
    clean_queue_filters,
    conflicts_for,
    filter_choices,
    latest_provider_attempt,
    names_as_entered,
    next_case_after,
    queue_queryset,
    resolve_queue_search,
    status_counts,
)
from apps.people.services import identity_review
from apps.people.services.identity_verification import ACTIVE_PUBLIC_STATUSES, _audit

LOGIN = "accounts:operational-sign-in"
PAGE_SIZE = 25

_EVIDENCE_LABELS = {
    "NATIONAL_ID_CARD": gettext_lazy("National identity card"),
    "PASSPORT_IDENTITY_PAGE": gettext_lazy("Passport identity page"),
}


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "private, no-store"
    return response


def _queue_querystring(filters: dict[str, str], *, without: tuple[str, ...] = ()) -> str:
    """The queue state for a URL. It never holds search text: a search is the
    opaque `ctx` reference of a server-side context (IDV-C1, R-IDV-05)."""
    return urlencode(
        {key: filters[key] for key in QUEUE_FILTER_KEYS if filters.get(key) and key not in without}
    )


def _queue_state(request, source):
    """`(filters, search, expired)`: the cleaned filters and the reviewer's own
    live search context for this request. An unknown, expired or foreign `ctx`
    is dropped and reported as expired."""
    filters = clean_queue_filters(source)
    search, expired = None, False
    reference = filters.get("ctx")
    if reference:
        search = search_context_for(request.session, request.user, reference)
        if search is None:
            filters.pop("ctx")
            expired = True
    return filters, search, expired


def _queue_url(filters: dict[str, str]) -> str:
    querystring = _queue_querystring(filters)
    url = reverse("identity:queue")
    return f"{url}?{querystring}" if querystring else url


def _case_or_404(request, pk) -> IdentityVerification:
    return get_object_or_404(
        identity_verifications_visible_to(request.user).select_related(
            "registration",
            "registration__event_edition",
            "registration__profile",
            "nationality",
            "current_revision__identifier",
        ),
        pk=pk,
    )


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@operational_permission_required("people.view_identityverification", login_url=LOGIN)
def identity_queue(request):
    if "q" in request.GET:
        # A search from the address is never used (R-IDV-05): drop it.
        return redirect(_queue_url(clean_queue_filters(request.GET)))
    filters, search, search_expired = _queue_state(request, request.GET)
    page = Paginator(queue_queryset(request.user, filters, search=search), PAGE_SIZE).get_page(
        request.GET.get("page", 1)
    )
    counts = status_counts(request.user, filters, search=search)
    choices = filter_choices(request.user)
    from apps.core.models import Country

    nationality_names = {
        country.pk: country.localized_name
        for country in Country.objects.filter(pk__in=choices["nationalities"])
    }
    return _no_store(
        render(
            request,
            "identity/queue.html",
            {
                "page": page,
                "filters": filters,
                "querystring": _queue_querystring(filters),
                "tab_querystring": _queue_querystring(filters, without=("status",)),
                "search": search,
                "search_expired": search_expired,
                "status_tabs": [
                    (value, label, counts.get(value, 0)) for value, label in IdentityStatus.choices
                ],
                "total_count": sum(counts.values()),
                "all_statuses": ALL_STATUSES,
                "reasons": IdentityReasonCode.choices,
                "routes": IdentityRoute.choices,
                "sources": VerificationSource.choices,
                "events": choices["events"],
                "nationalities": sorted(nationality_names.items(), key=lambda item: item[1]),
            },
        )
    )


@operational_permission_required("people.view_identityverification", login_url=LOGIN)
@require_POST
def identity_search(request):
    """Resolve a queue search on the server (R-IDV-05).

    The text arrives in a CSRF-protected POST body and is used once: the
    matching case ids are kept in the reviewer's session under a random
    reference, and the redirect carries only that reference. `action=clear`
    forgets a context. The audit event records the kind and the number of
    matches, never the text.
    """
    from apps.audit.contracts import AuditRecord
    from apps.audit.services import PersistentAuditRecorder

    filters, _search, _expired = _queue_state(request, request.POST)
    reference = filters.pop("ctx", "")
    if request.POST.get("action") == "clear":
        clear_search_context(request.session, request.user, reference)
        return redirect(_queue_url(filters))
    kind, case_ids, truncated = resolve_queue_search(request.user, request.POST.get("q", ""))
    if kind:
        filters["ctx"] = store_search_context(
            request.session, request.user, kind=kind, case_ids=case_ids, truncated=truncated
        )
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=request.user.pk,
                action_code=action_codes.IDENTITY_QUEUE_SEARCHED,
                target_type="IdentityQueue",
                result="SUCCESS",
                after_summary={"kind": kind, "matches": len(case_ids), "truncated": truncated},
                correlation_id=get_correlation_id() or "",
            )
        )
    return redirect(_queue_url(filters))


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


def _evidence_rows(case, *, can_view_evidence: bool):
    from apps.documents.services import active_identity_evidence, is_reviewable_evidence

    rows = []
    for document in active_identity_evidence(case.registration):
        reviewable = is_reviewable_evidence(document)
        rows.append(
            {
                "document": document,
                "label": _EVIDENCE_LABELS.get(document.document_type, document.document_type),
                "reviewable": reviewable,
                "scan_status": document.stored_object.malware_scan_status,
                "preview_url": (
                    reverse("identity:evidence", kwargs={"pk": case.pk, "document_id": document.pk})
                    if reviewable and can_view_evidence
                    else ""
                ),
            }
        )
    return rows


def _evidence_choices(rows, allowed_types):
    return [
        (str(row["document"].pk), f"{row['label']} ({row['document'].created_at:%Y-%m-%d %H:%M})")
        for row in rows
        if row["reviewable"] and row["document"].document_type in allowed_types
    ]


def _comparison_rows(case, attempt):
    """Submitted versus retained official facts, field by field."""
    profile = case.registration.profile
    comparison = (attempt.comparison or {}) if attempt is not None else {}
    official_date = ""
    if attempt is not None and attempt.official_birth_date:
        official_date = datetime.date.fromisoformat(attempt.official_birth_date)
    return [
        {
            "label": _("Family name"),
            "submitted": profile.submitted_family_name,
            "official": attempt.official_family_name_latin if attempt else "",
            "outcome": comparison.get("family_name", ""),
        },
        {
            "label": _("Given name(s)"),
            "submitted": profile.submitted_given_names,
            "official": attempt.official_given_names_latin if attempt else "",
            "outcome": comparison.get("given_names", ""),
        },
        {
            "label": _("Date of birth"),
            "submitted": profile.date_of_birth,
            "official": official_date,
            "official_text": attempt.official_birth_date_text if attempt else "",
            "outcome": comparison.get("birth_date", ""),
            "is_date": True,
        },
    ]


def _render_case(request, case, *, forms_by_action=None, status=200):
    filters, search, _expired = _queue_state(request, request.GET or request.POST)
    granted = granted_identity_codenames(request.user, case)
    can_view_evidence = "view_identity_evidence" in granted
    rows = _evidence_rows(case, can_view_evidence=can_view_evidence)
    attempt = latest_provider_attempt(case)
    revisions, decisions, attempts = case_history(case)
    conflicts = conflicts_for(request.user, case)
    identifier = case.current_revision.identifier if case.current_revision_id else None
    initial = {
        "expected_version": case.version,
        "after_changed_at": case.status_changed_at.isoformat(),
        "after_pk": case.pk,
    }
    nin_types = identity_review.manual_evidence_types(case)
    forms_by_action = forms_by_action or {}
    context_forms = {
        "verify": forms_by_action.get("verify")
        or VerifyIdentityForm(initial=initial, evidence_choices=_evidence_choices(rows, nin_types)),
        "exception": forms_by_action.get("exception")
        or ExceptionVerifyForm(
            initial=initial,
            evidence_choices=_evidence_choices(
                rows, {"NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE"}
            ),
        ),
        "return": forms_by_action.get("return")
        or ReturnForCorrectionForm(initial=initial, route=case.route),
        "correct": forms_by_action.get("correct")
        or CorrectNinForm(
            initial=initial,
            evidence_choices=_evidence_choices(
                rows, {"NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE"}
            ),
        ),
        "reject": forms_by_action.get("reject") or RejectIdentityForm(initial=initial),
    }
    _audit(
        action_codes.IDENTITY_CASE_VIEWED,
        verification=case,
        actor_user=request.user,
        after={"status": case.status},
        correlation_id=get_correlation_id() or "",
    )
    # R-IDV-06: nothing is offered on a closed registration.
    registration_closed = case.registration.public_status not in ACTIVE_PUBLIC_STATUSES
    # IDV-Q3: the grant behind a documentary-route case.
    exemption = None
    if case.route == IdentityRoute.NIN_EXEMPTION:
        from apps.people.services.nin_exemption import exemption_for_case

        exemption = exemption_for_case(case)
    actionable = case.status == IdentityStatus.MANUAL_REVIEW and not registration_closed
    following = next_case_after(
        request.user,
        filters,
        after_key=(case.status_changed_at, case.pk),
        exclude_pk=case.pk,
        search=search,
    )
    querystring = _queue_querystring(filters)
    skip_url = ""
    if following is not None:
        skip_url = reverse("identity:case", kwargs={"pk": following.pk})
        skip_url = f"{skip_url}?{querystring}" if querystring else skip_url
    action_codenames = granted - {"view_identityverification", "view_identity_evidence"}
    return _no_store(
        render(
            request,
            "identity/review.html",
            {
                "case": case,
                "registration": case.registration,
                "profile": case.registration.profile,
                "identifier": identifier,
                "show_full_identifier": can_view_evidence,
                "granted": granted,
                "evidence_rows": rows,
                "attempt": attempt,
                "comparison_rows": _comparison_rows(case, attempt),
                "revisions": revisions,
                "decisions": decisions,
                "attempts": attempts,
                "conflicts": conflicts,
                "forms": context_forms,
                "actionable": actionable,
                "rejectable": not registration_closed
                and case.status
                in (IdentityStatus.MANUAL_REVIEW, IdentityStatus.RETURNED_FOR_CORRECTION),
                "registration_closed": registration_closed,
                "exemption": exemption,
                # IDV-Q2: the participation outcome of a final rejection.
                "identity_rejection_outcome": case.status == IdentityStatus.REJECTED
                and case.registration.public_status == "NOT_APPROVED",
                "names_as_entered": names_as_entered(case),
                "filters": filters,
                "querystring": querystring,
                "skip_url": skip_url,
                "can_act_any": bool(action_codenames),
                "first_preview_url": next(
                    (row["preview_url"] for row in rows if row["preview_url"]), ""
                ),
                "return_items": identity_review.RETURN_ITEMS[case.route],
            },
            status=status,
        )
    )


@operational_permission_required("people.view_identityverification", login_url=LOGIN)
def identity_case(request, pk):
    return _render_case(request, _case_or_404(request, pk))


def _conflict(request, case, message: str) -> HttpResponse:
    querystring = _queue_querystring(_queue_state(request, request.POST)[0])
    url = reverse("identity:case", kwargs={"pk": case.pk})
    return _no_store(
        render(
            request,
            "identity/conflict.html",
            {"message": message, "case_url": f"{url}?{querystring}" if querystring else url},
            status=409,
        )
    )


def _after_success(request, case, *, message: str):
    """Back to the case (filters kept), or on to the next eligible case."""
    messages.success(request, message)
    filters, search, _expired = _queue_state(request, request.POST)
    querystring = _queue_querystring(filters)
    if request.POST.get("advance") == "next":
        after_key = None
        changed_at = parse_datetime(request.POST.get("after_changed_at") or "")
        after_pk = request.POST.get("after_pk") or ""
        if changed_at is not None and after_pk:
            after_key = (changed_at, after_pk)
        following = next_case_after(
            request.user, filters, after_key=after_key, exclude_pk=case.pk, search=search
        )
        if following is not None:
            url = reverse("identity:case", kwargs={"pk": following.pk})
        else:
            messages.info(request, _("No further case matches the current filters."))
            url = reverse("identity:queue")
    else:
        url = reverse("identity:case", kwargs={"pk": case.pk})
    return redirect(f"{url}?{querystring}" if querystring else url)


def _run_action(request, pk, *, form_key, form, command, success_message):
    case = _case_or_404(request, pk)
    if not form.is_valid():
        return _render_case(request, case, forms_by_action={form_key: form}, status=400)
    try:
        command(case, form.cleaned_data)
    except IdentityPermissionDenied:
        _audit(
            action_codes.IDENTITY_ACTION_REFUSED,
            verification=case,
            actor_user=request.user,
            result="DENIED",
            reason_code=form_key,
            correlation_id=get_correlation_id() or "",
        )
        raise
    except identity_review.StaleIdentityVersion:
        return _conflict(
            request,
            case,
            _(
                "This case changed after you opened it: another reviewer, the participant or "
                "the verification service updated it. Nothing was saved. Open the case again "
                "to see its current state."
            ),
        )
    except identity_review.DuplicateIdentityConflict:
        form.add_error(
            None,
            _(
                "Another person's registration holds this identifier. Resolve that conflict "
                "first; it cannot be overridden here."
            ),
        )
        return _render_case(request, case, forms_by_action={form_key: form}, status=409)
    except identity_review.EvidenceUnavailable:
        form.add_error(
            None,
            _("Select reviewable evidence: a clean, current document of the required kind."),
        )
        return _render_case(request, case, forms_by_action={form_key: form}, status=400)
    except identity_review.InvalidIdentityInput as error:
        message = {
            "explanation_required": _("Write an explanation of at least 10 characters."),
            "confirmation_required": _("Confirm this action deliberately to continue."),
            "nin_format": _("Enter exactly 18 digits."),
            "document_details": _("Enter the document number as printed on the document."),
            "document_expiry": _("Enter an expiry date in the future."),
        }.get(error.code, _("Check the selected reason and the values entered."))
        form.add_error(None, message)
        return _render_case(request, case, forms_by_action={form_key: form}, status=400)
    except identity_review.IdentityStateError:
        return _conflict(
            request,
            case,
            _(
                "This action is not available in the case's current state. Open the case again "
                "to see what can be done now."
            ),
        )
    case.refresh_from_db()
    return _after_success(request, case, message=success_message)


def _evidence_form_choices(case, types):
    rows = _evidence_rows(case, can_view_evidence=False)
    return _evidence_choices(rows, types)


@operational_permission_required("people.verify_identity_manually", login_url=LOGIN)
@require_POST
def identity_verify(request, pk):
    case = _case_or_404(request, pk)
    types = identity_review.manual_evidence_types(case)
    form = VerifyIdentityForm(request.POST, evidence_choices=_evidence_form_choices(case, types))
    return _run_action(
        request,
        pk,
        form_key="verify",
        form=form,
        command=lambda c, data: identity_review.verify_identity_manually(
            c.pk,
            actor=request.user,
            expected_version=data["expected_version"],
            evidence_document_id=data["evidence_document"],
            reason_code=data["reason_code"],
            note=data.get("note", ""),
            correlation_id=get_correlation_id() or "",
        ),
        success_message=_("Identity verified manually. Participation is decided separately."),
    )


@operational_permission_required("people.apply_identity_exception", login_url=LOGIN)
@require_POST
def identity_exception(request, pk):
    case = _case_or_404(request, pk)
    form = ExceptionVerifyForm(
        request.POST,
        evidence_choices=_evidence_form_choices(
            case, {"NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE"}
        ),
    )
    return _run_action(
        request,
        pk,
        form_key="exception",
        form=form,
        command=lambda c, data: identity_review.verify_identity_manually(
            c.pk,
            actor=request.user,
            expected_version=data["expected_version"],
            evidence_document_id=data["evidence_document"],
            reason_code=data["reason_code"],
            note=data["note"],
            exception=True,
            correlation_id=get_correlation_id() or "",
        ),
        success_message=_("Identity verified by staff-assisted exception."),
    )


@operational_permission_required("people.return_identity_for_correction", login_url=LOGIN)
@require_POST
def identity_return(request, pk):
    case = _case_or_404(request, pk)
    form = ReturnForCorrectionForm(request.POST, route=case.route)
    return _run_action(
        request,
        pk,
        form_key="return",
        form=form,
        command=lambda c, data: identity_review.return_identity_for_correction(
            c.pk,
            actor=request.user,
            expected_version=data["expected_version"],
            items=data["items"],
            note=data.get("note", ""),
            correlation_id=get_correlation_id() or "",
        ),
        success_message=_("Returned to the participant for correction."),
    )


@operational_permission_required("people.correct_identity_nin", login_url=LOGIN)
@require_POST
def identity_correct_nin(request, pk):
    case = _case_or_404(request, pk)
    form = CorrectNinForm(
        request.POST,
        evidence_choices=_evidence_form_choices(
            case, {"NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE"}
        ),
    )
    return _run_action(
        request,
        pk,
        form_key="correct",
        form=form,
        command=lambda c, data: identity_review.correct_nin_and_recheck(
            c.pk,
            actor=request.user,
            expected_version=data["expected_version"],
            new_nin=data["new_nin"],
            reason_code=data["reason_code"],
            note=data.get("note", ""),
            evidence_document_id=data.get("evidence_document") or None,
            confirmed=bool(data.get("confirmed")),
            correlation_id=get_correlation_id() or "",
        ),
        success_message=_(
            "The NIN check was queued. The case stays pending until the service answers."
        ),
    )


@operational_permission_required("people.reject_identity", login_url=LOGIN)
@require_POST
def identity_reject(request, pk):
    form = RejectIdentityForm(request.POST)
    return _run_action(
        request,
        pk,
        form_key="reject",
        form=form,
        command=lambda c, data: identity_review.reject_identity(
            c.pk,
            actor=request.user,
            expected_version=data["expected_version"],
            reason_code=data["reason_code"],
            note=data["note"],
            confirmed=bool(data.get("confirmed")),
            correlation_id=get_correlation_id() or "",
        ),
        success_message=_(
            "Identity rejected. The registration is not approved, and the participant was told "
            "how to register again."
        ),
    )


# ---------------------------------------------------------------------------
# NIN exemption for one Algerian draft (owner decision IDV-Q3)
# ---------------------------------------------------------------------------


def _exemption_registration_or_404(request, pk):
    from apps.people.policies import registrations_for_nin_exemption

    return get_object_or_404(registrations_for_nin_exemption(request.user), pk=pk)


def _exemption_refused(request, registration, message) -> HttpResponse:
    messages.error(request, message)
    return redirect("registrations:ops-intake-detail", pk=registration.pk)


@operational_permission_required("people.grant_nin_exemption", login_url=LOGIN)
@require_POST
def nin_exemption_grant(request, pk):
    """Grant the documentary route to one draft. The broad permission gates
    the route; the registration is fetched in the exact scope (a generic 404
    otherwise); the service checks the scoped permission again."""
    from apps.people.services import nin_exemption

    registration = _exemption_registration_or_404(request, pk)
    form = GrantNinExemptionForm(request.POST)
    if not form.is_valid():
        return _exemption_refused(
            request,
            registration,
            _("Choose a reason, write an explanation of at least 10 characters and confirm."),
        )
    try:
        nin_exemption.grant_nin_exemption(
            registration.pk,
            actor=request.user,
            reason_code=form.cleaned_data["reason_code"],
            explanation=form.cleaned_data["explanation"],
            confirmed=bool(form.cleaned_data.get("confirmed")),
            correlation_id=get_correlation_id() or "",
        )
    except identity_review.InvalidIdentityInput:
        return _exemption_refused(
            request,
            registration,
            _("Choose a reason, write an explanation of at least 10 characters and confirm."),
        )
    except identity_review.IdentityStateError:
        return _exemption_refused(
            request,
            registration,
            _(
                "An exemption can be granted only once, for a draft registration of an "
                "Algerian national. Reload the page to see its current state."
            ),
        )
    messages.success(
        request,
        _(
            "NIN exemption granted. The participant can now complete the identity step with "
            "an Algerian identity card or passport; the identity will be reviewed manually."
        ),
    )
    return redirect("registrations:ops-intake-detail", pk=registration.pk)


@operational_permission_required("people.grant_nin_exemption", login_url=LOGIN)
@require_POST
def nin_exemption_revoke(request, pk):
    from apps.people.services import nin_exemption

    registration = _exemption_registration_or_404(request, pk)
    form = RevokeNinExemptionForm(request.POST)
    exemption = nin_exemption.active_exemption(registration.pk)
    if exemption is None or not form.is_valid():
        return _exemption_refused(
            request,
            registration,
            _("Write an explanation of at least 10 characters, or reload the page."),
        )
    try:
        nin_exemption.revoke_nin_exemption(
            exemption.pk,
            actor=request.user,
            expected_version=form.cleaned_data["expected_version"],
            note=form.cleaned_data["note"],
            correlation_id=get_correlation_id() or "",
        )
    except identity_review.InvalidIdentityInput:
        return _exemption_refused(
            request, registration, _("Write an explanation of at least 10 characters.")
        )
    except (identity_review.StaleIdentityVersion, identity_review.IdentityStateError):  # fmt: skip
        return _exemption_refused(
            request,
            registration,
            _("The exemption changed or was already used. Reload the page to see its state."),
        )
    messages.success(request, _("NIN exemption revoked."))
    return redirect("registrations:ops-intake-detail", pk=registration.pk)


# ---------------------------------------------------------------------------
# Protected evidence preview
# ---------------------------------------------------------------------------

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}


@operational_permission_required("people.view_identity_evidence", login_url=LOGIN)
def identity_evidence(request, pk, document_id):
    """Inline preview of one identity document, for an authorized reviewer.

    As strict as the original: exact scope for `view_identity_evidence`, a
    reviewable (active, clean) document of this case's registration only, a
    generic 404 otherwise, `no-store`, `nosniff`, same-origin resource policy,
    and an audit event without any identity value.
    """
    from apps.documents.models import IDENTITY_EVIDENCE_DOCUMENT_TYPES, Document
    from apps.documents.services import is_reviewable_evidence
    from apps.documents.storage import ObjectNotFound, PrivateStorageError, get_private_storage

    case = get_object_or_404(
        identity_verifications_visible_to(request.user, codename="view_identity_evidence"), pk=pk
    )
    document = (
        Document.objects.select_related("stored_object")
        .filter(
            pk=document_id,
            registration_id=case.registration_id,
            document_type__in=IDENTITY_EVIDENCE_DOCUMENT_TYPES,
        )
        .first()
    )
    if document is None or not is_reviewable_evidence(document):
        raise Http404("Document not found.")
    try:
        stream = get_private_storage().open(document.stored_object.storage_key)
    except (ObjectNotFound, PrivateStorageError):  # fmt: skip
        raise Http404("Document not found.") from None
    _audit(
        action_codes.IDENTITY_EVIDENCE_VIEWED,
        verification=case,
        actor_user=request.user,
        after={"document_type": document.document_type},
        correlation_id=get_correlation_id() or "",
    )
    content_type = document.stored_object.content_type
    response = FileResponse(
        stream,
        content_type=content_type,
        as_attachment=False,
        filename=f"evidence.{_EXTENSIONS.get(content_type, 'bin')}",
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return _no_store(response)


# ---------------------------------------------------------------------------
# Participant: correct and resubmit (same account, same registration)
# ---------------------------------------------------------------------------


@participant_auth.participant_required
@require_http_methods(["GET", "POST"])
def identity_correction(request, pk):
    from apps.people.models import Person
    from apps.registrations.models import Registration

    person = get_object_or_404(Person, pk=participant_auth.get_participant_person_id(request))
    registration = get_object_or_404(Registration, pk=pk, person=person)
    correction = identity_review.open_correction_request(registration)
    if correction is None:
        messages.info(request, _("There is no identity correction to make for this registration."))
        return redirect("registrations:workspace")
    case = correction.verification
    profile = registration.profile
    identifier = case.current_revision.identifier
    initial = {
        "expected_version": case.version,
        "given_names": profile.submitted_given_names,
        "family_name": profile.submitted_family_name,
        "date_of_birth": profile.date_of_birth,
    }
    if case.route == IdentityRoute.NIN:
        initial["nin_value"] = identifier.value_encrypted
    elif case.route == IdentityRoute.NIN_EXEMPTION:
        initial.update(
            document_number=identifier.value_encrypted,
            document_expires_at=identifier.expires_at,
        )
    else:
        initial.update(
            passport_number=identifier.value_encrypted,
            passport_country_code=identifier.country_code_id,
            passport_expires_at=identifier.expires_at,
        )
    form = IdentityCorrectionForm(
        request.POST or None,
        request.FILES or None,
        initial=initial,
        route=case.route,
        document_kind=identifier.identifier_type,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        country = data.get("passport_country_code")
        try:
            identity_review.resubmit_identity_correction(
                registration,
                person=person,
                expected_version=data["expected_version"],
                given_names=data["given_names"],
                family_name=data["family_name"],
                date_of_birth=data["date_of_birth"],
                nin_value=data.get("nin_value", ""),
                passport_number=data.get("passport_number", ""),
                passport_country_code_id=country.pk if country else "",
                passport_expires_at=data.get("passport_expires_at"),
                national_id_card_file=data.get("national_id_card"),
                passport_page_file=data.get("passport_identity_page"),
                document_number=data.get("document_number", ""),
                document_expires_at=data.get("document_expires_at"),
                document_file=data.get("document_image"),
                correlation_id=get_correlation_id() or "",
            )
        except identity_review.StaleIdentityVersion, identity_review.IdentityStateError:
            messages.info(request, _("Your correction was already received."))
            return redirect("registrations:workspace")
        except identity_review.EvidenceUnavailable:
            form.add_error(None, _("Please upload the requested document."))
        except identity_review.InvalidIdentityInput:
            form.add_error(None, _("Please check the information provided and try again."))
        except Exception as error:  # noqa: BLE001 - a refused upload is shown, never a 500
            from apps.documents.services import (
                NationalIdCardValidationError,
                PassportIdentityPageValidationError,
            )

            if not isinstance(
                error, (NationalIdCardValidationError, PassportIdentityPageValidationError)
            ):
                raise
            from apps.documents.services import SCAN_UNAVAILABLE
            from apps.documents.views import scan_unavailable_message

            if error.reason_code == SCAN_UNAVAILABLE:
                form.add_error(None, scan_unavailable_message())
            else:
                form.add_error(
                    None, _("This file could not be accepted. Please try a different image.")
                )
        else:
            messages.success(
                request,
                _("Thank you. Your corrected information was received and will be checked."),
            )
            return redirect("registrations:workspace")
    from django.conf import settings

    return _no_store(
        render(
            request,
            "identity/participant_correction.html",
            {
                "registration": registration,
                "form": form,
                "requested_labels": correction.labels,
                "show_passport_page": case.route == IdentityRoute.PASSPORT
                or "ALGERIAN_PASSPORT_PAGE" in correction.items,
                "exemption_route": case.route == IdentityRoute.NIN_EXEMPTION,
                "max_size_mb": settings.PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES // (1024 * 1024),
            },
        )
    )
