from __future__ import annotations

from django.urls import path

from . import diagnostics_views, views

app_name = "identity"

urlpatterns = [
    path("ops/identity/", views.identity_queue, name="queue"),
    # Ministry NIN service diagnostics (technical operators; no participant data).
    path(
        "ops/integrations/ministry-nin/",
        diagnostics_views.ministry_nin_diagnostics,
        name="nin-diagnostics",
    ),
    path(
        "ops/integrations/ministry-nin/authentication/",
        diagnostics_views.ministry_nin_authentication_check,
        name="nin-diagnostics-authentication",
    ),
    path(
        "ops/integrations/ministry-nin/lookup/",
        diagnostics_views.ministry_nin_lookup_check,
        name="nin-diagnostics-lookup",
    ),
    path("ops/identity/search/", views.identity_search, name="search"),
    path("ops/identity/cases/<uuid:pk>/", views.identity_case, name="case"),
    path("ops/identity/cases/<uuid:pk>/verify/", views.identity_verify, name="verify"),
    path("ops/identity/cases/<uuid:pk>/exception/", views.identity_exception, name="exception"),
    path("ops/identity/cases/<uuid:pk>/return/", views.identity_return, name="return"),
    path(
        "ops/identity/cases/<uuid:pk>/correct-nin/", views.identity_correct_nin, name="correct-nin"
    ),
    path("ops/identity/cases/<uuid:pk>/reject/", views.identity_reject, name="reject"),
    path(
        "ops/identity/cases/<uuid:pk>/evidence/<uuid:document_id>/",
        views.identity_evidence,
        name="evidence",
    ),
    path(
        "ops/identity/registrations/<uuid:pk>/nin-exemption/grant/",
        views.nin_exemption_grant,
        name="nin-exemption-grant",
    ),
    path(
        "ops/identity/registrations/<uuid:pk>/nin-exemption/revoke/",
        views.nin_exemption_revoke,
        name="nin-exemption-revoke",
    ),
    path(
        "register/workspace/registrations/<uuid:pk>/identity-correction/",
        views.identity_correction,
        name="participant-correction",
    ),
]
