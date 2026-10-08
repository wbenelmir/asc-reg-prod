/*
 * ASC 2026 participant appearance (responsive themes). Project-owned.
 *
 * Loaded synchronously from the <head> of every page (templates/base.html),
 * before the stylesheets apply, so the chosen theme is on <html> before the
 * first paint. It acts only on participant pages: the participant shells
 * mark <html> with data-asc-appearance-scope="participant" and render the
 * Appearance menu (partials/appearance_switcher.html). On any other page it
 * removes its attributes and does nothing else.
 *
 * Layout categories follow the project's mobile breakpoint (Bootstrap md,
 * the `max-width: 767.98px` queries of asc-ui.css): narrower is "mobile",
 * anything wider (tablets included) is "desktop". Each category has its own
 * preference and default: Glass Dark on mobile, Color & Card on desktop.
 *
 * Kept on the device (localStorage), and only ever a theme identifier from
 * THEMES: `asc2026.appearance.mobile` and `asc2026.appearance.desktop`.
 * Nothing about the person, the account or the page is read or stored.
 * A missing, unknown or unreadable value falls back to the default, and an
 * unavailable storage keeps the choice for the current page only.
 *
 * A choice changes two attributes on <html> (data-asc-theme, data-asc-tone)
 * and the radios' checked state: no request, navigation, form submission,
 * re-render or focus change happens, so typed values, selected files and the
 * registration state stay as they are. Crossing the breakpoint applies the
 * other category's preference; it never writes a preference.
 *
 * htmx boosted navigation keeps the first page's <head> and <html>, so the
 * scope is re-read from the swapped body (the menu's presence) after every
 * swap. Listeners are delegated on `document` and bound once.
 */
