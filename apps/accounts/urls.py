from __future__ import annotations

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("start/", views.otp_request, name="otp-request"),
    path("start/human-check/", views.human_check_challenge, name="human-check-challenge"),
    path("verify/", views.otp_verify, name="otp-verify"),
    path("sign-out/", views.participant_logout, name="participant-sign-out"),
    path("session/extend/", views.participant_session_extend, name="participant-session-extend"),
    path("ops/sign-in/", views.operational_sign_in, name="operational-sign-in"),
    path("ops/sign-out/", views.operational_sign_out, name="operational-sign-out"),
    path(
        "ops/session/extend/",
        views.operational_session_extend,
        name="operational-session-extend",
    ),
]
