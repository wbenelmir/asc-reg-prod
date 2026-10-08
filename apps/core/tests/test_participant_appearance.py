"""Participant appearance themes: server-side wiring of the shells.

The themes themselves are applied in the browser (`static/js/asc-appearance.js`,
see `tests/browser/test_participant_appearance.py`). These tests pin what the
server renders:

* participant pages mark <html> as participant scope and render exactly one
  Appearance menu: four named radios, in no form, with no inline code;
* staff pages that share the public shell carry neither;
* every page loads the theme script in <head> (before the stylesheets) and the
  theme stylesheet, both same-origin files, so the CSP stays unchanged;
* the new labels are translated in French and Arabic;
* every theme rule is screen-only and scoped to the participant attribute.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import translation

pytestmark = pytest.mark.django_db

ROOT = Path(settings.BASE_DIR)
THEME_VALUES = ["aurora", "fresh", "glass", "color"]
SCOPE = 'data-asc-appearance-scope="participant"'


def _html_tag(content: str) -> str:
    return re.search(r"<html[^>]*>", content).group(0)


def _signed_in_client() -> Client:
    from django.utils import timezone

    from apps.accounts import participant_auth, session_expiry
    from apps.people.models import Person, PersonStatus

    person = Person.objects.create(status=PersonStatus.ACTIVE)
    client = Client()
    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return client


def _assert_participant_page(content: str) -> None:
    assert SCOPE in _html_tag(content)
    assert content.count("data-asc-appearance hidden") == 1, (
        "one menu, hidden until the script runs"
    )
    menu = content[content.index("data-asc-appearance hidden") :]
    menu = menu[: menu.index("</fieldset>")]
    assert re.findall(r'name="asc_appearance" value="(\w+)"', menu) == THEME_VALUES
    assert "<form" not in menu, "the theme radios belong to no form"
    assert 'aria-controls="asc-appearance-menu"' in menu and 'aria-expanded="false"' in menu
    assert " style=" not in menu and not re.search(r"\son\w+=", menu), "no inline code (CSP)"


def test_the_start_and_legal_pages_are_participant_pages(client: Client) -> None:
    for url in (reverse("accounts:otp-request"), "/legal/"):
        response = client.get(url)
        assert response.status_code == 200, url
        _assert_participant_page(response.content.decode())


def test_the_workspace_shell_is_a_participant_page_with_a_compact_menu() -> None:
    response = _signed_in_client().get(reverse("registrations:workspace"))
    assert response.status_code == 200
    content = response.content.decode()
    _assert_participant_page(content)
    assert "asc-appearance asc-appearance-compact" in content


@pytest.mark.parametrize(
    ("url", "template"),
    [
        ("/accounts/ops/sign-in/", "accounts/operational_sign_in.html"),
        # An unknown setup link ends on the "not valid" page (served as 404).
        ("/accounts/setup/browser-check-0000000000/", "accounts/credential_setup_invalid.html"),
    ],
)
def test_staff_pages_on_the_public_shell_are_not_themed(
    client: Client, url: str, template: str
) -> None:
    response = client.get(url, follow=True)
    assert template in [t.name for t in response.templates]
    content = response.content.decode()
    assert "data-asc-appearance-scope" not in _html_tag(content)
    assert "data-asc-appearance" not in content.split("</head>", 1)[1]
    assert 'name="asc_appearance"' not in content


def test_every_page_loads_the_theme_script_early_and_the_stylesheet(client: Client) -> None:
    content = client.get(reverse("accounts:operational-sign-in")).content.decode()
    head = content.split("</head>", 1)[0]
    script = head.index("js/asc-appearance.js")
    assert script < head.index('<link rel="stylesheet"'), "the theme is set before styles apply"
    assert re.search(r'<script src="[^"]*js/asc-appearance\.js[^"]*"></script>', head)
    assert "css/asc-appearance.css" in head
    assert "asc-appearance.js" not in content.split("</head>", 1)[1]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("fr", ["Apparence", "Aurore sombre", "Clair frais", "Verre sombre", "Couleur et carte"]),
        ("ar", ["المظهر", "الشفق الداكن", "فاتح منعش", "زجاجي داكن", "ألوان وبطاقة"]),
        ("en", ["Appearance", "Aurora Dark", "Fresh Light", "Glass Dark", "Color &amp; Card"]),
    ],
)
def test_the_menu_labels_are_translated(client: Client, language: str, expected: list[str]) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    content = client.get(reverse("accounts:otp-request")).content.decode()
    for label in expected:
        assert label in content, (language, label)
    translation.activate("en")


def test_theme_rules_are_screen_only_and_scoped_to_participant_pages() -> None:
    css = (ROOT / "static" / "css" / "asc-appearance.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S).strip()
    assert body.startswith("@media screen {") and body.endswith("}")
    inner = body[len("@media screen {") : -1]
    # Every selector either targets the menu itself (participant pages only)
    # or is scoped by an attribute that only participant pages carry.
    for block in re.findall(r"([^{}@]+)\{[^{}]*\}", inner):
        for selector in (s.strip() for s in block.split(",")):
            if not selector or selector.startswith(("from", "to")):
                continue
            assert (
                selector.startswith(
                    ("[data-asc-theme", "[data-asc-tone", '[dir="rtl"][data-asc-theme')
                )
                or selector.startswith("[data-asc-appearance-scope")
                or ".asc-appearance" in selector
            ), selector


def test_the_script_keeps_only_theme_identifiers_on_the_device() -> None:
    script = (ROOT / "static" / "js" / "asc-appearance.js").read_text(encoding="utf-8")
    assert re.findall(r"localStorage\.(\w+)\(", script) == ["getItem", "setItem"]
    assert "sessionStorage" not in script and "document.cookie" not in script
    assert "fetch(" not in script and "XMLHttpRequest" not in script and "eval(" not in script
    keys = re.search(r"var KEYS = \{([^}]*)\}", script).group(1)
    assert re.findall(r'"([^"]+)"', keys) == [
        "asc2026.appearance.mobile",
        "asc2026.appearance.desktop",
    ]
    assert "(max-width: 767.98px)" in script, "the project's mobile breakpoint"
