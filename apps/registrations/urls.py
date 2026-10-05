from __future__ import annotations

from django.urls import path

from . import views

app_name = "registrations"

urlpatterns = [
    path("workspace/", views.workspace, name="workspace"),
    path("register/continue/<uuid:pk>/", views.continue_registration, name="continue"),
    path("register/again/<uuid:pk>/", views.register_again, name="register-again"),
    path(
        "register/invitation/",
        views.start_pending_invitation,
        name="start-pending-invitation",
    ),
    path("register/identity/", views.identity_step, name="step-identity"),
    path("register/contact/", views.contact_step, name="step-contact"),
    path("register/professional/", views.professional_step, name="step-professional"),
    path("register/interests/", views.interests_step, name="step-interests"),
    path("register/review/", views.review_step, name="step-review"),
    path("register/notices/", views.notices_step, name="step-notices"),
    path("register/confirmation/<str:reference>/", views.confirmation, name="confirmation"),
    path("ops/registrations/", views.ops_intake_list, name="ops-intake-list"),
    path(
        "ops/accommodation-requests/",
        views.ops_accommodation_list,
        name="ops-accommodation-list",
    ),
    path(
        "workspace/accommodation/<uuid:pk>/withdraw/",
        views.withdraw_accommodation,
        name="withdraw-accommodation",
    ),
    path("ops/registrations/<uuid:pk>/", views.ops_intake_detail, name="ops-intake-detail"),
]
