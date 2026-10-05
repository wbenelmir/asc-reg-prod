/*
 * Entry & Security checkpoint behaviour (Phase 3 Prompts 4 and 5).
 * Project-owned. Progressive enhancement only: every form works without it.
 *
 * 1. Participant-detail clearing (Flow §15.3): on a result screen, the
 *    participant panel and the decision controls are removed after
 *    `data-entry-clear-after` seconds without operator activity. The RESULT
 *    HEADING stays until the operator acknowledges it (UI/UX §12.2: critical
 *    results are never auto-dismissed). The server enforces its own,
 *    independent expiry on the verification itself.
 * 2. Scanner-friendly keyboard behaviour (FE-006): the QR field keeps focus;
 *    if focus is elsewhere (not in another field), the first printable key a
 *    USB scanner "types" moves focus back to the QR field so no character is
 *    lost. A visible status tells the operator whether the field is ready.
 * 3. Loading state and double-submit guard: a submitted lookup shows
 *    "Verifying…" and cannot be submitted twice.
 * 4. Esc on a result screen = "Clear and verify next".
 * 5. Result announcement (UI/UX §9.5): the heading and reason are placed in
 *    a live region -- assertive for results that must stop the operator,
 *    polite otherwise.
 * 6. Connection indicator (UI/UX §9.7): browser online/offline events plus a
 *    periodic PASSIVE status request (it never counts as operator activity
 *    on the server). Changes are announced once, politely, and never
 *    repeatedly (UI/UX §12.2). Offline verification is not available on
 *    this page: the offline state tells the operator not to verify.
 *    Phase 4 Prompt 2 (OFF-001): a failed status request no longer flips
 *    the page to "offline" by itself. It shows "Checking connection..."
 *    and retries sooner; "offline" follows only 3 consecutive failures
 *    spanning at least 20 seconds (the approved values, from the shell
 *    configuration), or the browser reporting that it has no network.
 *
 * No participant value, token, identity number, or search text is read,
 * stored, or sent by this script.
 */
