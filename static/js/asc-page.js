/*
 * ASC 2026 page behaviours shared by every layout (P4-4). These were four
 * inline scripts in templates/base.html; they moved here unchanged in
 * behaviour so the enforced Content Security Policy needs no 'unsafe-inline',
 * nonce or hash (apps/core/middleware/security_headers.py).
 *
 * 1. htmx: restore ancestor inheritance, so `hx-boost` on `#main-content`
 *    cascades to its links and forms (htmx 4.0.0 defaults it to false).
 * 2. Error summary focus and a localized announcement after a boosted swap
 *    (WCAG 2.2 3.3.1 and 4.1.3).
 * 3. Language-switch form-value preservation: a strict allowlist of wizard
 *    fields is kept in sessionStorage (browser-local only) across the
 *    language POST-redirect, and restored once on the same path.
 * 4. Session-expiry warning banners driven by a client-side timer.
 * 5. `data-auto-submit` selects submit their form when changed (replaces the
 *    former inline `onchange` handlers).
 *
 * htmx re-inserts the body's scripts on a boosted swap, so this file may run
 * again: document listeners are bound once (guard below), and the per-page
 * set-up is idempotent and re-run after every `htmx:after:swap`.
 */
(function () {
  "use strict";

  if (window.htmx && window.htmx.config) {
    window.htmx.config.implicitInheritance = true;
  }
  if (window.ascPageBound) {
    return;
  }
  window.ascPageBound = true;

  // ------------------------------------------------ 2. error summary focus
  function focusErrorSummary() {
    var summary = document.getElementById("error-summary");
    if (summary) {
      summary.focus();
    }
  }

  function announceSwap() {
    var region = document.getElementById("status-region");
    var announcement = document.getElementById("htmx-status-announcement");
    if (region && announcement) {
      region.textContent = announcement.getAttribute("data-text");
    }
  }

  // ------------------------------------- 3. language-switch preservation
  var STORAGE_KEY = "asc2026_form_snapshot_v1";
  // Forbidden by exact (case-insensitive) name, whatever the opt-in marker.
  var FORBIDDEN_NAMES = [
    "password", "code", "otp", "token", "secret", "csrfmiddlewaretoken",
    "nin_value", "passport_number", "passport_country_code",
    "passport_expires_at", "passport_expires_at_day",
    "passport_expires_at_month", "passport_expires_at_year", "mobile_number",
  ];
  var FORBIDDEN_NAME_SUBSTRINGS = ["password", "token", "secret"];
  var FORBIDDEN_AUTOCOMPLETE_VALUES = ["current-password", "new-password", "one-time-code"];
  // An older snapshot is discarded without being restored.
  var MAX_SNAPSHOT_AGE_MS = 5 * 60 * 1000;

  function isForbiddenField(el) {
    if (el.type === "password" || el.type === "file" || el.type === "hidden") return true;
    if (el.disabled) return true;
    var name = (el.name || "").toLowerCase();
    if (FORBIDDEN_NAMES.indexOf(name) !== -1) return true;
    for (var i = 0; i < FORBIDDEN_NAME_SUBSTRINGS.length; i++) {
      if (name.indexOf(FORBIDDEN_NAME_SUBSTRINGS[i]) !== -1) return true;
    }
    var autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (FORBIDDEN_AUTOCOMPLETE_VALUES.indexOf(autocomplete) !== -1) return true;
    return false;
  }

  function findWizardForm() {
    var main = document.getElementById("main-content");
    if (!main) return null;
    return main.querySelector('form[data-language-preserve="safe-fields"]');
  }

  function snapshot() {
    var form = findWizardForm();
    if (!form) return;
    var data = {};
    var candidates = form.querySelectorAll("[data-language-preserve-field]");
    Array.prototype.forEach.call(candidates, function (el) {
      if (!el.name || isForbiddenField(el)) return;
      if (el.type === "checkbox" || el.type === "radio") {
        if (el.checked) data[el.name] = (data[el.name] || []).concat([el.value]);
      } else {
        data[el.name] = el.value;
      }
    });
    try {
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ path: location.pathname, timestamp: Date.now(), data: data })
      );
    } catch (e) { /* sessionStorage may be unavailable (private mode) */ }
  }

  function discardSnapshot() {
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch (e) { /* ignore */ }
  }

  function restore() {
    var form = findWizardForm();
    if (!form) return;
    var raw;
    try {
      raw = sessionStorage.getItem(STORAGE_KEY);
    } catch (e) { return; }
    if (!raw) return;
    var snap;
    try {
      snap = JSON.parse(raw);
    } catch (e) {
      discardSnapshot();
      return;
    }
    // Malformed, path-mismatched or expired: discarded, never left in place.
    var isWellFormed = snap && typeof snap === "object" && snap.data && typeof snap.timestamp === "number";
    var isExpired = isWellFormed && Date.now() - snap.timestamp > MAX_SNAPSHOT_AGE_MS;
    var isPathMismatch = isWellFormed && snap.path !== location.pathname;
    if (!isWellFormed || isExpired || isPathMismatch) {
      discardSnapshot();
      return;
    }
    Object.keys(snap.data).forEach(function (name) {
      var value = snap.data[name];
      // The opt-in marker and every exclusion are checked again at restore.
      var elements = form.querySelectorAll(
        '[name="' + name + '"][data-language-preserve-field]'
      );
      if (!elements.length) return;
      elements = Array.prototype.filter.call(elements, function (el) {
        return !isForbiddenField(el);
      });
      if (!elements.length) return;
      if (Array.isArray(value)) {
        elements.forEach(function (el) {
          el.checked = value.indexOf(el.value) !== -1;
        });
      } else if (elements[0].type !== "radio" && elements[0].type !== "checkbox") {
        elements[0].value = value;
      }
    });
    discardSnapshot();
  }

  // ------------------------------------------- 4. session-expiry warnings
  var bannerTimers = {};

  function wireBanner(bannerId, extendFormId) {
    if (bannerTimers[bannerId]) {
      window.clearTimeout(bannerTimers[bannerId]);
      delete bannerTimers[bannerId];
    }
    var banner = document.getElementById(bannerId);
    if (!banner) return;
    var inactivityDeadline = parseInt(banner.getAttribute("data-inactivity-deadline"), 10);
    var absoluteDeadline = parseInt(banner.getAttribute("data-absolute-deadline"), 10);
    var warningSeconds = parseInt(banner.getAttribute("data-warning-seconds"), 10) || 0;
    if (!inactivityDeadline || !absoluteDeadline) return;

    var textEl = banner.querySelector("[data-role=warning-text]");
    var extendForm = extendFormId ? document.getElementById(extendFormId) : null;
    var revealed = false;

    function reveal() {
      if (revealed) return;
      revealed = true;
      var now = Date.now();
      var msToInactivity = inactivityDeadline - now;
      var msToAbsolute = absoluteDeadline - now;
      // Name the deadline that is actually limiting.
      var limitingReason = msToInactivity <= msToAbsolute ? "inactivity" : "absolute";
      if (textEl) {
        var text = banner.getAttribute(
          limitingReason === "inactivity" ? "data-inactivity-text" : "data-absolute-text"
        );
        textEl.textContent = text || "";
      }
      if (extendForm) {
        // Extending inactivity cannot help against the absolute deadline.
        extendForm.hidden = limitingReason === "absolute";
      }
      // Bootstrap `.d-flex` overrides `[hidden]`, so classes are toggled.
      banner.classList.remove("d-none");
      banner.classList.add("d-flex");
    }

    var now = Date.now();
    var limitingDeadline = Math.min(inactivityDeadline, absoluteDeadline);
    var msUntilWarning = limitingDeadline - warningSeconds * 1000 - now;
    if (msUntilWarning <= 0) {
      reveal();
    } else {
      bannerTimers[bannerId] = window.setTimeout(reveal, msUntilWarning);
    }
  }

  function wireBanners() {
    wireBanner("participant-session-warning", "participant-session-extend-form");
    wireBanner("operational-session-warning", "operational-session-extend-form");
  }

  // ----------------------------------------------------- 5. auto-submit
  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!target || !target.matches || !target.matches("select[data-auto-submit]")) return;
    var form = target.form;
    if (!form) return;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      form.submit();
    }
  });

  // The language form is found by its select, on the page current at submit.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form && form.querySelector && form.querySelector("#language-switcher-select")) {
      snapshot();
    }
  }, true);

  // Synchronously, as the former inline scripts did: this file is loaded at
  // the end of <body>, so the page above is already parsed. The restore must
  // happen before asc-enhance.js builds its widgets on DOMContentLoaded (the
  // country search box reads the select's value once, when it is built).
  focusErrorSummary();
  restore();
  wireBanners();
  // Listening on `document`: the triggering element may already be detached.
  document.addEventListener("htmx:after:swap", function () {
    announceSwap();
    focusErrorSummary();
    restore();
    wireBanners();
  });
})();
