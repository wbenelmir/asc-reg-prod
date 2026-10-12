# Google Analytics 4

Production website: `https://reg.africanstartupconference.org`, GA4 web
stream measurement ID `G-FDXGG6H52P`.

Code: `config/settings/_analytics.py` (configuration and CSP sources),
`config/settings/production.py`, `apps/core/context_processors.py`
(`site_metadata`), `templates/partials/analytics.html` (marker, consent
banner, loader), `static/js/asc-analytics.js`, footer control in
`templates/layouts/base_public.html`. Tests:
`apps/core/tests/test_upd09_analytics.py` and
`tests/browser/test_upd09_analytics_browser.py` (Google mocked).

GA4 measures visits to two public pages. It never replaces the
registration, identity or decision figures, which come from the
application database (`/ops/statistics/`).

## 1. Configuration

| Item | Value |
| --- | --- |
| Variable | `ANALYTICS_GA4_MEASUREMENT_ID` (optional, production only) |
| Unset | `G-FDXGG6H52P` (`DEFAULT_GA4_MEASUREMENT_ID`) |
| Empty | analytics off (no banner, no script, no Google CSP source) |
| Other value | must match `G-[A-Z0-9]{4,20}`, otherwise start-up stops |
| Staging, development, tests | always off (`ANALYTICS_GA4_MEASUREMENT_ID = ""` in `base.py`; only `production.py` reads the variable) |

CSP sources added in production when the ID is set (Google tag platform,
"Content Security Policy", Google Analytics without Ads features):

* `script-src`: `https://www.googletagmanager.com`
* `connect-src`: `https://www.googletagmanager.com`, `https://*.google-analytics.com`,
  `https://*.analytics.google.com`, `https://*.google.com`
* `img-src`: `https://www.googletagmanager.com`, `https://*.google-analytics.com`

No `'unsafe-inline'`, `'unsafe-eval'`, nonce or bare wildcard: the tag is
started by the same-origin `asc-analytics.js`, not by Google's inline
snippet. Every other directive is unchanged.

## 2. Where and when it runs

* **Pages:** only the public start page (`/accounts/start/`, reached from `/`)
  and the legal information page (`/legal/`). Staff and operations pages,
  sign-in, code entry, credential setup, the participant workspace and
  registration wizard, identity review and document pages never include it
  (guarded by a test that lists every template including the partial).
* **Consent (opt-in):** nothing is requested from Google until the visitor
  clicks "Accept analytics". "Decline analytics" keeps Google off. The choice
  is stored on the device (`localStorage`, key `asc2026.analytics.consent`).
  "Analytics preferences" in the footer reopens the banner; declining after
  accepting sets Google's documented opt-out (`window['ga-disable-<ID>']`),
  updates consent to denied and deletes the `_ga` cookies.
* **Page views:** exactly one per page load, sent by the script with a fixed
  path and an English title (`/accounts/start/` "Start registration",
  `/legal/` "Privacy Notice and Terms"), never the real address, query string
  or page title. The referrer is reduced to its origin. Google signals and ad
  personalisation are off; `send_page_view` is false.
* **htmx navigation:** once Google's tag is loaded, a boosted link is turned
  into a normal page load (the next page is counted once by its own tag, or
  starts without Google), and any other htmx request or history change seals
  the tag for the rest of the page (`ga-disable`), so nothing is measured in a
  page the tag was not started for.

## 3. Diagnosis of "Data collection isn't active" (2026-10-12)

Verified locally:

1. The tag, measurement ID, loader and initialization were correct and
   production-only; nothing was missing from the templates. Collection starts
   only after a visitor accepts analytics, so the stream receives nothing
   until someone does: with an opt-in banner, a low acceptance rate alone
   keeps GA4 showing this warning.
2. **CSP:** production allowed `connect-src` to `*.google-analytics.com`,
   `*.analytics.google.com` and `*.googletagmanager.com`, but not
   `https://*.google.com`, which Google's current CSP guide lists for Google
   Analytics without Ads features. A collection request to a `google.com`
   host would have been blocked by the browser. Added.
3. **Withdrawal control:** "Analytics preferences" stayed hidden on a normal
   page load (the script ran before the footer existed), so a visitor who
   accepted could not withdraw without a boosted navigation first. Fixed.
4. **Accepting again after declining** on the same page left consent denied.
   Fixed (consent update to granted).
5. **Double counting and real URLs:** a boosted link between the two public
   pages kept the tag alive; the script sent its page view and GA4 Enhanced
   measurement sent another one for the history change, with the real
   address and title. Fixed (section 2); a negative-control run of the
   browser tests against the previous script reproduces it (4 page views
   instead of 2).
6. The sanitised page values are now set before the tag is configured, so
   every hit of the page uses them.

Not verifiable locally (production and Google account checks, section 5):
whether the deployed release serves the tag, the live CSP header, browser
extensions or blockers, visitor acceptance, and the GA4 property settings.
The local fix does not prove that production is collecting.

## 4. Google Analytics settings for the owner (account side)

Not changed by this update; done by the owner in the GA4 admin
(Admin > Data streams > the web stream):

* **Enhanced measurement:** switch it off, or at least switch off "Page
  changes based on browser history events" (Google: page views are sent on
  history changes even with `send_page_view: false`), "Site search", "Form
  interactions", "Outbound clicks", "File downloads", "Video engagement" and
  "Scrolls". The application sends its own page views; the automatic events
  would add duplicates or real addresses. The script already prevents them in
  practice (section 2), so this is defence in depth.
* **Data collection:** keep Google signals off; no ads links are needed.
* **Data retention** and **IP/region** settings: choose per the privacy
  notice.

## 5. Smoke test after deployment (owner or deployment team)

No Google credential is needed in the repository; do not send test events
from scripts.

1. Open `https://reg.africanstartupconference.org/accounts/start/` in a
   private window. View source: one
   `<div data-asc-analytics data-measurement-id="G-FDXGG6H52P" data-page-path="/accounts/start/" ...>`
   and one `/static/js/asc-analytics.js?v=2` script. Response header
   `Content-Security-Policy` contains `https://www.googletagmanager.com` in
   `script-src` and `https://*.google.com` in `connect-src`.
2. DevTools > Network: before choosing, no request to Google. Console: no CSP
   error.
3. Click "Accept analytics": one request to
   `https://www.googletagmanager.com/gtag/js?id=G-FDXGG6H52P`, then a
   collection request (`/g/collect`, status 204) whose `dl` is
   `https://reg.africanstartupconference.org/accounts/start/` and `dt` is
   `Start registration`. No CSP error.
4. Follow "Privacy Notice and Terms": a normal page load and one collection
   request for `/legal/`.
5. Footer "Analytics preferences" > "Decline analytics": no further
   collection request; the `_ga` cookies are gone.
6. GA4 > Reports > Realtime (or Admin > DebugView with Tag Assistant):
   the visit appears within minutes. The "Data collection isn't active"
   notice can take up to 48 hours to clear after the first hits.
7. Sign in as staff and open `/ops/registrations/`: no request to Google.

If step 3 shows no loader request: check that the release is deployed, that
`ANALYTICS_GA4_MEASUREMENT_ID` is not set to an empty value, and that a
browser extension is not blocking `googletagmanager.com`.
