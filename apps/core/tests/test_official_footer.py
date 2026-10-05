"""The official footer lines (owner decision FOOTER-02, 2026-10-04).

Exactly two lines, with the owner-supplied spelling, in every layout that uses
the shared footer partial and in every language: no prefix, no link, French
(`lang="fr"`, left-to-right inside the Arabic page), the directorate line
smaller and subordinate.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

MINISTRY = "Ministère de l'Économie de la Connaissance, des Start-up et des Micro-entreprise"
DIRECTORATE = "Direction des Systèmes d’Information (DSI)"
FORBIDDEN_PREFIXES = ("Coordination", "Développement", "Conception", "Maintenance")

pytestmark = pytest.mark.django_db


def _footer(html: str) -> str:
    start = html.index('<footer class="asc-footer">')
    return html[start : html.index("</footer>", start)]


def _without_spans(html: str) -> str:
    """Inline `text-nowrap` spans removed: they keep a term on one line and add
    no character."""
    return re.sub(r"</?span[^>]*>", "", html)


def _text(fragment: str) -> list[str]:
    """The visible lines of the institution block, one per paragraph."""
    block = fragment[fragment.index("data-footer-institution") :]
    block = _without_spans(block[: block.index("</div>")].split(">", 1)[1])
    return [line.strip() for line in re.sub(r"<[^>]+>", "\n", block).splitlines() if line.strip()]


def _participant_client() -> Client:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import participant_client

    return participant_client(resolve_or_create_participant_for_email("footer@example.test"))


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
@pytest.mark.parametrize(
    "page",
    ["otp-request", "operational-sign-in", "legal", "workspace", "not-found"],
)
def test_every_shared_footer_shows_exactly_the_two_official_lines(page, language) -> None:
    client = _participant_client() if page == "workspace" else Client()
    url = {
        "otp-request": reverse("accounts:otp-request"),
        "operational-sign-in": reverse("accounts:operational-sign-in"),
        "legal": reverse("privacy:legal-information"),
        "workspace": reverse("registrations:workspace"),
        "not-found": "/no-such-page-anywhere/",
    }[page]
    client.cookies["django_language"] = language
    footer = _footer(client.get(url).content.decode())
    assert _text(footer) == [MINISTRY, DIRECTORATE]
    flat = _without_spans(footer)
    assert flat.count(MINISTRY) == 1 and flat.count(DIRECTORATE) == 1
    assert 'lang="fr" dir="ltr"' in footer
    assert re.search(r'<p class="asc-footer-directorate">\s*' + re.escape(DIRECTORATE), footer)
    assert "<a " not in footer  # A-10's removed links stay removed
    for prefix in FORBIDDEN_PREFIXES:
        assert prefix not in footer


def test_the_directorate_line_is_styled_smaller_than_the_ministry_line() -> None:
    from pathlib import Path

    css = (Path(__file__).resolve().parents[3] / "static" / "css" / "asc-ui.css").read_text(
        encoding="utf-8"
    )
    rule = re.search(r"\.asc-footer-institution \.asc-footer-directorate \{([^}]*)\}", css)
    footer = re.search(r"\.asc-ui \.asc-footer \{([^}]*)\}", css)
    size = float(re.search(r"font-size: ([\d.]+)rem", rule.group(1)).group(1))
    base = float(re.search(r"font-size: ([\d.]+)rem", footer.group(1)).group(1))
    assert size < base
    assert "flex: 1 1 100%" in css and "overflow-wrap: anywhere" in css