(function () {
  "use strict";
  if (window.ascAppearanceBound) return;
  window.ascAppearanceBound = true;

  var root = document.documentElement;
  var THEMES = { aurora: "dark", fresh: "light", glass: "dark", color: "light" };
  var DEFAULTS = { mobile: "glass", desktop: "color" };
  var KEYS = { mobile: "asc2026.appearance.mobile", desktop: "asc2026.appearance.desktop" };
  var SCOPE = "data-asc-appearance-scope";
  var mobileQuery = window.matchMedia ? window.matchMedia("(max-width: 767.98px)") : null;
  // Choices made while the device storage is unavailable (this page only).
  var unsaved = {};

  function isTheme(value) {
    return typeof value === "string" && Object.prototype.hasOwnProperty.call(THEMES, value);
  }

  function category() {
    return mobileQuery && mobileQuery.matches ? "mobile" : "desktop";
  }

  function readPreference(layout) {
    try {
      var value = window.localStorage.getItem(KEYS[layout]);
      return isTheme(value) ? value : null;
    } catch (error) {
      return null;
    }
  }

  function writePreference(layout, theme) {
    try {
      window.localStorage.setItem(KEYS[layout], theme);
      return true;
    } catch (error) {
      return false;
    }
  }

  function currentTheme(layout) {
    return readPreference(layout) || unsaved[layout] || DEFAULTS[layout];
  }

  function controls() {
    return document.querySelectorAll("[data-asc-appearance]");
  }

  function syncControls(theme) {
    Array.prototype.forEach.call(controls(), function (control) {
      var current = null;
      Array.prototype.forEach.call(control.querySelectorAll("input[name=asc_appearance]"), function (input) {
        input.checked = input.value === theme;
        if (input.checked) current = input;
      });
      var label = control.querySelector("[data-asc-appearance-current]");
      var name = current && current.parentNode.querySelector(".asc-appearance-name");
      if (label && name) label.textContent = name.textContent;
      control.hidden = false;
    });
  }

  // A theme change takes effect at once: transitions are off for two frames
  // (asc-appearance.css), so no control fades from the previous theme's
  // colours under the new text colour.
  var switchingFrame = 0;

  function suspendTransitions() {
    root.setAttribute("data-asc-theme-switching", "");
    if (!window.requestAnimationFrame) {
      root.removeAttribute("data-asc-theme-switching");
      return;
    }
    var frame = ++switchingFrame;
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        if (frame === switchingFrame) root.removeAttribute("data-asc-theme-switching");
      });
    });
  }

  function apply() {
    if (root.getAttribute(SCOPE) !== "participant") {
      root.removeAttribute("data-asc-theme");
      root.removeAttribute("data-asc-tone");
      root.removeAttribute("data-asc-layout");
      return;
    }
    var layout = category();
    var theme = currentTheme(layout);
    var previous = root.getAttribute("data-asc-theme");
    if (previous && previous !== theme) suspendTransitions();
    root.setAttribute("data-asc-theme", theme);
    root.setAttribute("data-asc-tone", THEMES[theme]);
    root.setAttribute("data-asc-layout", layout);
    syncControls(theme);
  }

  function choose(theme) {
    if (!isTheme(theme)) return;
    var layout = category();
    if (!writePreference(layout, theme)) unsaved[layout] = theme;
    apply();
  }

  // ------------------------------------------------------------ menu
  function menuOf(node) {
    var control = node && node.closest ? node.closest("[data-asc-appearance]") : null;
    return control ? {
      control: control,
      trigger: control.querySelector("[data-asc-appearance-trigger]"),
      panel: control.querySelector("[data-asc-appearance-menu]"),
    } : null;
  }

  function openMenu(menu) {
    menu.panel.hidden = false;
    menu.trigger.setAttribute("aria-expanded", "true");
    var checked = menu.panel.querySelector("input:checked") || menu.panel.querySelector("input");
    if (checked) checked.focus();
  }

  function closeMenu(menu, returnFocus) {
    if (menu.panel.hidden) return;
    menu.panel.hidden = true;
    menu.trigger.setAttribute("aria-expanded", "false");
    if (returnFocus) menu.trigger.focus();
  }

  function closeAll(except) {
    Array.prototype.forEach.call(controls(), function (control) {
      if (control !== except) closeMenu(menuOf(control), false);
    });
  }

  // A pointer choice closes the menu; arrow keys only move the selection.
  var pointerChoice = false;

  document.addEventListener("pointerdown", function (event) {
    var menu = menuOf(event.target);
    pointerChoice = !!(menu && menu.panel.contains(event.target));
    closeAll(menu ? menu.control : null);
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    var menu = menuOf(target);
    if (!menu) return;
    if (target.closest("[data-asc-appearance-trigger]")) {
      if (menu.panel.hidden) openMenu(menu);
      else closeMenu(menu, false);
    } else if (target.name === "asc_appearance" && pointerChoice) {
      // The radio's click (also when its label was clicked) runs before its
      // change event; choosing the theme already shown also closes the menu.
      pointerChoice = false;
      choose(target.value);
      closeMenu(menu, true);
    }
  });

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input || input.name !== "asc_appearance" || !menuOf(input)) return;
    choose(input.value);
  });

  document.addEventListener("keydown", function (event) {
    var menu = menuOf(event.target);
    if (!menu) return;
    pointerChoice = false;
    if (event.key === "Escape" && !menu.panel.hidden) {
      event.preventDefault();
      closeMenu(menu, true);
    } else if ((event.key === "Enter" || event.key === " ") && event.target.name === "asc_appearance") {
      // Space checks the focused radio (native); Enter confirms. Both close.
      event.preventDefault();
      if (!event.target.checked) {
        event.target.checked = true;
        choose(event.target.value);
      }
      closeMenu(menu, true);
    }
  });

  // Leaving the menu with Tab closes it without moving focus back.
  document.addEventListener("focusout", function (event) {
    var menu = menuOf(event.target);
    if (!menu || menu.panel.hidden) return;
    var next = event.relatedTarget;
    if (next && !menu.control.contains(next)) closeMenu(menu, false);
  });

  // ------------------------------------------------------------ lifecycle
  if (mobileQuery) {
    var onLayoutChange = function () { apply(); };
    if (mobileQuery.addEventListener) mobileQuery.addEventListener("change", onLayoutChange);
    else if (mobileQuery.addListener) mobileQuery.addListener(onLayoutChange);
  }

  // Another tab changed a preference.
  window.addEventListener("storage", function (event) {
    if (event.key === KEYS.mobile || event.key === KEYS.desktop) apply();
  });

  // A page restored from the back/forward cache shows the latest choice.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) apply();
  });

  document.addEventListener("htmx:after:swap", function () {
    if (document.querySelector("[data-asc-appearance]")) root.setAttribute(SCOPE, "participant");
    else root.removeAttribute(SCOPE);
    apply();
  });

  // Before the first paint: the theme only (the body is not parsed yet).
  apply();
  // Once the header exists: reveal the menu and mark the current theme.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply);
  } else {
    apply();
  }
})();
