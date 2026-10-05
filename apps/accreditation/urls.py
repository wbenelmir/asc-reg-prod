from __future__ import annotations

from django.urls import path

from . import views

app_name = "accreditation"

urlpatterns = [
    path(
        "ops/accreditation/registrations/<uuid:pk>/",
        views.registration_accreditation_detail,
        name="registration-detail",
    ),
    path(
        "ops/accreditation/registrations/<uuid:pk>/<str:kind>/assign/",
        views.assignment_assign,
        name="assignment-assign",
    ),
    path(
        "ops/accreditation/registrations/<uuid:pk>/<str:kind>/<uuid:assignment_pk>/revoke/",
        views.assignment_revoke,
        name="assignment-revoke",
    ),
    path(
        "ops/accreditation/registrations/<uuid:pk>/<str:kind>/<uuid:assignment_pk>/change/",
        views.assignment_change,
        name="assignment-change",
    ),
    path("ops/accreditation/bulk/preview/", views.bulk_preview, name="bulk-preview"),
    path("ops/accreditation/bulk/<uuid:pk>/execute/", views.bulk_execute, name="bulk-execute"),
]
