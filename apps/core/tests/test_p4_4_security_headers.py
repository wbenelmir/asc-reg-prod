"""P4-4: enforced Content Security Policy, Permissions-Policy, forwarded-scheme
hygiene, HSTS rollout validation and session-row lifetime. Synthetic data only."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse

from apps.core.middleware.security_headers import (
    CSP_HEADER,
    ContentSecurityPolicyMiddleware,
    ForwardedProtoGuardMiddleware,
    build_policy,
    current_policy,
)
from config.settings.validation import (
    DeploymentConfigurationError,
    validate_deployment_configuration,
)
from tests.foundation.test_static_validation import _valid_snapshot

ROOT = Path(settings.BASE_DIR)
TEMPLATE_DIRS = [ROOT / "templates"]
PROJECT_JS = sorted((ROOT / "static" / "js").glob("*.js")) + [
    ROOT / "templates" / "entry" / "service_worker.js",
    ROOT / "templates" / "entry" / "service_worker_disabled.js",
]


def _directives(policy: str) -> dict[str, list[str]]:
    result = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            result[tokens[0]] = tokens[1:]
    return result


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


def test_the_policy_is_same_origin_with_no_unsafe_source() -> None:
    directives = _directives(current_policy())
    for name in ("script-src", "style-src", "font-src", "connect-src", "worker-src"):
        assert directives[name] == ["'self'"], name
    assert directives["img-src"] == ["'self'", "data:"]
    for name in ("object-src", "frame-src", "base-uri", "frame-ancestors", "media-src"):
        assert directives[name] == ["'none'"], name
    assert directives["form-action"] == ["'self'"]
    text = current_policy()
    for forbidden in (
        "'unsafe-inline'",
        "'unsafe-eval'",
        "'unsafe-hashes'",
        "*",
        "http:",
        "https:",
    ):
        assert forbidden not in text.split(), forbidden
    assert "nonce-" not in text and "sha256-" not in text


def test_an_empty_directive_is_refused_rather_than_dropped() -> None:
    with pytest.raises(ValueError):
        build_policy({"script-src": ()}, upgrade=False)


def test_https_environments_upgrade_insecure_requests() -> None:
    assert "upgrade-insecure-requests" in build_policy({"default-src": ("'self'",)}, upgrade=True)
    assert "upgrade-insecure-requests" not in current_policy()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    ["accounts:otp-request", "privacy:legal-information", "accounts:operational-sign-in"],
)
def test_pages_carry_the_enforced_policy_and_permissions_policy(client, url_name) -> None:
    response = client.get(reverse(url_name))
    assert response.status_code == 200
    assert response.headers[CSP_HEADER] == current_policy()
    assert "Content-Security-Policy-Report-Only" not in response.headers
    permissions = response.headers["Permissions-Policy"]
    assert "camera=()" in permissions and "geolocation=()" in permissions
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.django_db
def test_error_json_and_worker_responses_carry_the_policy_too(client) -> None:
    missing = client.get("/no-such-page-p4-4/")
    assert missing.status_code == 404
    assert missing.headers[CSP_HEADER] == current_policy()
    challenge = client.get(reverse("accounts:human-check-challenge"))
    assert challenge.headers[CSP_HEADER] == current_policy()
    worker = client.get(reverse("entry:service-worker"))
    assert worker.headers[CSP_HEADER] == current_policy()


def test_a_view_that_sets_its_own_policy_keeps_it() -> None:
    def view(request):
        response = HttpResponse("x")
        response.headers[CSP_HEADER] = "default-src 'none'"
        return response

    response = ContentSecurityPolicyMiddleware(view)(RequestFactory().get("/"))
    assert response.headers[CSP_HEADER] == "default-src 'none'"


def test_the_policy_middleware_runs_just_after_security_middleware() -> None:
    order = settings.MIDDLEWARE
    assert order[0] == "apps.core.middleware.security_headers.ForwardedProtoGuardMiddleware"
    assert order[1] == "django.middleware.security.SecurityMiddleware"
    assert order[2] == "apps.core.middleware.security_headers.ContentSecurityPolicyMiddleware"


# ---------------------------------------------------------------------------
# Nothing in the project's own markup or scripts needs an unsafe source
# ---------------------------------------------------------------------------

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/json\")[^>]*>", re.I)
_EVENT_HANDLER = re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.I)
_STYLE_ATTRIBUTE = re.compile(r"<[a-z][^>]*\sstyle\s*=", re.I)
_STYLE_ELEMENT = re.compile(r"<style[\s>]", re.I)
_JAVASCRIPT_URL = re.compile(r"""(?:href|src|action)\s*=\s*["']\s*javascript:""", re.I)


def _templates():
    for directory in TEMPLATE_DIRS:
        yield from sorted(directory.rglob("*.html"))


def test_no_template_has_inline_script_handler_style_or_javascript_url() -> None:
    offenders = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for label, pattern in (
            ("inline script", _INLINE_SCRIPT),
            ("event handler", _EVENT_HANDLER),
            ("style attribute", _STYLE_ATTRIBUTE),
            ("style element", _STYLE_ELEMENT),
            ("javascript: URL", _JAVASCRIPT_URL),
        ):
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {label}")
    assert offenders == []


def test_the_guard_detects_what_it_forbids() -> None:
    assert _INLINE_SCRIPT.search("<script>alert(1)</script>")
    assert not _INLINE_SCRIPT.search('<script src="/static/x.js"></script>')
    assert not _INLINE_SCRIPT.search('<script id="c" type="application/json">{}</script>')
    assert _EVENT_HANDLER.search('<select onchange="x()">')
    assert _STYLE_ATTRIBUTE.search('<span style="--p: 1%">')
    assert _JAVASCRIPT_URL.search('<a href="javascript:void(0)">')


