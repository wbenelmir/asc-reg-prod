from __future__ import annotations

from django.urls import path

from . import views

app_name = "reviews"

urlpatterns = [
    path("ops/reviews/queue/", views.queue_list, name="queue-list"),
    path(
        "ops/reviews/queue/decisions/export/",
        views.decision_workbook_export,
        name="decision-workbook-export",
    ),
    path(
        "ops/reviews/queue/decisions/import/",
        views.decision_workbook_import,
        name="decision-workbook-import",
    ),
    path(
        "ops/reviews/queue/decisions/imports/<uuid:pk>/",
        views.decision_import_detail,
        name="decision-import-detail",
    ),
    path(
        "ops/reviews/queue/decisions/imports/<uuid:pk>/apply/",
        views.decision_import_apply,
        name="decision-import-apply",
    ),
    path(
        "ops/reviews/queue/decisions/imports/<uuid:pk>/errors.csv",
        views.decision_import_report,
        name="decision-import-report",
    ),
    path("ops/reviews/cases/<uuid:pk>/", views.case_detail, name="case-detail"),
    path("ops/reviews/cases/<uuid:pk>/assign/", views.case_assign, name="case-assign"),
    path(
        "ops/reviews/cases/<uuid:pk>/status/", views.case_change_status, name="case-change-status"
    ),
    path(
        "ops/reviews/cases/<uuid:pk>/checklist/",
        views.case_checklist_result,
        name="case-checklist-result",
    ),
    path("ops/reviews/cases/<uuid:pk>/notes/", views.case_add_note, name="case-add-note"),
    path(
        "ops/reviews/cases/<uuid:pk>/duplicate/resolve/",
        views.case_resolve_duplicate,
        name="case-resolve-duplicate",
    ),
    path(
        "ops/reviews/cases/<uuid:pk>/information-requests/new/",
        views.case_create_information_request,
        name="case-create-information-request",
    ),
    path(
        "ops/reviews/information-requests/<uuid:pk>/",
        views.information_request_detail,
        name="information-request-detail",
    ),
    path(
        "ops/reviews/information-requests/<uuid:pk>/send/",
        views.information_request_send,
        name="information-request-send",
    ),
    path(
        "ops/reviews/information-requests/<uuid:pk>/cancel/",
        views.information_request_cancel,
        name="information-request-cancel",
    ),
    path(
        "ops/reviews/information-requests/<uuid:pk>/close/",
        views.information_request_close,
        name="information-request-close",
    ),
    path(
        "ops/reviews/cases/<uuid:pk>/decision/not-approved/",
        views.case_decision,
        name="case-decision",
    ),
    path(
        "ops/reviews/cases/<uuid:pk>/decision/approved/",
        views.case_decision_approve,
        name="case-decision-approve",
    ),
    path(
        "ops/reviews/registrations/<uuid:pk>/reopen/",
        views.registration_reopen,
        name="registration-reopen",
    ),
    path(
        "ops/reviews/registrations/<uuid:pk>/cancel/",
        views.registration_cancel,
        name="registration-cancel",
    ),
    path(
        "register/workspace/requests/<uuid:pk>/",
        views.my_information_request,
        name="my-information-request",
    ),
    path(
        "register/workspace/registrations/<uuid:pk>/withdraw/",
        views.my_registration_withdraw,
        name="my-registration-withdraw",
    ),
]
