"""Controlled export workspace views (Phase 2 Prompt 5 §4.5).

Every mutation/retrieval re-checks `has_scoped_permission` against the
EXACT event/organization scope involved -- never only the broad,
unscoped `operational_permission_required` gate -- mirroring
`apps.accreditation.views`'s "action-specific object scope on every
mutation" pattern. A denied or nonexistent export always returns a
generic 404 (never a distinguishing 403), matching
`apps.documents.views.document_stream`.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.policies import has_scoped_permission, operational_permission_required

from .forms import ExportRequestForm
from .selectors import (
    export_requests_visible_to,
    export_scope_choices,
    registrations_visible_for_export,
)
from .services import (
    ExportAuthorizationError,
    ExportExpiredError,
    ExportNotReadyError,
    InvalidExportPurposeError,
    InvalidExportReasonError,
    request_export,
    retrieve_export,
)

_GENERIC_NOT_FOUND = "Export not found."


@operational_permission_required(
    "exports.add_exportrequest", login_url="accounts:operational-sign-in"
)
def export_workspace(request):
    """List the caller's visible exports; on POST, generate a new one
    scoped to an event edition (and, optionally, one organization) the
    caller is actually authorized for.

    Event and organization are chosen from the caller's own export scope
    (UI/UX Completion Gate F8). A submitted value outside that scope, or a
    forged one, is answered exactly like a denied scope (409), so the
    response never tells whether that event or organization exists."""
    events, organizations = export_scope_choices(request.user)
    form = ExportRequestForm(
        request.POST if request.method == "POST" else None,
        events=events,
        organizations=organizations,
    )
    context = {
        "exports": export_requests_visible_to(request.user)
        .select_related("event_edition")
        .order_by("-created_at")[:50],
        "form": form,
    }
    if request.method != "POST":
        return render(request, "exports/workspace.html", context)

    if not form.is_valid():
        if form.has_scope_violation():
            messages.error(
                request, _("You are not authorized to request an export for this scope.")
            )
            return render(request, "exports/workspace.html", context, status=409)
        return render(request, "exports/workspace.html", context)

    event_edition = form.cleaned_data["event_edition_id"]
    organization = form.cleaned_data.get("organization_id")
    if not has_scoped_permission(
        request.user,
        "exports.add_exportrequest",
        event_edition_id=event_edition.pk,
        organization_id=getattr(organization, "pk", None),
    ):
        messages.error(request, _("You are not authorized to request an export for this scope."))
        return render(request, "exports/workspace.html", context, status=409)

    scoped_registrations = registrations_visible_for_export(request.user).filter(
        event_edition=event_edition
    )
    if organization is not None:
        scoped_registrations = scoped_registrations.filter(source_organization=organization)
    registration_ids = list(scoped_registrations.values_list("pk", flat=True))

    try:
        request_export(
            event_edition=event_edition,
            organization=organization,
            purpose_code=form.cleaned_data["purpose_code"],
            reason=form.cleaned_data["reason"],
            requested_by=request.user,
            registration_ids=registration_ids,
        )
    except InvalidExportPurposeError:
        messages.error(request, _("Select an approved export purpose."))
        return render(request, "exports/workspace.html", context)
    except InvalidExportReasonError:
        messages.error(request, _("A reason is required to request an export."))
        return render(request, "exports/workspace.html", context)

    messages.success(request, _("Export generated."))
    return redirect("exports:workspace")


@require_http_methods(["GET"])
def export_download(request, pk):
    export_request = get_object_or_404(
        export_requests_visible_to(request.user, codename="view_exportrequest"), pk=pk
    )
    if not has_scoped_permission(
        request.user,
        "exports.view_exportrequest",
        event_edition_id=export_request.event_edition_id,
        organization_id=export_request.organization_id,
    ):
        raise Http404(_GENERIC_NOT_FOUND)
    try:
        stream = retrieve_export(export_request=export_request, actor=request.user)
    except ExportAuthorizationError, ExportNotReadyError, ExportExpiredError:
        raise Http404(_GENERIC_NOT_FOUND) from None
    response = FileResponse(
        stream,
        content_type="text/csv",
        as_attachment=True,
        filename=f"export-{export_request.pk}.csv",
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
