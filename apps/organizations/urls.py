from __future__ import annotations

from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    # UPD-06: organization management (Organizations / Organization Workspace).
    path("ops/organizations/", views.organization_list, name="list"),
    path("ops/organizations/new/", views.organization_create, name="create"),
    path("ops/organizations/<uuid:pk>/", views.organization_detail, name="detail"),
    path("ops/organizations/<uuid:pk>/edit/", views.organization_edit, name="edit"),
    path("ops/organizations/<uuid:pk>/status/", views.organization_status, name="status"),
]
