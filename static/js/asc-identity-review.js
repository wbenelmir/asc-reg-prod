/*
 * ASC 2026 identity review screen (IDV-3). Project-owned progressive
 * enhancement: every form and link works without it.
 *
 * Evidence viewer: switching between the case's documents, zoom in five
 * steps, rotation in 90 degree steps, reset, and the loading and error
 * states. Only classes and the `hidden` attribute change -- never an inline
 * style -- so the enforced CSP (no 'unsafe-inline') is respected. Only the
 * selected document is requested.
 *
 * Keyboard shortcuts (listed on the page): ignored while focus is in a field
 * or a dialog is open, and with Ctrl, Alt or Meta held. None of them submits
 * a form: "V" only moves focus to the Verify form, "N" and "Q" only navigate.
 *
 * No form value, image or identity value is read, stored or sent.
 */
(function () {
  "use strict";
  var root = document.querySelector("[data-idv-review]");
  if (!root) return;
  var viewer = document.querySelector("[data-idv-viewer]");
  var image = viewer ? viewer.querySelector("[data-idv-image]") : null;
  var ZOOM_LABELS = ["100%", "125%", "150%", "200%", "300%"];
  var ROTATIONS = [0, 90, 180, 270];
  var zoom = 0;
  var rotation = 0;

  function apply() {
    if (!image) return;
    ZOOM_LABELS.forEach(function (_label, index) {
      image.classList.toggle("asc-idv-z" + index, index === zoom);
    });
    ROTATIONS.forEach(function (degrees) {
      image.classList.toggle("asc-idv-r" + degrees, degrees === rotation);
    });
    var level = viewer.querySelector("[data-idv-zoom-level]");
    if (level) level.textContent = ZOOM_LABELS[zoom];
  }

  function showState(name) {
    ["loading", "error", "empty"].forEach(function (state) {
      var element = viewer.querySelector("[data-idv-" + state + "]");
      if (element) element.hidden = state !== name;
    });
  }

  function zoomBy(step) {
    zoom = Math.max(0, Math.min(ZOOM_LABELS.length - 1, zoom + step));
    apply();
  }

  function rotateBy(step) {
    rotation = (rotation + step + 360) % 360;
    apply();
  }

  function reset() {
    zoom = 0;
    rotation = 0;
    apply();
  }

  if (image) {
    image.addEventListener("load", function () {
      image.hidden = false;
      showState("");
    });
    image.addEventListener("error", function () {
      image.hidden = true;
      showState("error");
    });
    if (image.getAttribute("src") && image.complete && image.naturalWidth) showState("");
    if (!image.getAttribute("src")) image.hidden = true;
  }

  var documentButtons = viewer
    ? Array.prototype.slice.call(viewer.querySelectorAll("[data-idv-doc]"))
    : [];

  function selectDocument(button) {
    if (!image || !button) return;
    documentButtons.forEach(function (other) {
      other.setAttribute("aria-pressed", other === button ? "true" : "false");
    });
    var url = button.getAttribute("data-idv-doc");
    if (image.getAttribute("src") !== url) {
      showState("loading");
      image.setAttribute("src", url);
    }
    reset();
  }

  function cycleDocument(step) {
    if (!documentButtons.length) return;
    var current = documentButtons.findIndex(function (button) {
      return button.getAttribute("aria-pressed") === "true";
    });
    var next = (current + step + documentButtons.length) % documentButtons.length;
    selectDocument(documentButtons[next]);
    documentButtons[next].focus();
  }

  documentButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      selectDocument(button);
    });
  });

  if (viewer) {
    viewer.addEventListener("click", function (event) {
      var control = event.target.closest("[data-idv-zoom], [data-idv-rotate], [data-idv-reset]");
      if (!control) return;
      if (control.hasAttribute("data-idv-zoom")) {
        zoomBy(control.getAttribute("data-idv-zoom") === "in" ? 1 : -1);
      } else if (control.hasAttribute("data-idv-rotate")) {
        rotateBy(control.getAttribute("data-idv-rotate") === "cw" ? 90 : -90);
      } else {
        reset();
      }
    });
  }

  function isTyping(target) {
    if (!target || !target.tagName) return false;
    var tag = target.tagName;
    return (
      target.isContentEditable ||
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT"
    );
  }

  function focusVerifyForm() {
    var section = document.querySelector("[data-idv-action='verify']");
    if (!section) return;
    section.open = true;
    var field = section.querySelector("input:not([type=hidden]), select, textarea, button");
    if (field) field.focus();
  }

  function follow(selector, attribute) {
    var element = document.querySelector(selector);
    if (!element) return;
    var url = element.getAttribute(attribute);
    if (url) window.location.assign(url);
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (isTyping(event.target) || document.querySelector("dialog[open]")) return;
    var handled = true;
    switch (event.key) {
      case "+":
      case "=":
        zoomBy(1);
        break;
      case "-":
        zoomBy(-1);
        break;
      case "0":
        reset();
        break;
      case "r":
        rotateBy(90);
        break;
      case "R":
        rotateBy(-90);
        break;
      case "]":
        cycleDocument(1);
        break;
      case "[":
        cycleDocument(-1);
        break;
      case "v":
      case "V":
        focusVerifyForm();
        break;
      case "n":
      case "N":
        follow("[data-idv-skip]", "href");
        break;
      case "q":
      case "Q":
        follow("[data-idv-review]", "data-queue-url");
        break;
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  });

  apply();
})();
