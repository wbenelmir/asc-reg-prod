/*
 * ASC 2026 Google Analytics 4, consent-gated. Project-owned.
 *
 * The same-origin equivalent of Google's inline gtag snippet, so the
 * enforced CSP needs no 'unsafe-inline'. Loaded only by
 * `partials/analytics.html`, which production renders on the public start
 * and legal information pages; it does nothing on a page without the
 * `[data-asc-analytics]` marker (staff, OTP code entry, signed-in,
 * registration and diagnostics pages never carry it).
 *
 * Consent: Google's script is requested only after the visitor clicks
 * "Accept analytics". The choice ("granted" or "denied", nothing else) is
 * kept on the device under `asc2026.analytics.consent`; "Analytics
 * preferences" in the footer reopens the banner. Declining after accepting
 * disables the tag and removes the GA cookies of this site.
 *
 * Data minimisation: no automatic page view. Each page view is sent by this
 * script with the page's FIXED path and title from the marker (never
 * location.href, the query string, the real title or form values); the
 * referrer is reduced to its origin; Google signals and ad personalisation
 * are off. GA4 "Enhanced measurement" must be switched off in the GA admin
 * (see the deployment notes), since it would collect the real URL.
 *
 * htmx re-runs body scripts after a boosted swap: listeners are bound once,
 * and the per-page step runs again (a page view only on a marked page).
 */
(function () {
  "use strict";

  var KEY = "asc2026.analytics.consent";
  var ID_PATTERN = /^G-[A-Z0-9]{4,20}$/;
  var TAG_ORIGIN = "https://www.googletagmanager.com";

  function marker() {
    return document.querySelector("[data-asc-analytics]");
  }

  function readConsent() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "granted" || value === "denied" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function writeConsent(value) {
    try {
      window.localStorage.setItem(KEY, value);
    } catch (error) {
      // Unavailable storage: the choice holds for this page only.
    }
    window.ascAnalyticsChoice = value;
  }

  function consent() {
    return readConsent() || window.ascAnalyticsChoice || null;
  }

  function measurementId(node) {
    var id = node && node.getAttribute("data-measurement-id");
    return id && ID_PATTERN.test(id) ? id : null;
  }

  function gtag() {
    window.dataLayer.push(arguments);
  }

  function loadTag(id) {
    window["ga-disable-" + id] = false;
    if (window.ascAnalyticsLoaded) return;
    window.ascAnalyticsLoaded = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = gtag;
    gtag("consent", "default", {
      analytics_storage: "granted",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
    });
    gtag("js", new Date());
    gtag("config", id, {
      send_page_view: false,
      allow_google_signals: false,
      allow_ad_personalization_signals: false,
    });
    var script = document.createElement("script");
    script.async = true;
    script.src = TAG_ORIGIN + "/gtag/js?id=" + encodeURIComponent(id);
    document.head.appendChild(script);
  }

  function referrerOrigin() {
    try {
      return document.referrer ? new URL(document.referrer).origin + "/" : "";
    } catch (error) {
      return "";
    }
  }

  function sendPageView(node) {
    // One view per rendered page: a boosted swap both re-runs this script
    // and fires htmx:after:swap.
    if (node.hasAttribute("data-asc-analytics-sent")) return;
    node.setAttribute("data-asc-analytics-sent", "");
    var path = node.getAttribute("data-page-path") || "/";
    if (path.charAt(0) !== "/" || /[?#]/.test(path)) return;
    var page = {
      page_location: window.location.origin + path,
      page_path: path,
      page_title: node.getAttribute("data-page-title") || "ASC 2026",
      page_referrer: referrerOrigin(),
    };
    // Every later hit of this page uses the same sanitised values.
    gtag("set", page);
    gtag("event", "page_view", page);
  }

  function removeGoogleCookies() {
    var host = window.location.hostname;
    var domains = ["", host, "." + host];
    var parts = host.split(".");
    if (parts.length > 2) domains.push("." + parts.slice(-2).join("."));
    document.cookie.split(";").forEach(function (entry) {
      var name = entry.split("=")[0].trim();
      if (name !== "_ga" && name.indexOf("_ga_") !== 0) return;
      domains.forEach(function (domain) {
        document.cookie = name + "=; Max-Age=0; path=/" + (domain ? "; domain=" + domain : "");
      });
    });
  }

  function banner() {
    return document.querySelector("[data-asc-analytics-banner]");
  }

  function setBanner(open) {
    var node = banner();
    if (node) node.hidden = !open;
  }

  function setUp() {
    var node = marker();
    var id = measurementId(node);
    Array.prototype.forEach.call(document.querySelectorAll("[data-asc-analytics-open]"), function (button) {
      button.hidden = !id;
    });
    if (!id) return;
    var choice = consent();
    setBanner(choice === null);
    if (choice === "granted") {
      loadTag(id);
      sendPageView(node);
    }
  }

  if (!window.ascAnalyticsBound) {
    window.ascAnalyticsBound = true;

    document.addEventListener("click", function (event) {
      var target = event.target.closest && event.target.closest("[data-asc-analytics-choice], [data-asc-analytics-open]");
      if (!target) return;
      var node = marker();
      var id = measurementId(node);
      if (!id) return;
      if (target.hasAttribute("data-asc-analytics-open")) {
        setBanner(true);
        var first = banner() && banner().querySelector("button");
        if (first) first.focus();
        return;
      }
      var value = target.getAttribute("data-asc-analytics-choice");
      if (value !== "granted" && value !== "denied") return;
      var before = consent();
      writeConsent(value);
      setBanner(false);
      if (value === "granted" && before !== "granted") {
        loadTag(id);
        sendPageView(node);
      } else if (value === "denied") {
        window["ga-disable-" + id] = true;
        if (window.ascAnalyticsLoaded) gtag("consent", "update", { analytics_storage: "denied" });
        removeGoogleCookies();
      }
    });

    document.addEventListener("htmx:after:swap", setUp);
  }

  setUp();
})();