(function () {
  "use strict";

  var doc = document;

  function isEditable(el) {
    if (!el || el === doc.body) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  // ---------------------------------------------------------------------
  // 1. Participant-detail clearing
  // ---------------------------------------------------------------------
  var panel = doc.getElementById("entry-participant");
  if (panel) {
    var seconds = parseInt(panel.getAttribute("data-entry-clear-after"), 10) || 0;
    if (seconds > 0) {
      var timer = null;
      var activityEvents = ["pointerdown", "keydown", "input"];
      var clearDetails = function () {
        var notice = doc.createElement("div");
        notice.className = "asc-notice asc-notice-neutral";
        notice.setAttribute("role", "status");
        var text = doc.createElement("p");
        text.textContent = panel.getAttribute("data-entry-cleared-text") || "";
        notice.appendChild(text);
        panel.replaceWith(notice);
        // Nothing may be recorded against a screen the operator can no
        // longer see: remove every decision form and its card.
        var decisions = doc.querySelectorAll(
          "form[action$='/entry/decision/'], form[action$='/entry/override/'], [data-entry-decision]"
        );
        Array.prototype.forEach.call(decisions, function (node) {
          node.remove();
        });
        var next = doc.getElementById("entry-next");
        if (next) next.focus();
        activityEvents.forEach(function (name) {
          doc.removeEventListener(name, reset, true);
        });
      };
      var reset = function () {
        if (timer) clearTimeout(timer);
        timer = setTimeout(clearDetails, seconds * 1000);
      };
      activityEvents.forEach(function (name) {
        doc.addEventListener(name, reset, true);
      });
      reset();
    }
  }

  // ---------------------------------------------------------------------
  // 2. Scanner-friendly focus
  // ---------------------------------------------------------------------
  var scanForm = doc.querySelector("[data-entry-scan-form]");
  var scanField = scanForm ? scanForm.querySelector("input[name='token']") : null;
  var scanStatus = doc.querySelector("[data-scan-status]");

  // Set by the connection indicator (section 6) while verification cannot
  // reach the server: the field must never claim to be ready then.
  var blockedText = "";

  function setScanReady(ready) {
    if (!scanStatus) return;
    var effective = ready && !blockedText;
    scanStatus.setAttribute("data-ready", effective ? "true" : "false");
    var label = scanStatus.querySelector("[data-scan-status-text]");
    if (label) {
      label.textContent =
        blockedText ||
        scanStatus.getAttribute(effective ? "data-text-ready" : "data-text-idle") ||
        "";
    }
  }

  if (scanField) {
    // Do not steal focus from a field that holds a validation error.
    var summary = doc.getElementById("error-summary");
    if (!summary) scanField.focus();
    setScanReady(doc.activeElement === scanField);
    scanField.addEventListener("focus", function () { setScanReady(true); });
    scanField.addEventListener("blur", function () { setScanReady(false); });
    doc.addEventListener(
      "keydown",
      function (event) {
        if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key.length !== 1 || isEditable(doc.activeElement)) return;
        // Redirect the keystroke into the QR field before the browser
        // delivers it, so a scanner's first character is never dropped.
        scanField.focus();
      },
      true
    );
  }

  // ---------------------------------------------------------------------
  // 3. Loading state and double-submit guard
  // ---------------------------------------------------------------------
  var busyText = doc.body.getAttribute("data-entry-busy-text");
  Array.prototype.forEach.call(doc.querySelectorAll("#main-content form"), function (form) {
    form.addEventListener("submit", function (event) {
      if (form.getAttribute("data-submitting") === "true") {
        event.preventDefault();
        return;
      }
      form.setAttribute("data-submitting", "true");
      form.setAttribute("aria-busy", "true");
      var button = form.querySelector("button[type='submit']");
      if (button) {
        var label = button.querySelector("span");
        var text = button.getAttribute("data-busy-text") || busyText;
        if (label && text) label.textContent = text;
        // Disable on the next tick so the submitter's own value is sent.
        setTimeout(function () { button.disabled = true; }, 0);
      }
    });
  });

  // ---------------------------------------------------------------------
  // 4. Esc clears a result
  // ---------------------------------------------------------------------
  var nextLink = doc.getElementById("entry-next");
  if (nextLink && doc.getElementById("entry-result")) {
    doc.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      var active = doc.activeElement;
      if (active && (active.tagName === "SELECT" || active.tagName === "TEXTAREA")) return;
      window.location.assign(nextLink.href);
    });
  }

  // ---------------------------------------------------------------------
  // 5. Result announcement
  // ---------------------------------------------------------------------
  var result = doc.getElementById("entry-result");
  if (result) {
    var mode = result.getAttribute("data-announce") === "assertive" ? "assertive" : "polite";
    var region = doc.getElementById("entry-announcer-" + mode);
    if (region) {
      var parts = [];
      var heading = result.querySelector("h1");
      if (heading) parts.push(heading.textContent.trim());
      Array.prototype.forEach.call(result.querySelectorAll(".asc-result-reason"), function (p) {
        parts.push(p.textContent.trim());
      });
      // A short delay lets screen readers finish the page-load announcement.
      setTimeout(function () { region.textContent = parts.join(". "); }, 250);
    }
  }

  // ---------------------------------------------------------------------
  // 6. Connection indicator
  // ---------------------------------------------------------------------
  var indicator = doc.getElementById("entry-connection");
  if (!indicator) return;

  var statusUrl = indicator.getAttribute("data-status-url");
  var pollMs = (parseInt(indicator.getAttribute("data-poll-seconds"), 10) || 20) * 1000;
  var announcer = doc.getElementById("entry-connection-announcer");
  var banners = {
    offline: doc.getElementById("entry-offline-banner"),
    degraded: doc.getElementById("entry-degraded-banner"),
    "session-ended": doc.getElementById("entry-session-banner"),
  };
  var current = indicator.getAttribute("data-state") || "online";
  var failureThreshold = parseInt(indicator.getAttribute("data-failures-to-offline"), 10) || 3;
  var failureSpanMs = (parseInt(indicator.getAttribute("data-failure-span-seconds"), 10) || 20) * 1000;
  var retryMs = (parseInt(indicator.getAttribute("data-retry-seconds"), 10) || 5) * 1000;
  var failures = 0;
  var firstFailureAt = null;
  var retryTimer = null;

  function recordFailure() {
    failures += 1;
    if (firstFailureAt === null) firstFailureAt = Date.now();
    if (failures >= failureThreshold && Date.now() - firstFailureAt >= failureSpanMs) {
      applyState("offline");
    } else {
      if (current !== "offline") applyState("checking");
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = setTimeout(check, retryMs);
    }
  }

  function recordSuccess() {
    failures = 0;
    firstFailureAt = null;
  }

  function applyState(state) {
    if (state === current) return;
    current = state;
    indicator.setAttribute("data-state", state);
    var label = indicator.querySelector("[data-conn-text]");
    var text = indicator.getAttribute("data-text-" + state) || "";
    if (label) label.textContent = text;
    Object.keys(banners).forEach(function (key) {
      if (banners[key]) banners[key].hidden = key !== state;
    });
    // Verification cannot succeed without the server: block new lookups
    // rather than let the operator wait on a failing request.
    var blocked = state === "offline" || state === "session-ended";
    blockedText = blocked ? text : "";
    setScanReady(scanField !== null && doc.activeElement === scanField);
    Array.prototype.forEach.call(
      doc.querySelectorAll("#main-content form[action*='/entry/'] button[type='submit']"),
      function (button) {
        if (blocked) {
          button.setAttribute("data-conn-disabled", "true");
          button.disabled = true;
        } else if (button.getAttribute("data-conn-disabled") === "true") {
          button.removeAttribute("data-conn-disabled");
          button.disabled = false;
        }
      }
    );
    if (announcer && state !== "checking") announcer.textContent = text;
  }

  function check() {
    if (!statusUrl || !window.fetch) return;
    if (navigator.onLine === false) {
      applyState("offline");
      return;
    }
    var controller = window.AbortController ? new AbortController() : null;
    var timeout = setTimeout(function () {
      if (controller) controller.abort();
    }, 8000);
    fetch(statusUrl, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "manual",
      headers: { Accept: "application/json" },
      signal: controller ? controller.signal : undefined,
    })
      .then(function (response) {
        clearTimeout(timeout);
        recordSuccess();
        if (response.type === "opaqueredirect" || response.status === 401 || response.status === 403) {
          applyState("session-ended");
          return null;
        }
        if (!response.ok) {
          applyState("degraded");
          return null;
        }
        return response.json();
      })
      .then(function (payload) {
        if (!payload) return;
        applyState(payload.state === "degraded" ? "degraded" : payload.state === "session-ended" ? "session-ended" : "online");
      })
      .catch(function () {
        clearTimeout(timeout);
        recordFailure();
      });
  }

  window.addEventListener("offline", function () { applyState("offline"); });
  window.addEventListener("online", function () {
    applyState("checking");
    check();
  });
  doc.addEventListener("visibilitychange", function () {
    if (doc.visibilityState === "visible") check();
  });
  setInterval(function () {
    if (doc.visibilityState === "visible") check();
  }, pollMs);
  // One early check, so a page restored from history or left open across a
  // server-side session end is corrected without waiting a full interval.
  setTimeout(check, 1500);
})();
