from __future__ import annotations

from django.urls import path

from . import views

app_name = "invitations"

urlpatterns = [
    path("invite/<str:token>/", views.invitation_start, name="invitation-start"),
    # The literal "resume" path MUST be registered before the wildcard
    # `<str:token>` pattern below -- otherwise Django's ordered URL
    # resolver would match "/claim/resume/" against `claim/<str:token>/`
    # first, treating "resume" itself as a claim token (Phase 2 Prompt 2
    # V2 correction pass "make the participant claim journey survive OTP
    # authentication" -- discovered via a failing end-to-end test).
    path("claim/resume/", views.on_behalf_claim_resume, name="on-behalf-claim-resume"),
    path("claim/<str:token>/", views.on_behalf_claim, name="on-behalf-claim"),
    path("organizations/workspace/", views.workspace_dashboard, name="workspace-dashboard"),
    path(
        "organizations/workspace/registrations/",
        views.workspace_registrations,
        name="workspace-registrations",
    ),
    path("organizations/search/", views.organization_search, name="organization-search"),
    path(
        "organizations/workspace/campaigns/new/<uuid:organization_id>/",
        views.campaign_create,
        name="campaign-create",
    ),
    path(
        "organizations/workspace/campaigns/<uuid:pk>/",
        views.campaign_detail,
        name="campaign-detail",
    ),
    path(
        "organizations/workspace/campaigns/<uuid:pk>/status/",
        views.campaign_change_status,
        name="campaign-change-status",
    ),
    path(
        "organizations/workspace/campaigns/<uuid:pk>/issue-link/",
        views.campaign_issue_link,
        name="campaign-issue-link",
    ),
    path(
        "organizations/workspace/campaigns/<uuid:pk>/rotate-link/",
        views.campaign_rotate_link,
        name="campaign-rotate-link",
    ),
    path(
        "organizations/workspace/delegations/upload/<uuid:organization_id>/",
        views.delegation_upload,
        name="delegation-upload",
    ),
    path(
        "organizations/workspace/delegations/<uuid:pk>/",
        views.delegation_detail,
        name="delegation-detail",
    ),
    path(
        "organizations/workspace/delegations/<uuid:pk>/apply/",
        views.delegation_apply,
        name="delegation-apply",
    ),
    path(
        "organizations/workspace/on-behalf/new/<uuid:organization_id>/",
        views.on_behalf_create,
        name="on-behalf-create",
    ),
]
