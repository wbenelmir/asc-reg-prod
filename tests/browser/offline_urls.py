"""Test-only URLconf for the Phase 4 Prompt 2 emergency-wipe browser tests
(never mounted by any settings module). It serves one UNRELATED service
worker outside `/entry/`, so a test can prove the emergency wipe removes the
ASC `/entry/` registration only and leaves any other worker on the same
origin alone. Every other URL is the project's own."""

from __future__ import annotations

from django.http import HttpResponse
from django.urls import include, path

_UNRELATED_WORKER = (
    "// Synthetic service worker unrelated to ASC entry (test only).\n"
    "self.addEventListener('install', () => self.skipWaiting());\n"
    "self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));\n"
)


def _unrelated_worker(request):
    response = HttpResponse(_UNRELATED_WORKER, content_type="application/javascript")
    response["Cache-Control"] = "no-store"
    return response


urlpatterns = [
    path("__offline_test__/unrelated-sw.js", _unrelated_worker),
    path("", include("config.urls")),
]
