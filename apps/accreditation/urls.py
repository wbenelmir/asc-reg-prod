from __future__ import annotations

from django.urls import path

from . import attendance_views, views

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
    # Attendance entitlements (apps.accreditation.attendance).
    path("ops/attendance/", attendance_views.attendance_overview, name="attendance-overview"),
    path(
        "ops/attendance/<uuid:event_pk>/",
        attendance_views.attendance_policy,
        name="attendance-policy",
    ),
    path(
        "ops/attendance/<uuid:event_pk>/activate/",
        attendance_views.attendance_activate,
        name="attendance-activate",
    ),
    path(
        "ops/attendance/<uuid:event_pk>/deactivate/",
        attendance_views.attendance_deactivate,
        name="attendance-deactivate",
    ),
    path(
        "ops/attendance/<uuid:event_pk>/registrations/",
        attendance_views.attendance_worklist,
        name="attendance-worklist",
    ),
    path(
        "ops/attendance/registrations/<uuid:pk>/change/",
        attendance_views.attendance_change,
        name="attendance-change",
    ),
]
