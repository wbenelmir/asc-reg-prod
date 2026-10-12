"""UPD-09: Google Analytics 4 in a real browser, with Google MOCKED.

Every request to a non-local host is intercepted: the Google tag loader is
answered with a small stand-in script and collection requests are recorded
and answered with 204. Nothing reaches Google. The stand-in follows the
documented contract the integration relies on:

* it processes `dataLayer` commands (`set`, `config`, `event`) and sends one
  collection request per event, with the page values set on the page;
* like GA4 Enhanced measurement, it sends its own `page_view` with the REAL
  address and title on every history change (pushState, popstate), even with
  `send_page_view: false`;
* it sends nothing while `window["ga-disable-<ID>"]` is true (Google's
  documented opt-out).

The analytics settings of production are applied to the live server for
these tests only (measurement ID and the CSP built by
`config/settings/_analytics.py`), so the enforced CSP is the production one.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from config.settings._analytics import ga4_content_security_policy

pytestmark = pytest.mark.django_db(transaction=True)

STREAM_ID = "G-FDXGG6H52P"
CONSENT_KEY = "asc2026.analytics.consent"

GTAG_STAND_IN = r"""
(function () {
  var id = new URL(document.currentScript.src).searchParams.get("id");
  window.__gaStandInLoads = (window.__gaStandInLoads || 0) + 1;
  var fields = {};
  function send(name, params) {
    if (window["ga-disable-" + id] === true) return;
    var pick = function (key, fallback) {
      if (params[key] !== undefined) return params[key];
      return fields[key] !== undefined ? fields[key] : fallback;
    };
    var query = "v=2&tid=" + encodeURIComponent(id) + "&en=" + encodeURIComponent(name) +
      "&dl=" + encodeURIComponent(pick("page_location", location.href)) +
      "&dt=" + encodeURIComponent(pick("page_title", document.title)) +
      "&dr=" + encodeURIComponent(pick("page_referrer", document.referrer));
    fetch("https://region1.google-analytics.com/g/collect?" + query,
      { method: "POST", mode: "no-cors", keepalive: true });
  }
  function handle(args) {
    var a = Array.prototype.slice.call(args);
    if (a[0] === "set" && a[1] && typeof a[1] === "object") Object.assign(fields, a[1]);
    else if (a[0] === "event") send(a[1], a[2] || {});
    else if (a[0] === "config" && !(a[2] && a[2].send_page_view === false)) send("page_view", {});
  }
  var layer = window.dataLayer = window.dataLayer || [];
  layer.forEach(handle);
  var push = layer.push;
  layer.push = function () {
    for (var i = 0; i < arguments.length; i++) handle(arguments[i]);
    return push.apply(layer, arguments);
  };
  // Enhanced measurement: history changes are page views with the REAL address.
  var pushState = history.pushState;
  history.pushState = function () {
    var result = pushState.apply(history, arguments);
    send("page_view", { page_location: location.href, page_title: document.title });
    return result;
  };
  window.addEventListener("popstate", function () {
    send("page_view", { page_location: location.href, page_title: document.title });
  });
})();
"""


class GoogleMock:
    """Records loader and collection requests; aborts any other external request."""

    def __init__(self, page, live_server_url: str) -> None:
        self.loads: list[str] = []
        self.hits: list[dict[str, str]] = []
        self.blocked: list[str] = []
        self.local = urlsplit(live_server_url).netloc
        page.context.route(re.compile(r"^https?://.*"), self._handle)

    def _handle(self, route) -> None:
        url = route.request.url
        parts = urlsplit(url)
        if parts.netloc == self.local:
            route.continue_()
        elif parts.netloc == "www.googletagmanager.com" and parts.path == "/gtag/js":
            self.loads.append(url)
            route.fulfill(status=200, content_type="text/javascript", body=GTAG_STAND_IN)
        elif parts.netloc.endswith(".google-analytics.com") and parts.path == "/g/collect":
            self.hits.append({key: values[0] for key, values in parse_qs(parts.query).items()})
            route.fulfill(status=204, body="")
        else:
            self.blocked.append(url)
            route.abort()

    def page_views(self) -> list[dict[str, str]]:
        return [hit for hit in self.hits if hit.get("en") == "page_view"]


@pytest.fixture
def production_analytics(settings):
    from config.settings.base import CONTENT_SECURITY_POLICY

    settings.ANALYTICS_GA4_MEASUREMENT_ID = STREAM_ID
    settings.CONTENT_SECURITY_POLICY = ga4_content_security_policy(
        CONTENT_SECURITY_POLICY, STREAM_ID
    )


def _settle(page) -> None:
    page.wait_for_load_state("load")
    page.wait_for_timeout(400)


def _granted_before_load(page) -> None:
    page.add_init_script(
        f"try {{ localStorage.setItem('{CONSENT_KEY}', 'granted'); }} catch (e) {{}}"
    )


def test_without_a_choice_nothing_is_requested_and_declining_keeps_it_so(
    live_server, page, seeded_open_event, production_analytics
) -> None:
    google = GoogleMock(page, live_server.url)
    response = page.goto(f"{live_server.url}/accounts/start/")
    policy = response.headers["content-security-policy"]
    assert "script-src 'self' https://www.googletagmanager.com;" in policy
    assert "unsafe" not in policy
    banner = page.locator("[data-asc-analytics-banner]")
    expect(banner).to_be_visible()
    # The withdrawal control is usable from the first load, not only after a swap.
    expect(page.locator("[data-asc-analytics-open]")).to_be_visible()
    _settle(page)
    assert google.loads == [] and google.hits == []

    page.get_by_role("button", name="Decline analytics").click()
    expect(banner).to_be_hidden()
    assert page.evaluate(f"() => localStorage.getItem('{CONSENT_KEY}')") == "denied"
    page.reload()
    _settle(page)
    expect(page.locator("[data-asc-analytics-banner]")).to_be_hidden()
    assert google.loads == [] and google.hits == []
    assert page.evaluate("() => window.dataLayer === undefined")


def test_accepting_loads_the_tag_once_and_sends_one_sanitised_page_view(
    live_server, page, seeded_open_event, production_analytics
) -> None:
    google = GoogleMock(page, live_server.url)
    # A query string that looks like personal data must never be measured.
    page.goto(
        f"{live_server.url}/accounts/start/?email=someone%40example.test&ref=ASC-ABC123&token=t0k3n"
    )
    _settle(page)
    page.get_by_role("button", name="Accept analytics").click()
    expect(page.locator("[data-asc-analytics-banner]")).to_be_hidden()
    _settle(page)
    assert len(google.loads) == 1
    assert parse_qs(urlsplit(google.loads[0]).query) == {"id": [STREAM_ID]}
    views = google.page_views()
    assert len(views) == 1
    view = views[0]
    assert view["tid"] == STREAM_ID
    assert view["dl"] == f"{live_server.url}/accounts/start/"
    assert view["dt"] == "Start registration"
    assert view.get("dr", "") == ""
    for hit in google.hits:
        flat = " ".join(hit.values())
        for leaked in ("example.test", "ASC-ABC123", "t0k3n", "?"):
            assert leaked not in flat
    config = page.evaluate(
        "() => window.dataLayer.map(a => Array.prototype.slice.call(a))"
        ".filter(a => a[0] === 'config')"
    )
    assert config == [
        [
            "config",
            STREAM_ID,
            {
                "send_page_view": False,
                "allow_google_signals": False,
                "allow_ad_personalization_signals": False,
            },
        ]
    ]

    # The choice is remembered: the next load counts one view, without the banner.
    page.reload()
    _settle(page)
    expect(page.locator("[data-asc-analytics-banner]")).to_be_hidden()
    assert len(google.loads) == 2 and len(google.page_views()) == 2
    assert google.blocked == []


def test_withdrawing_consent_stops_hits_and_removes_the_cookies(
    live_server, page, seeded_open_event, production_analytics
) -> None:
    google = GoogleMock(page, live_server.url)
    _granted_before_load(page)
    page.goto(f"{live_server.url}/legal/")
    _settle(page)
    assert len(google.page_views()) == 1
    assert google.page_views()[0]["dl"] == f"{live_server.url}/legal/"
    assert google.page_views()[0]["dt"] == "Privacy Notice and Terms"
    page.evaluate(
        "() => { document.cookie = '_ga=GA1.1.1.1; path=/';"
        " document.cookie = '_ga_TEST=GS1; path=/'; }"
    )

    page.locator("[data-asc-analytics-open]").click()
    expect(page.locator("[data-asc-analytics-banner]")).to_be_visible()
    page.get_by_role("button", name="Decline analytics").click()
    assert page.evaluate(f"() => window['ga-disable-{STREAM_ID}']") is True
    assert "_ga" not in page.evaluate("() => document.cookie")
    consent_updates = page.evaluate(
        "() => window.dataLayer.map(a => Array.prototype.slice.call(a))"
        ".filter(a => a[0] === 'consent' && a[1] === 'update').map(a => a[2])"
    )
    assert consent_updates == [{"analytics_storage": "denied"}]
    before = len(google.hits)
    page.evaluate("() => window.gtag('event', 'page_view', {})")
    page.wait_for_timeout(300)
    assert len(google.hits) == before

    # Accepting again on the same page restores consent without a second view.
    page.locator("[data-asc-analytics-open]").click()
    page.get_by_role("button", name="Accept analytics").click()
    page.wait_for_timeout(300)
    consent_updates = page.evaluate(
        "() => window.dataLayer.map(a => Array.prototype.slice.call(a))"
        ".filter(a => a[0] === 'consent' && a[1] === 'update').map(a => a[2])"
    )
    assert consent_updates[-1] == {"analytics_storage": "granted"}
    assert len(google.page_views()) == 1
    assert len(google.loads) == 1


def test_htmx_navigation_never_double_counts_or_measures_another_page(
    live_server, page, seeded_open_event, seeded_legal_notices, production_analytics
) -> None:
    """The public pages link to each other inside the boosted main content.
    Left to htmx, the second page would be counted twice (this script's view
    plus the history page view) in the first page's document. Instead:
    a boosted GET becomes a normal page load counted once by its own tag;
    any other htmx request or history change seals the loaded tag."""
    google = GoogleMock(page, live_server.url)
    _granted_before_load(page)
    page.goto(f"{live_server.url}/accounts/start/")
    _settle(page)
    assert len(google.page_views()) == 1

    # Boosted GET link (main content) to the legal page: a full page load.
    page.evaluate("() => { window.__sameDocument = true; }")
    page.locator("[data-legal-link] a").click()
    page.wait_for_url("**/legal/")
    _settle(page)
    assert page.evaluate("() => window.__sameDocument === undefined")
    assert [view["dl"] for view in google.page_views()] == [
        f"{live_server.url}/accounts/start/",
        f"{live_server.url}/legal/",
    ]
    assert len(google.loads) == 2

    # In-page anchors and their history entries add no page view.
    page.locator(".asc-legal-toc a").first.click()
    _settle(page)
    page.go_back()
    _settle(page)
    assert len(google.page_views()) == 2

    # "Back to registration" (boosted GET): a normal load of the start page.
    page.evaluate("() => { window.__sameDocument = true; }")
    page.get_by_role("link", name="Back to registration").click()
    page.wait_for_url("**/accounts/start/")
    _settle(page)
    assert page.evaluate("() => window.__sameDocument === undefined")
    assert len(google.page_views()) == 3

    # A partial htmx request seals the tag for the rest of this page.
    page.evaluate(
        "() => htmx.ajax('GET', '/legal/', {target: document.querySelector('#main-content'),"
        " swap: 'innerHTML'})"
    )
    _settle(page)
    assert page.evaluate(f"() => window['ga-disable-{STREAM_ID}']") is True
    page.evaluate("() => window.gtag('event', 'page_view', {})")
    page.wait_for_timeout(300)
    assert len(google.page_views()) == 3

    # A boosted navigation to a page without analytics: a normal page load
    # that starts without Google.
    page.evaluate("() => htmx.ajax('GET', '/accounts/ops/sign-in/', {target: document.body})")
    page.wait_for_url("**/accounts/ops/sign-in/")
    _settle(page)
    assert page.evaluate("() => window.dataLayer === undefined")
    assert len(google.page_views()) == 3
    assert len(google.loads) == 3
    measured = {hit["dl"] for hit in google.hits}
    assert measured == {f"{live_server.url}/accounts/start/", f"{live_server.url}/legal/"}
    assert google.blocked == []


def test_staff_sign_in_never_loads_google_even_with_consent(
    live_server, page, production_analytics
) -> None:
    google = GoogleMock(page, live_server.url)
    _granted_before_load(page)
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    _settle(page)
    assert page.locator("[data-asc-analytics]").count() == 0
    assert page.locator("[data-asc-analytics-banner]").count() == 0
    assert google.loads == [] and google.hits == []
    assert page.evaluate("() => window.dataLayer === undefined")


@pytest.mark.parametrize("language", ["fr", "ar"])
def test_the_banner_is_translated_and_the_measured_title_stays_fixed(
    live_server, page, seeded_open_event, production_analytics, language
) -> None:
    from tests.browser.helpers import switch_language

    google = GoogleMock(page, live_server.url)
    page.goto(f"{live_server.url}/accounts/start/")
    switch_language(page, language)
    _settle(page)
    assert page.locator("html").get_attribute("lang") == language
    banner = page.locator("[data-asc-analytics-banner]")
    expect(banner).to_be_visible()
    assert "Accept analytics" not in banner.inner_text()
    banner.locator("[data-asc-analytics-choice=granted]").click()
    _settle(page)
    views = google.page_views()
    assert len(views) == 1
    assert views[0]["dt"] == "Start registration"
    assert views[0]["dl"] == f"{live_server.url}/accounts/start/"
