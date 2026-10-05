"""Test-only URLconf for the Stage 3 browser evidence (never mounted by any
settings module): it adds views that raise, so the real 500 and 400
handlers render their static-safe pages, and one view that renders the
rarely reachable claim-conflict state through the real template. Every
other URL is the project's own."""

from __future__ import annotations

from django.core.exceptions import SuspiciousOperation
from django.shortcuts import render
from django.urls import include, path


def _server_error(request):
    raise RuntimeError("synthetic failure for the Stage 3 500 evidence")


def _bad_request(request):
    raise SuspiciousOperation("synthetic bad request for the Stage 3 400 evidence")


def _claim_conflict(request):
    return render(request, "invitations/claim_conflict.html")


urlpatterns = [
    path("__stage3__/boom/", _server_error),
    path("__stage3__/bad/", _bad_request),
    path("__stage3__/claim-conflict/", _claim_conflict),
    path("", include("config.urls")),
]
