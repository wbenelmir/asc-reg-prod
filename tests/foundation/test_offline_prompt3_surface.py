"""Prompt 3 shell localization, accessibility structure and cache transition."""

import hashlib
from html.parser import HTMLParser

import pytest
from django.template.loader import render_to_string
from django.utils.translation import override

from apps.entry.offline_contract import client_config
from apps.entry.offline_views import _cache_version, _precache_urls, shell_strings


class _ShellControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.controls = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.forms.append(dict(attrs))
        if tag in {"input", "select", "textarea", "button"}:
            self.controls.append(dict(attrs))


def test_prompt3_invalidates_the_approved_prompt2_local_cache():
    old_version = hashlib.sha256("\n".join(_precache_urls()).encode()).hexdigest()[:16]
    assert _cache_version() != old_version


@pytest.mark.parametrize(
    "language,direction,label",
    [
        ("en", "ltr", "Verify offline"),
        ("fr", "ltr", "Vérifier hors ligne"),
        ("ar", "rtl", "التحقق دون اتصال"),
    ],
)
def test_shell_keeps_localized_controls_live_regions_and_brand(language, direction, label):
    with override(language):
        html = render_to_string(
            "entry/offline_shell.html",
            {
                "enabled": True,
                "offline_config": client_config(),
                "offline_strings": shell_strings(),
            },
        )
    assert f'lang="{language}" dir="{direction}"' in html
    assert label in html
    assert 'aria-live="polite"' in html
    assert 'id="offline-qr"' in html
    assert 'for="offline-qr"' in html
    assert "asc-logo.svg" in html
    if language == "ar":
        assert "bootstrap.rtl.min.css" in html and "thmanyahsans-Regular.woff2" in html
    assert "csrfmiddlewaretoken" not in html
    markup = _ShellControls()
    markup.feed(html)
    assert len(markup.forms) == 1
    assert markup.forms[0]["id"] == "offline-verify-form"
    assert "hidden" in markup.forms[0]
    assert "action" not in markup.forms[0] and "method" not in markup.forms[0]
    # Local inputs must never become a native GET/POST credential payload.
    assert all("name" not in control and "formaction" not in control for control in markup.controls)
    qr = next(control for control in markup.controls if control.get("id") == "offline-qr")
    assert "value" not in qr and qr["autocomplete"] == "off"
