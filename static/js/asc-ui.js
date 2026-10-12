/*
 * ASC 2026 shared page behaviour for the operations and workspace shells
 * (Phase 3 Prompt 5). Project-owned progressive enhancement: every form
 * works without it.
 *
 * Double-submit guard and loading state: a submitted form is marked
 * aria-busy, its submit button shows the localized "working" text and is
 * disabled, so a slow response can never create a second command. (Every
 * state-changing command is also idempotent server-side; this only avoids
 * confusing the operator.)
 *
 * No form value is read, stored, or sent by this script.
 */
(function () {
  "use strict";
  var busyText = document.body.getAttribute("data-entry-busy-text");
  Array.prototype.forEach.call(document.querySelectorAll("#main-content form"), function (form) {
    form.addEventListener("submit", function (event) {
      if (form.getAttribute("data-submitting") === "true") {
        event.preventDefault();
        return;
      }
      form.setAttribute("data-submitting", "true");
      form.setAttribute("aria-busy", "true");
      var button = event.submitter || form.querySelector("button[type=submit]");
      if (button) {
        var label = button.querySelector("span");
        if (label && busyText) label.textContent = busyText;
        setTimeout(function () { button.disabled = true; }, 0);
      }
    });
  });
  // Operations section bar (components/ops_nav.html): rendered open so it
  // works without JavaScript; collapsed below its breakpoint (on load and
  // when the viewport narrows) and re-opened above it, where its toggle is
  // hidden.
  Array.prototype.forEach.call(document.querySelectorAll("details[data-collapse-below]"), function (menu) {
    var breakpoint = parseInt(menu.getAttribute("data-collapse-below"), 10);
    if (!breakpoint || !window.matchMedia) return;
    var narrow = window.matchMedia("(max-width: " + (breakpoint - 0.02) + "px)");
    if (narrow.matches) menu.open = false;
    var sync = function (event) { menu.open = !event.matches; };
    if (narrow.addEventListener) narrow.addEventListener("change", sync);
  });
  // UPD-06: copy a value shown once (an invitation link) to the clipboard.
  // Without JavaScript or clipboard access the value stays selectable.
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-copy-target]") : null;
    if (!button) return;
    var source = document.getElementById(button.getAttribute("data-copy-target"));
    if (!source || !navigator.clipboard) return;
    navigator.clipboard.writeText(source.textContent.trim()).then(function () {
      var label = button.querySelector("span");
      var done = button.getAttribute("data-copy-done");
      if (label && done) label.textContent = done;
    });
  });
  // UPD-08: the printable report. A "Print or save as PDF" button opens the
  // browser's print dialog; a report opened from a PDF link opens it once.
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-print-report]") : null;
    if (!button) return;
    event.preventDefault();
    window.print();
  });
  if (document.querySelector("[data-auto-print]")) {
    window.addEventListener("load", function () {
      setTimeout(function () { window.print(); }, 250);
    });
  }
  // A page restored from the back/forward cache must be usable again.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    Array.prototype.forEach.call(document.querySelectorAll("form[data-submitting]"), function (form) {
      form.removeAttribute("data-submitting");
      form.removeAttribute("aria-busy");
      Array.prototype.forEach.call(form.querySelectorAll("button[disabled]"), function (b) {
        b.disabled = false;
      });
    });
  });
})();
