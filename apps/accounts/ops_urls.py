"""Staff account administration routes, under `/ops/` like every operations
area (the operational sign-in `next` allow-list covers them)."""

from __future__ import annotations

from django.urls import path

from . import administration_views as views

app_name = "staff_accounts"

urlpatterns = [
    path("ops/staff-accounts/", views.staff_account_list, name="list"),
    path("ops/staff-accounts/new/", views.staff_account_create, name="create"),
    path("ops/staff-accounts/<uuid:pk>/", views.staff_account_detail, name="detail"),
    path("ops/staff-accounts/<uuid:pk>/update/", views.staff_account_update, name="update"),
    path("ops/staff-accounts/<uuid:pk>/status/", views.staff_account_status, name="status"),
    path(
        "ops/staff-accounts/<uuid:pk>/credential-link/",
        views.staff_account_credential_link,
        name="credential-link",
    ),
    path("ops/staff-accounts/<uuid:pk>/roles/grant/", views.staff_account_grant, name="grant"),
    path("ops/staff-accounts/<uuid:pk>/roles/revoke/", views.staff_account_revoke, name="revoke"),
]
