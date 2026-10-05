"""Root URL configuration.

Phase 1 Prompt 4 adds the universal registration vertical slice (public
OTP access, registration wizard, participant workspace, minimum
operations intake). Public, workspace and operations routes are added
from Prompt 4 onward. The Django admin is mounted only when it is present
in INSTALLED_APPS, which `config/settings/local.py` alone arranges
(accepted plan §10).
"""

from django.apps import apps
from django.shortcuts import redirect
from django.urls import include, path

from apps.core import views as core_views


def home(request):
    return redirect("accounts:otp-request")


urlpatterns = [
    path("healthz", core_views.liveness, name="health-liveness"),
    path("readyz", core_views.readiness, name="health-readiness"),
    path("robots.txt", core_views.robots_txt, name="robots-txt"),
    path("favicon.ico", core_views.favicon_ico, name="favicon-ico"),
    path("i18n/setlang/", core_views.set_language, name="set-language"),
    path("", home, name="home"),
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.registrations.urls")),
    path("", include("apps.documents.urls")),
    path("", include("apps.invitations.urls")),
    path("", include("apps.reviews.urls")),
    path("", include("apps.people.urls")),
    path("", include("apps.accreditation.urls")),
    path("", include("apps.communications.urls")),
    path("", include("apps.exports.urls")),
    path("", include("apps.badges.urls")),
    path("", include("apps.entry.urls")),
    path("", include("apps.events.urls")),
    path("", include("apps.privacy.urls")),
]

if apps.is_installed("django.contrib.admin"):
    from django.contrib import admin

    urlpatterns += [path("admin/", admin.site.urls)]
