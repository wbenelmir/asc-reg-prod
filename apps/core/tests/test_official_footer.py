"""The official institutional footer is empty (owner, version 1.1 UI work
package 01, 2026-10-05: "The institutional footer remains empty").

That supersedes FOOTER-02 (2026-10-04, two French institutional lines). The
shared footer still renders, in every layout and language, with the platform
name and no institution block, no link, no contact and no social item (A-10).
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

FORMER_LINES = (
    "Ministère de l'Économie de la Connaissance",
    "Direction des Systèmes d’Information",
)

pytestmark = pytest.mark.django_db


def _footer(html: str) -> str:
    start = html.index('<footer class="asc-footer">')
    return html[start : html.index("</footer>", start)]


def _participant_client() -> Client:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import participant_client

    return participant_client(resolve_or_create_participant_for_email("footer@example.test"))


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
@pytest.mark.parametrize(
    "page",
    ["otp-request", "operational-sign-in", "legal", "workspace", "not-found"],
)
def test_every_shared_footer_renders_without_an_institution_block(page, language) -> None:
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
    assert "data-footer-institution" not in footer
    for line in FORMER_LINES:
        assert line not in footer
    assert "<a " not in footer  # A-10's removed links stay removed


def test_the_shared_partial_is_empty() -> None:
    from django.template.loader import render_to_string

    assert render_to_string("partials/footer_official.html").strip() == ""
