"""UPD-09: the Google Analytics 4 integration (configuration, CSP, rendering).

No request ever reaches Google here: these tests read settings and rendered
HTML only. The browser behaviour (consent, loader, page views, htmx
navigation) is covered with a mocked Google endpoint in
`tests/browser/test_upd09_analytics_browser.py`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from config.settings._analytics import (
    DEFAULT_GA4_MEASUREMENT_ID,
    ga4_content_security_policy,
    measurement_id_from,
)
from scripts.check import (
    BASE_DIR,
    SYNTHETIC_DEPLOY_ENV,
    SYNTHETIC_MIGRATION_ENV_EXTRA,
    synthetic_deploy_env,
)

STREAM_ID = "G-FDXGG6H52P"
MARKER = re.compile(r"<div data-asc-analytics [^>]*>")


def _marker(html: str) -> str | None:
    found = MARKER.findall(html)
    assert len(found) <= 1
    return found[0] if found else None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_the_production_default_is_the_required_stream():
    assert DEFAULT_GA4_MEASUREMENT_ID == STREAM_ID
    assert measurement_id_from(None) == STREAM_ID
    assert measurement_id_from(f"  {STREAM_ID} ") == STREAM_ID
    assert measurement_id_from("") == ""
    assert measurement_id_from("   ") == ""


@pytest.mark.parametrize(
    "value", ["UA-12345-1", "g-fdxgg6h52p", "G-", "G-ABC<script>", "GTM-ABCDE"]
)
def test_a_malformed_measurement_id_is_refused(value):
    with pytest.raises(ImproperlyConfigured):
        measurement_id_from(value)


def test_development_and_tests_never_enable_analytics():
    assert django_settings.ANALYTICS_GA4_MEASUREMENT_ID == ""
    sources = " ".join(" ".join(v) for v in django_settings.CONTENT_SECURITY_POLICY.values())
    assert "google" not in sources


def test_the_csp_addition_is_the_documented_narrow_set():
    from config.settings.base import CONTENT_SECURITY_POLICY as base

    assert ga4_content_security_policy(base, "") is base
    policy = ga4_content_security_policy(base, STREAM_ID)
    assert policy["script-src"] == ("'self'", "https://www.googletagmanager.com")
    assert policy["connect-src"] == (
        "'self'",
        "https://www.googletagmanager.com",
        "https://*.google-analytics.com",
        "https://*.analytics.google.com",
        "https://*.google.com",
    )
    assert policy["img-src"] == (
        "'self'",
        "data:",
        "https://www.googletagmanager.com",
        "https://*.google-analytics.com",
    )
    # Every other directive is untouched (no frame, style or default change).
    for name in set(base) - {"script-src", "connect-src", "img-src"}:
        assert policy[name] == base[name]
    for sources in policy.values():
        for source in sources:
            assert "unsafe" not in source and "nonce" not in source
            assert source not in {"*", "https:", "http:", "https://*"}
            assert source.startswith(("'", "data:", "https://"))
    # Applying it twice adds nothing.
    assert ga4_content_security_policy(policy, STREAM_ID) == policy


_PRINT = """
import json, django
django.setup()
from django.conf import settings
from apps.core.middleware.security_headers import current_policy
print(json.dumps({"id": settings.ANALYTICS_GA4_MEASUREMENT_ID, "csp": current_policy()}))
"""


def _settings_in_subprocess(module: str, **changes) -> subprocess.CompletedProcess:
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in SYNTHETIC_DEPLOY_ENV and key not in SYNTHETIC_MIGRATION_ENV_EXTRA
    }
    env = {**inherited, **synthetic_deploy_env(module)}
    env.pop("ANALYTICS_GA4_MEASUREMENT_ID", None)
    env.update({key: value for key, value in changes.items() if value is not None})
    return subprocess.run(  # noqa: S603 - the project interpreter and a fixed script only
        [sys.executable, "-c", _PRINT],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_production_runs_the_stream_and_staging_stays_off():
    production = _settings_in_subprocess("config.settings.production")
    assert production.returncode == 0, production.stderr[-2000:]
    out = json.loads(production.stdout.strip().splitlines()[-1])
    assert out["id"] == STREAM_ID
    assert "script-src 'self' https://www.googletagmanager.com;" in out["csp"]
    assert "https://*.google-analytics.com" in out["csp"]
    assert "unsafe-inline" not in out["csp"] and "unsafe-eval" not in out["csp"]

    switched_off = _settings_in_subprocess(
        "config.settings.production", ANALYTICS_GA4_MEASUREMENT_ID=""
    )
    assert switched_off.returncode == 0, switched_off.stderr[-2000:]
    out = json.loads(switched_off.stdout.strip().splitlines()[-1])
    assert out["id"] == "" and "google" not in out["csp"]

    staging = _settings_in_subprocess(
        "config.settings.staging", ANALYTICS_GA4_MEASUREMENT_ID=STREAM_ID
    )
    assert staging.returncode == 0, staging.stderr[-2000:]
    out = json.loads(staging.stdout.strip().splitlines()[-1])
    assert out["id"] == "" and "google" not in out["csp"]


def test_a_malformed_production_value_stops_start_up():
    result = _settings_in_subprocess(
        "config.settings.production", ANALYTICS_GA4_MEASUREMENT_ID="UA-1-1"
    )
    assert result.returncode != 0
    assert "ANALYTICS_GA4_MEASUREMENT_ID must look like" in result.stderr
    assert "UA-1-1" not in result.stderr.split("ANALYTICS_GA4_MEASUREMENT_ID must look like")[-1]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

#: The only templates that may include the analytics partial.
ALLOWED_INCLUDERS = {
    "templates/accounts/otp_request.html",
    "templates/privacy/legal_information.html",
}


def test_only_the_two_public_visitor_pages_include_the_partial():
    root = Path(BASE_DIR)
    includers = {
        path.relative_to(root).as_posix()
        for path in (root / "templates").rglob("*.html")
        if '{% include "partials/analytics.html"' in path.read_text(encoding="utf-8")
    }
    assert includers == ALLOWED_INCLUDERS
    # The script itself is referenced by the partial only.
    referencing = {
        path.relative_to(root).as_posix()
        for path in (root / "templates").rglob("*.html")
        if "static 'js/asc-analytics.js'" in path.read_text(encoding="utf-8")
    }
    assert referencing == {"templates/partials/analytics.html"}


@pytest.mark.django_db
def test_nothing_is_rendered_when_analytics_is_off(client):
    for url in (reverse("accounts:otp-request"), reverse("privacy:legal-information")):
        html = client.get(url).content.decode()
        assert _marker(html) is None
        assert "asc-analytics.js" not in html
        assert "data-asc-analytics-banner" not in html
        assert "data-asc-analytics-open" not in html
        assert "googletagmanager" not in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "path", "title"),
    [
        ("accounts:otp-request", "/accounts/start/", "Start registration"),
        ("privacy:legal-information", "/legal/", "Privacy Notice and Terms"),
    ],
)
def test_public_pages_carry_a_fixed_path_and_title_only(client, settings, url_name, path, title):
    settings.ANALYTICS_GA4_MEASUREMENT_ID = STREAM_ID
    # A query string that looks like personal data must never reach the marker.
    query = "?email=someone%40example.test&ref=ASC-ABC123&token=synthetic-token&q=name"
    response = client.get(reverse(url_name) + query)
    assert response.status_code == 200
    html = response.content.decode()
    marker = _marker(html)
    assert marker is not None
    assert f'data-measurement-id="{STREAM_ID}"' in marker
    assert f'data-page-path="{path}"' in marker
    assert f'data-page-title="{title}"' in marker
    for leaked in ("example.test", "ASC-ABC123", "synthetic-token", "?"):
        assert leaked not in marker
    # One loader script, same-origin, no inline script, no Google URL in the page.
    assert html.count("js/asc-analytics.js") == 1
    assert "googletagmanager" not in html
    assert "gtag(" not in html
    assert "data-asc-analytics-banner" in html and "data-asc-analytics-open" in html


@pytest.mark.django_db
def test_the_marker_keeps_the_english_title_in_every_language(client, settings):
    settings.ANALYTICS_GA4_MEASUREMENT_ID = STREAM_ID
    for language in ("fr", "ar"):
        client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = language
        html = client.get(reverse("accounts:otp-request")).content.decode()
        assert f'lang="{language}"' in html
        assert 'data-page-title="Start registration"' in _marker(html)


@pytest.mark.django_db
def test_sensitive_and_staff_pages_never_carry_analytics(client, settings):
    """Authentication, code entry, participant and staff pages stay free of
    the marker, the banner and the loader even with analytics configured."""
    from apps.accounts.models import OperationalUser, OperationalUserStatus
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import (
        TEST_OPERATIONAL_PASSWORD,
        participant_client,
        staff_client,
    )

    settings.ANALYTICS_GA4_MEASUREMENT_ID = STREAM_ID

    def assert_clean(response):
        html = response.content.decode()
        assert _marker(html) is None, response.request["PATH_INFO"]
        assert "asc-analytics.js" not in html
        assert "data-asc-analytics-banner" not in html

    # Code entry needs a pending code (the browser test reaches it); without
    # one it redirects to the start page, so only its own response is read.
    verify = client.get(reverse("accounts:otp-verify"))
    assert verify.status_code in (200, 302)
    assert_clean(verify)
    for url in (reverse("accounts:operational-sign-in"), reverse("accounts:credential-setup")):
        assert_clean(client.get(url, follow=True))

    participant = participant_client(
        resolve_or_create_participant_for_email("upd09-participant@example.test")
    )
    for url in (reverse("registrations:workspace"), reverse("registrations:step-identity")):
        assert_clean(participant.get(url, follow=True))

    admin = OperationalUser.objects.create_user(
        email="upd09-admin@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=True,
    )
    staff = staff_client(admin)
    for url in (
        reverse("registrations:ops-intake-list"),
        reverse("registrations:statistics"),
        reverse("identity:queue"),
    ):
        response = staff.get(url, follow=True)
        assert response.status_code == 200, url
        assert_clean(response)
