"""The conference artwork in the navy sign-in panel, in a real browser.

On the participant start page and the staff sign-in page, in English, French
and Arabic, at the widths where the panel shows and where it does not:

* the supplied lockup is served locally, loads, keeps its aspect ratio, stays
  inside the panel and never overlaps the panel text;
* the stripe motif is gone where the artwork shows;
* the panel names the year and the edition ("ASC 2026 · 5th edition");
* nothing overflows horizontally; where the panel is hidden (tablet, phone)
  the header still names the conference, the year and the edition;
* the sign-in fields stay where they were (no artwork in the form column).

Screenshots go to `var/test_artifacts/asc2026_update/panel_artwork/`.
Chromium against `live_server`; no data is created.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "asc2026_update"
SHOTS = SHOTS / "panel_artwork"
PAGES = {"participant": "/accounts/start/", "operations": "/accounts/ops/sign-in/"}
EDITION = {"en": "5th edition", "fr": "5e édition", "ar": "الدورة الخامسة"}
NATURAL_RATIO = 1639 / 393

MEASURE = """() => {
  const aside = document.querySelector('.asc-auth-aside');
  const shown = !!aside && getComputedStyle(aside).display !== 'none';
  const header = document.querySelector('[data-conference-identity]').innerText;
  const out = {shown, header, overflow: document.documentElement.scrollWidth > innerWidth,
               dir: document.documentElement.dir};
  if (!shown) return out;
  const img = aside.querySelector('.asc-auth-aside-lockup');
  const r = img.getBoundingClientRect(), a = aside.getBoundingClientRect();
  const body = aside.querySelector('.asc-auth-aside-body').getBoundingClientRect();
  return Object.assign(out, {
    loaded: img.complete && img.naturalWidth === 1639 && img.naturalHeight === 393,
    src: new URL(img.currentSrc).pathname,
    sameOrigin: new URL(img.currentSrc).origin === location.origin,
    width: r.width, height: r.height,
    inside: r.left >= a.left - 0.5 && r.right <= a.right + 0.5 && r.top >= a.top - 0.5,
    overlap: r.bottom > body.top + 0.5,
    motif: getComputedStyle(aside.querySelector('.asc-auth-aside-motif')).display,
    kicker: aside.querySelector('.asc-auth-kicker').innerText,
    alt: img.alt,
    formHasArt: !!document.querySelector('.asc-auth-form [data-panel-artwork]'),
  });
}"""


def _open(page, live_server, path, language, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.context.add_cookies(
        [{"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url}]
    )
    page.goto(f"{live_server.url}{path}")
    page.wait_for_load_state("load")
    if width >= 992:
        page.wait_for_function(
            "() => { const i = document.querySelector('.asc-auth-aside-lockup');"
            " return i && i.complete && i.naturalWidth > 0; }"
        )
    return page.evaluate(MEASURE)


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
@pytest.mark.parametrize("name", list(PAGES))
def test_the_artwork_fills_the_panel_without_clipping_or_overlap(live_server, page, name, language):
    SHOTS.mkdir(parents=True, exist_ok=True)
    for width, height, label in ((1280, 800, "desktop"), (1000, 800, None), (1600, 900, None)):
        m = _open(page, live_server, PAGES[name], language, width, height)
        assert m["shown"], (name, language, width)
        assert m["loaded"] and m["sameOrigin"], m
        assert m["src"].endswith("/img/brand/asc-conference-lockup.png"), m["src"]
        assert abs(m["width"] / m["height"] - NATURAL_RATIO) < 0.02, m
        assert m["width"] >= 300, f"artwork too small to read at {width}px: {m['width']}"
        assert m["inside"] and not m["overlap"], m
        assert m["motif"] == "none", m
        assert "2026" in m["kicker"] and EDITION[language] in m["kicker"], m["kicker"]
        assert m["alt"] and "DEEPTECH" in m["alt"], m["alt"]
        assert not m["formHasArt"] and not m["overflow"], m
        assert m["dir"] == ("rtl" if language == "ar" else "ltr")
        if label:
            page.screenshot(path=str(SHOTS / f"{name}-{language}-{label}.png"), full_page=True)


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
@pytest.mark.parametrize("name", list(PAGES))
def test_where_the_panel_is_hidden_the_header_still_names_the_edition(
    live_server, page, name, language
):
    for width, height, label in ((820, 1000, None), (390, 844, "phone")):
        m = _open(page, live_server, PAGES[name], language, width, height)
        assert not m["shown"], (name, language, width)
        assert not m["overflow"], (name, language, width)
        assert "2026" in m["header"] and EDITION[language] in m["header"], m["header"]
        if label:
            page.screenshot(path=str(SHOTS / f"{name}-{language}-{label}.png"), full_page=True)


def test_unrelated_public_pages_keep_their_panel_without_the_artwork(live_server, page):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{live_server.url}/legal/")
    page.wait_for_load_state("load")
    assert page.locator("img[src$='asc-conference-lockup.png']").count() == 0
    # A page with the default panel (an invalid setup link) keeps its stripes.
    page.goto(f"{live_server.url}/accounts/setup/browser-check-0000000000/")
    page.wait_for_load_state("load")
    assert page.locator(".asc-auth-aside").count() == 1
    assert page.locator("[data-panel-artwork]").count() == 0
    assert (
        page.locator(".asc-auth-aside-motif").evaluate("e => getComputedStyle(e).display") != "none"
    )