def test_project_scripts_never_evaluate_strings_or_write_style_attributes() -> None:
    forbidden = (
        re.compile(r"\beval\s*\("),
        re.compile(r"\bnew\s+Function\s*\("),
        re.compile(r"setAttribute\(\s*[\"']style[\"']"),
        re.compile(r"set(?:Timeout|Interval)\(\s*[\"']"),
        re.compile(r"""innerHTML\s*=.*\son[a-z]+\s*="""),
    )
    offenders = [
        f"{path.relative_to(ROOT)}: {pattern.pattern}"
        for path in PROJECT_JS
        for pattern in forbidden
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.django_db
def test_rendered_public_pages_have_no_inline_code(client) -> None:
    pages = [
        client.get(reverse("accounts:otp-request")),
        client.get(reverse("privacy:legal-information")),
        client.get("/no-such-page-p4-4/"),
        client.post(reverse("accounts:otp-request"), {"email": "p44@example.com"}),
    ]
    for response in pages:
        html = response.content.decode("utf-8")
        assert not _INLINE_SCRIPT.search(html), response.request["PATH_INFO"]
        assert not _EVENT_HANDLER.search(html), response.request["PATH_INFO"]
        assert not _STYLE_ATTRIBUTE.search(html), response.request["PATH_INFO"]
        assert "js/asc-page.js" in html


@pytest.mark.django_db
def test_htmx_never_copies_a_style_attribute_during_a_swap(client) -> None:
    """P4-4 gate attempt 1: htmx 4 settles a swap by copying the old element's
    attributes onto the new one with setAttribute, so a style attribute left
    by script was re-applied as an inline style and refused by the CSP. The
    config is read once, from the first page's meta tag."""
    import json

    html = client.get(reverse("accounts:otp-request")).content.decode("utf-8")
    match = re.search(r"<meta name=\"htmx-config\" content='([^']+)'>", html)
    assert match
    ignored = json.loads(match.group(1))["morphIgnore"]
    assert "style" in ignored
    assert "data-htmx-powered" in ignored  # htmx's own default is kept
    assert html.index('name="htmx-config"') < html.index("vendor/htmx/htmx.min.js")


# ---------------------------------------------------------------------------
# Forwarded scheme: only a trusted proxy may say the request used HTTPS
# ---------------------------------------------------------------------------


def _is_secure_seen_by_django(remote_addr: str):
    seen = {}

    def view(request):
        seen["secure"] = request.is_secure()
        return HttpResponse("ok")

    request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https", REMOTE_ADDR=remote_addr)
    ForwardedProtoGuardMiddleware(view)(request)
    return seen["secure"], "HTTP_X_FORWARDED_PROTO" in request.META


@override_settings(
    TRUSTED_PROXY_FORWARDING_ENABLED=True,
    TRUSTED_PROXY_CIDRS=["10.20.0.0/16"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
def test_a_trusted_proxy_can_report_https() -> None:
    assert _is_secure_seen_by_django("10.20.3.4") == (True, True)


@override_settings(
    TRUSTED_PROXY_FORWARDING_ENABLED=True,
    TRUSTED_PROXY_CIDRS=["10.20.0.0/16"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
@pytest.mark.parametrize("peer", ["203.0.113.9", "not-an-address", ""])
def test_an_untrusted_peer_cannot_spoof_https(peer) -> None:
    assert _is_secure_seen_by_django(peer) == (False, False)


@override_settings(
    TRUSTED_PROXY_FORWARDING_ENABLED=False,
    TRUSTED_PROXY_CIDRS=["10.20.0.0/16"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
def test_forwarding_disabled_ignores_even_a_listed_peer() -> None:
    assert _is_secure_seen_by_django("10.20.3.4") == (False, False)


def test_local_and_test_settings_do_not_trust_a_forwarded_scheme() -> None:
    assert settings.SECURE_PROXY_SSL_HEADER is None


# ---------------------------------------------------------------------------
# HSTS rollout and CIDR validation (staging/production static validation)
# ---------------------------------------------------------------------------


def _validate(**changes):
    validate_deployment_configuration(dataclasses.replace(_valid_snapshot(), **changes))


def test_the_approved_hsts_values_validate() -> None:
    _validate(hsts_seconds=31536000, hsts_include_subdomains=True, hsts_preload=True)


def test_a_staged_hsts_rollout_without_preload_validates() -> None:
    _validate(hsts_seconds=300, hsts_include_subdomains=False, hsts_preload=False)


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"hsts_seconds": 0}, "positive number"),
        (
            {"hsts_seconds": 31536000, "hsts_include_subdomains": False, "hsts_preload": True},
            "requires DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
        ),
        (
            {"hsts_seconds": 86400, "hsts_include_subdomains": True, "hsts_preload": True},
            "at least 31536000",
        ),
        (
            {"trusted_proxy_forwarding_enabled": True, "trusted_proxy_cidrs": ["10.0.0.0/8", "x"]},
            "TRUSTED_PROXY_CIDRS entry 2",
        ),
    ],
)
def test_unsafe_transport_configuration_is_refused(changes, fragment) -> None:
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        _validate(**changes)
    assert fragment in str(excinfo.value)
    assert "10.0.0.0/8" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Session rows are not kept for Django's two-week default
# ---------------------------------------------------------------------------


def test_session_rows_expire_with_the_longest_approved_session() -> None:
    assert settings.SESSION_COOKIE_AGE == max(
        settings.PARTICIPANT_SESSION_ABSOLUTE_SECONDS,
        settings.OPERATIONAL_SESSION_ABSOLUTE_SECONDS,
        settings.ENTRY_DEVICE_SESSION_SECONDS,
    )
    assert settings.SESSION_COOKIE_AGE < 14 * 24 * 60 * 60
