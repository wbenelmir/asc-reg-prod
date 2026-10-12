"""Organization management for authorized staff (UPD-06).

The master organization records were only reachable through the Django admin,
which is not mounted in staging or production. These pages make the complete
workflow usable from Organizations / Organization Workspace:

* list (search, status filter, pagination, counts), create, view, edit,
  activate and deactivate organizations -- `organizations.view_organization`,
  `add_organization`, `change_organization` (default model permissions; no
  existing staff group holds them, so they belong to the superadmin unless a
  role is granted them explicitly);
* from an organization: its campaigns (create, open/close, edit, links), its
  delegation lists (upload, preview, apply), on-behalf registration, "View
  registrations" and the export -- each control is shown only to holders of
  that action's own permission in the organization's scope, and every action
  endpoint enforces it again (`apps.invitations.views`).

Every lookup by id is scope-filtered (an out-of-scope id is a 404), and every
change is audited by its service. The subordinate data of an organization
(registration counts, campaigns, delegation lists) is read through the
reader's own action-specific selectors, so a staff member granted
organization access for one event never sees another event's campaigns or
counts (R-04); the superadmin sees everything.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.organizations.forms.management import OrganizationFilterForm, OrganizationForm
from apps.organizations.models import Organization, OrganizationStatus
from apps.organizations.selectors import organizations_visible_to
from apps.organizations.services import (
    OrganizationStatusError,
    create_organization_audited,
    find_possible_duplicate_organizations,
    set_organization_status,
    update_organization,
)

LOGIN = "accounts:operational-sign-in"
PAGE_SIZE = 50


def _count_subquery(visible, field: str):
    """A per-organization count of the reader's VISIBLE rows (`visible`, an
    action-specific selector's queryset) as a correlated subquery (no join,
    so the counts never multiply each other)."""
    from django.db.models import IntegerField, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    model = visible.model
    counts = (
        model.objects.filter(pk__in=visible.values("pk"))
        .filter(**{field: OuterRef("pk")})
        .order_by()
        .values(field)
        .annotate(total=Count("pk"))
        .values("total")
    )
    return Coalesce(Subquery(counts, output_field=IntegerField()), 0)


def _visible(user, codename: str = "view_organization"):
    return organizations_visible_to(user, app_label="organizations", codename=codename)


def _organization_or_404(request, pk, codename: str = "view_organization") -> Organization:
    organization = _visible(request.user, codename).filter(pk=pk).first()
    if organization is None:
        raise Http404("No Organization matches the given query.")
    return organization


@operational_permission_required("organizations.view_organization", login_url=LOGIN)
def organization_list(request):
    filters = OrganizationFilterForm(request.GET or None)
    queryset = _visible(request.user).select_related("country_code")
    total = queryset.count()
    query, status = "", ""
    if filters.is_valid():
        query = filters.cleaned_data.get("q", "").strip()
        status = filters.cleaned_data.get("status", "")
    if query:
        queryset = queryset.filter(
            Q(official_name__icontains=query) | Q(aliases__alias_name__icontains=query)
        ).distinct()
    if status:
        queryset = queryset.filter(status=status)
    from apps.accounts.selectors import registrations_visible_to
    from apps.invitations.selectors import campaigns_visible_to

    queryset = queryset.annotate(
        registration_count=_count_subquery(
            registrations_visible_to(request.user), "source_organization"
        ),
        campaign_count=_count_subquery(campaigns_visible_to(request.user), "organization"),
    ).order_by("official_name", "pk")
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page", 1))
    from django.utils.http import urlencode

    return render(
        request,
        "organizations/organization_list.html",
        {
            "page": page,
            "total": total,
            "query": query,
            "status": status,
            "statuses": OrganizationStatus.choices,
            "querystring": urlencode(
                {k: v for k, v in {"q": query, "status": status}.items() if v}
            ),
            "can_create": has_scoped_permission(request.user, "organizations.add_organization"),
        },
    )


@operational_permission_required("organizations.add_organization", login_url=LOGIN)
def organization_create(request):
    form = OrganizationForm(request.POST or None)
    duplicates = []
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        duplicates = find_possible_duplicate_organizations(data["official_name"])
        if duplicates and not data.get("confirm_duplicate"):
            form.add_error(
                None,
                _(
                    "An organization with the same name exists. Open it instead, or confirm "
                    "that this is a different organization."
                ),
            )
        else:
            country = data.get("country_code")
            organization = create_organization_audited(
                actor=request.user,
                official_name=data["official_name"],
                organization_type=data["organization_type"],
                country_code_id=country.pk if country else None,
            )
            messages.success(request, _("Organization created."))
            return redirect("organizations:detail", pk=organization.pk)
    return render(
        request,
        "organizations/organization_form.html",
        {"form": form, "duplicates": duplicates, "organization": None},
    )


@operational_permission_required("organizations.view_organization", login_url=LOGIN)
def organization_detail(request, pk):
    from apps.accounts.selectors import registrations_visible_to
    from apps.events.selectors import NoOpenEventEdition, current_event_edition
    from apps.invitations.selectors import campaigns_visible_to, delegation_batches_visible_to
    from apps.invitations.services import campaign_usage
    from apps.registrations import reporting
    from apps.registrations.models import Registration

    organization = _organization_or_404(request, pk)
    try:
        event = current_event_edition()
    except NoOpenEventEdition:
        event = None
    scope = {"organization_id": organization.pk}
    if event is not None:
        scope["event_edition_id"] = event.pk
    # R-04: only what the reader may see in each subordinate scope.
    visible_registrations = registrations_visible_to(request.user).filter(
        source_organization=organization
    )
    registrations = Registration.objects.filter(
        pk__in=visible_registrations.values("pk")
    ).order_by()
    counts = registrations.aggregate(
        total=Count("pk"),
        applications=Count("pk", filter=reporting.applications_q()),
        active=Count("pk", filter=reporting.active_q()),
        final_approved=Count("pk", filter=reporting.final_approved_q()),
        withdrawn=Count("pk", filter=reporting.withdrawn_q()),
    )
    campaigns = list(
        campaigns_visible_to(request.user)
        .filter(organization=organization)
        .select_related("event_edition")
        .annotate(
            registration_count=Count(
                "registrations",
                filter=Q(registrations__in=visible_registrations.values("pk")),
                distinct=True,
            )
        )
        .order_by("-created_at")
    )
    for campaign in campaigns:
        campaign.usage = campaign_usage(campaign)
        campaign.active_link = campaign.links.filter(status="ACTIVE").first()
    batches = list(
        delegation_batches_visible_to(request.user)
        .filter(organization=organization)
        .order_by("-created_at")[:50]
    )
    active = organization.status == OrganizationStatus.ACTIVE
    context = {
        "organization": organization,
        "counts": counts,
        "campaigns": campaigns,
        "batches": batches,
        "event": event,
        "active": active,
        "can_change": has_scoped_permission(
            request.user, "organizations.change_organization", organization_id=organization.pk
        ),
        "can_create_campaign": active
        and event is not None
        and has_scoped_permission(request.user, "invitations.add_invitationcampaign", **scope),
        "can_upload_delegation": active
        and event is not None
        and has_scoped_permission(request.user, "invitations.add_delegationbatch", **scope),
        "can_register_on_behalf": active
        and event is not None
        and has_scoped_permission(request.user, "registrations.register_on_behalf", **scope),
        "can_export": event is not None
        and has_scoped_permission(request.user, "exports.add_exportrequest", **scope),
        "can_view_registrations": has_scoped_permission(
            request.user, "registrations.view_registration", organization_id=organization.pk
        ),
    }
    return render(request, "organizations/organization_detail.html", context)


@operational_permission_required("organizations.change_organization", login_url=LOGIN)
def organization_edit(request, pk):
    organization = _organization_or_404(request, pk, "change_organization")
    initial = {
        "official_name": organization.official_name,
        "organization_type": organization.organization_type,
        "country_code": organization.country_code_id,
    }
    form = OrganizationForm(
        request.POST or None, initial=initial, current_type=organization.organization_type
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        country = data.get("country_code")
        update_organization(
            organization,
            actor=request.user,
            official_name=data["official_name"],
            organization_type=data["organization_type"],
            country_code_id=country.pk if country else None,
        )
        messages.success(request, _("Organization updated."))
        return redirect("organizations:detail", pk=organization.pk)
    return render(
        request,
        "organizations/organization_form.html",
        {"form": form, "duplicates": [], "organization": organization},
    )


@operational_permission_required("organizations.change_organization", login_url=LOGIN)
@require_POST
def organization_status(request, pk):
    organization = _organization_or_404(request, pk, "change_organization")
    active = request.POST.get("status") == OrganizationStatus.ACTIVE
    try:
        set_organization_status(organization, actor=request.user, active=active)
    except OrganizationStatusError:
        messages.error(request, _("A merged organization cannot change status."))
    else:
        messages.success(
            request,
            _("Organization activated.") if active else _("Organization deactivated."),
        )
    return redirect("organizations:detail", pk=organization.pk)
