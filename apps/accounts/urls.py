from __future__ import annotations

from django.urls import path

from . import administration_views, views

app_name = "accounts"

urlpatterns = [
    path("start/", views.otp_request, name="otp-request"),
    path("start/human-check/", views.human_check_challenge, name="human-check-challenge"),
    path("verify/", views.otp_verify, name="otp-verify"),
    path("sign-out/", views.participant_logout, name="participant-sign-out"),
    path("session/extend/", views.participant_session_extend, name="participant-session-extend"),
    path("ops/sign-in/", views.operational_sign_in, name="operational-sign-in"),
    # Staff sign-in image CAPTCHA: the session-bound image and the CSRF-protected
    # refresh only (django-simple-captcha's own URLconf is not mounted).
    path(
        "ops/sign-in/security-image/<str:key>/",
        views.staff_captcha_image,
        name="staff-captcha-image",
    ),
    path(
        "ops/sign-in/security-image-refresh/",
        views.staff_captcha_refresh,
        name="staff-captcha-refresh",
    ),
    path("ops/sign-out/", views.operational_sign_out, name="operational-sign-out"),
    path(
        "ops/session/extend/",
        views.operational_session_extend,
        name="operational-session-extend",
    ),
    # Staff credential setup and reset links (apps.accounts.administration).
    path(
        "setup/<str:token>/",
        administration_views.credential_setup_start,
        name="credential-setup-start",
    ),
    path("setup/", administration_views.credential_setup, name="credential-setup"),
]
