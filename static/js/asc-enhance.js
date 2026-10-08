/*
 * ASC 2026 shared progressive enhancement for every page (UI/UX Completion
 * Gate Stage 3). Project-owned; every form keeps working without it.
 *
 * 1. Shared confirmation dialog (components/confirm_dialog.html). A form
 *    carrying `data-confirm` is not submitted directly: its submission is
 *    intercepted, the dialog opens, and only the dialog's confirm button
 *    submits the form again through the browser's normal form submission
 *    (`requestSubmit`, so constraint validation, CSRF tokens, htmx boosting
 *    and the double-submit guard all apply as usual). Opening the dialog
 *    never executes anything. Escape or "Go back" closes it and returns
 *    focus to the button that opened it.
 *
 * 2. Filterable picker (components/picker.html). A long, server-scoped
 *    select gains a filter box that hides non-matching options locally.
 *    Nothing is requested from the server and no value is stored or sent.
 *
 * 3. One-time code (input[data-asc-otp], UX-1). One accessible input drawn as
 *    six cells by CSS. The script only sanitizes typed, pasted and autofilled
 *    text (ASCII digits, Arabic-Indic digits read as ASCII, no separators) and
 *    never submits the form.
 *
 * 4. Digit counter (input[data-asc-digit-counter]). A running "n / max" that
 *    only counts; it never edits, strips or converts what was typed.
 *
 * 5. Search-and-select (select[data-asc-combobox], UX-1, decision S-07). A
 *    WAI-ARIA 1.2 combobox over a native select for long lists. The native
 *    select stays the submitted value and the server validates it.
 *
 * 6. Phone example. The placeholder of the mobile number follows the selected
 *    calling country.
 *
 * 7. Human check: since UX-C2 (UX-D01 option M) the vendored ALTCHA widget
 *    and `static/js/asc-altcha.js` handle it; this file has no solver.
 *
 * 8. Identity-document panels (form[data-identity-panels], UXR-C1). Only the
 *    selected document's panel is shown and its controls enabled; set up on
 *    load and after every boosted swap.
 *
 * Listeners are delegated on `document` so boosted page swaps need no
 * re-binding; the guard below keeps a re-executed script from binding twice.
 */
(function () {
  "use strict";
  if (window.ascEnhanceBound) return;
  window.ascEnhanceBound = true;

  // ---------------------------------------------------------------- dialog
  var pending = null;

  function dialogElement() {
    return document.getElementById("asc-confirm-dialog");
  }

  function openConfirmation(form, submitter) {
    var dialog = dialogElement();
    var title = dialog.querySelector("#asc-confirm-title");
    var message = dialog.querySelector("#asc-confirm-message");
    var accept = dialog.querySelector("[data-confirm-accept]");
    var cancel = dialog.querySelector("[data-confirm-cancel]");
    var tone = form.getAttribute("data-confirm-tone") === "primary" ? "primary" : "danger";
    var submitterText = submitter ? submitter.textContent.replace(/\s+/g, " ").trim() : "";

    title.textContent = form.getAttribute("data-confirm-title") || dialog.getAttribute("data-default-title");
    message.textContent = form.getAttribute("data-confirm");
    accept.textContent = form.getAttribute("data-confirm-action") || submitterText;
    accept.className = "btn " + (tone === "primary" ? "btn-primary" : "btn-danger");
    dialog.setAttribute("data-tone", tone);

    pending = { form: form, submitter: submitter || null };
    dialog.showModal();
    // The least destructive choice has focus first.
    cancel.focus();
  }

  function restoreFocus(entry) {
    if (!entry) return;
    var target = entry.submitter || entry.form.querySelector("[type=submit]");
    if (target && typeof target.focus === "function") target.focus();
  }

  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) return;
      if (form.getAttribute("data-confirmed") === "true") {
        form.removeAttribute("data-confirmed");
        return;
      }
      // A server-validated form (`novalidate`) that is visibly incomplete
      // goes straight to the server, which refuses it and shows the styled
      // error summary: nothing can execute, so there is nothing to confirm.
      if (form.noValidate && !form.checkValidity()) return;
      var dialog = dialogElement();
      if (!dialog || typeof dialog.showModal !== "function") {
        // A browser without <dialog>: fall back to the native question.
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      openConfirmation(form, event.submitter);
    },
    true
  );

  document.addEventListener("click", function (event) {
    var dialog = dialogElement();
    if (!dialog || !dialog.contains(event.target)) return;
    if (event.target.closest("[data-confirm-cancel]")) {
      dialog.close("cancel");
      return;
    }
    if (event.target.closest("[data-confirm-accept]")) {
      var entry = pending;
      pending = null;
      dialog.close("confirm");
      if (!entry) return;
      entry.form.setAttribute("data-confirmed", "true");
      if (typeof entry.form.requestSubmit === "function") {
        entry.form.requestSubmit(entry.submitter && entry.submitter.form === entry.form ? entry.submitter : undefined);
      } else {
        entry.form.submit();
      }
    }
  });

  // A boosted swap parses the response with scripting disabled
  // (Document.parseHTMLUnsafe / DOMParser), so a <noscript> no-JavaScript
  // consent box (components/confirm_fallback.html) arrives as real, hidden
  // elements. Its unchecked `required` checkbox then fails the form's
  // constraint validation, the submit event never fires and the dialog
  // never opens. Scripting is running here, so that content is emptied; a
  // full page load parses it as text and has nothing to empty.
  function clearParsedNoscript(scope) {
    Array.prototype.forEach.call(scope.querySelectorAll("noscript"), function (node) {
      if (node.firstElementChild) node.replaceChildren();
    });
  }

  // Escape fires `cancel`, then `close`; both paths end here.
  document.addEventListener(
    "close",
    function (event) {
      if (event.target !== dialogElement()) return;
      var entry = pending;
      pending = null;
      restoreFocus(entry);
    },
    true
  );

  // ---------------------------------------------------------------- picker
  function normalise(value) {
    return (value || "").toLocaleLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
  }

  function setupPicker(picker) {
    if (picker.getAttribute("data-picker-ready") === "true") return;
    var select = picker.querySelector("select");
    var filterBox = picker.querySelector(".asc-picker-filter");
    var filter = picker.querySelector("[data-picker-filter]");
    var status = picker.querySelector("[data-picker-status]");
    if (!select || !filterBox || !filter) return;
    picker.setAttribute("data-picker-ready", "true");
    var threshold = parseInt(picker.getAttribute("data-picker-threshold"), 10) || 8;
    var options = Array.prototype.filter.call(select.options, function (option) {
      return option.value !== "";
    });
    if (options.length <= threshold) return;
    filterBox.hidden = false;

    filter.addEventListener("input", function () {
      var query = normalise(filter.value.trim());
      var shown = 0;
      options.forEach(function (option) {
        var match = !query || normalise(option.textContent).indexOf(query) !== -1;
        // The selected option always stays, so a filter never changes the value.
        var keep = match || option.selected;
        option.hidden = !keep;
        option.disabled = !keep;
        if (keep) shown += 1;
      });
      if (status) {
        status.hidden = !query;
        status.textContent = query ? status.getAttribute("data-template") + " " + shown : "";
      }
    });
  }

  // A grouped select that follows another select (bulk accreditation: the
  // item list narrows to the chosen assignment type). Presentation only: the
  // server still rejects an item of another type.
  function setupGroupFollow(select) {
    if (select.getAttribute("data-group-ready") === "true") return;
    var controller = document.getElementById(select.getAttribute("data-group-follows"));
    if (!controller) return;
    select.setAttribute("data-group-ready", "true");
    function sync() {
      var chosen = controller.selectedIndex > 0 ? controller.options[controller.selectedIndex].textContent.trim() : "";
      Array.prototype.forEach.call(select.querySelectorAll("optgroup"), function (group) {
        var off = chosen !== "" && group.label.trim() !== chosen;
        group.disabled = off;
        group.hidden = off;
      });
      var current = select.options[select.selectedIndex];
      if (current && current.parentElement && current.parentElement.disabled) select.value = "";
    }
    controller.addEventListener("change", sync);
    sync();
  }

  // -------------------------------------------------------------- digits
  // Arabic-Indic (U+0660-0669) and Extended Arabic-Indic (U+06F0-06F9) digits
  // read as ASCII, matching the server's normalization (decision S-02). Any
  // other character is left alone here: the server has the last word.
  function asciiDigits(value) {
    return (value || "").replace(/[٠-٩۰-۹]/g, function (ch) {
      var code = ch.charCodeAt(0);
      return String.fromCharCode(48 + (code >= 0x06F0 ? code - 0x06F0 : code - 0x0660));
    });
  }

  function digitsOnly(value) {
    return asciiDigits(value).replace(/\D/g, "");
  }

  // ------------------------------------------------------------------- otp
  // One logical input drawn as six cells by CSS (`.asc-otp-input`). This
  // script only sanitizes: typed, pasted and autofilled text keeps digits,
  // reads Arabic-Indic digits as ASCII, drops spaces and hyphens, and stops
  // at the code length. It never submits the form (WCAG 3.2.2).
  function setupOtp(input) {
    if (input.getAttribute("data-otp-ready") === "true") return;
    input.setAttribute("data-otp-ready", "true");
    var length = parseInt(input.getAttribute("data-asc-otp"), 10) || 6;
    input.classList.add("asc-otp-input");
    input.style.setProperty("--asc-otp-length", String(length));

    function paint() {
      var count = Math.min(input.value.length, length - 1);
      input.style.setProperty("--asc-otp-active", String(count));
      input.classList.toggle("is-complete", input.value.length >= length);
      // Never leave a digit scrolled out of its cell.
      input.scrollLeft = 0;
    }

    function sanitize() {
      var raw = input.value;
      var caret = typeof input.selectionStart === "number" ? input.selectionStart : raw.length;
      var cleaned = digitsOnly(raw).slice(0, length);
      if (cleaned !== raw) {
        var keptBeforeCaret = digitsOnly(raw.slice(0, caret)).length;
        input.value = cleaned;
        var position = Math.min(keptBeforeCaret, cleaned.length);
        try {
          input.setSelectionRange(position, position);
        } catch (e) { /* not selectable in this state */ }
      }
      paint();
    }

    input.addEventListener("input", sanitize);
    input.addEventListener("change", sanitize);
    input.addEventListener("paste", function (event) {
      var data = event.clipboardData || window.clipboardData;
      if (!data) return;
      var pasted = digitsOnly(data.getData("text"));
      if (!pasted) return;
      event.preventDefault();
      input.value = pasted.slice(0, length);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    input.addEventListener("keyup", paint);
    input.addEventListener("focus", paint);
    sanitize();
  }

  // --------------------------------------------------------- digit counter
  // A running "n / max" for a digits-only identifier such as the NIN. It only
  // counts: it never edits the value, strips a separator or converts a digit,
  // so identity data is exactly what the person typed. The visible counter is
  // hidden from assistive technology; a polite status speaks only when the
  // count reaches the target or passes it.
  function setupDigitCounter(input) {
    if (input.getAttribute("data-counter-ready") === "true") return;
    input.setAttribute("data-counter-ready", "true");
    var max = parseInt(input.getAttribute("data-asc-digit-counter"), 10);
    if (!max) return;
    input.classList.add("asc-digits-input");
    var visible = document.createElement("p");
    visible.className = "form-text asc-digit-counter";
    visible.setAttribute("aria-hidden", "true");
    var bdi = document.createElement("bdi");
    bdi.setAttribute("dir", "ltr");
    visible.appendChild(bdi);
    var status = document.createElement("p");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    input.insertAdjacentElement("afterend", visible);
    visible.insertAdjacentElement("afterend", status);
    var lastState = "";

    function update() {
      var count = digitsOnly(input.value).length;
      bdi.textContent = count + " / " + max;
      var state = count === max ? "complete" : count > max ? "over" : "";
      visible.classList.toggle("is-complete", state === "complete");
      visible.classList.toggle("is-over", state === "over");
      if (state !== lastState) {
        status.textContent =
          state === "complete"
            ? input.getAttribute("data-complete-text") || ""
            : state === "over"
              ? input.getAttribute("data-over-text") || ""
              : "";
        lastState = state;
      }
    }
    input.addEventListener("input", update);
    update();
  }

  // ---------------------------------------------------------- phone example
  // The placeholder of the mobile number follows the selected calling
  // country (examples come from the server, one per region). Presentation
  // only: it changes no value and no validation.
  function setupRegionExample(input) {
    if (input.getAttribute("data-example-ready") === "true") return;
    input.setAttribute("data-example-ready", "true");
    var examples;
    try {
      examples = JSON.parse(input.getAttribute("data-region-examples") || "{}");
    } catch (e) { return; }
    var form = input.form;
    var source = form && form.elements.namedItem(input.getAttribute("data-region-source"));
    if (!source || source instanceof RadioNodeList) return;
    function apply() {
      var example = examples[source.value];
      if (example) input.setAttribute("placeholder", example);
      else input.removeAttribute("placeholder");
    }
    source.addEventListener("change", apply);
    apply();
  }

  // -------------------------------------------------------------- combobox
  // Search-and-select over a native select (decision S-07): the select stays
  // in the page and is the value that is submitted; the server validates it
  // against the same queryset. The control follows the WAI-ARIA 1.2 combobox
  // pattern with a filterable listbox: `aria-expanded`, `aria-activedescendant`,
  // Down/Up/Home/End/Enter/Escape and type-ahead filtering. It is used only
  // when the list is longer than its threshold; a shorter list stays a
  // native select. Without this script nothing changes.
  var comboboxCounter = 0;

  function setupCombobox(select) {
    if (select.getAttribute("data-combobox-ready") === "true") return;
    var threshold = parseInt(select.getAttribute("data-combobox-threshold"), 10) || 8;
    var choices = Array.prototype.filter.call(select.options, function (option) {
      return option.value !== "";
    });
    if (choices.length <= threshold) return;
    select.setAttribute("data-combobox-ready", "true");
    comboboxCounter += 1;

    var originalId = select.id;
    var label = originalId ? document.querySelector('label[for="' + originalId + '"]') : null;
    var hasEmpty = Array.prototype.some.call(select.options, function (option) {
      return option.value === "";
    });
    var listId = (originalId || "asc-combobox-" + comboboxCounter) + "_listbox";
    var strings = {
      toggle: select.getAttribute("data-label-toggle") || "Show options",
      clear: select.getAttribute("data-label-clear") || "Clear selection",
      empty: select.getAttribute("data-label-empty") || "No matching choices",
      count: select.getAttribute("data-label-count") || "Choices shown:",
      hint: select.getAttribute("data-label-hint") || "Type to filter the list"
    };

    var wrapper = document.createElement("div");
    wrapper.className = "asc-combobox";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "form-control asc-combobox-input";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", listId);
    input.setAttribute("autocomplete", "off");
    input.setAttribute("autocapitalize", "off");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("placeholder", strings.hint);
    ["aria-describedby", "aria-invalid", "aria-required"].forEach(function (name) {
      if (select.hasAttribute(name)) input.setAttribute(name, select.getAttribute(name));
    });
    // The control that people and error links reach keeps the field's id, and
    // the label follows it. The native select is renamed and hidden, but its
    // `name` and value are untouched.
    select.id = (originalId || "asc-combobox-" + comboboxCounter) + "_native";
    input.id = originalId || "asc-combobox-" + comboboxCounter;
    if (label) label.setAttribute("for", input.id);

    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "asc-combobox-toggle";
    toggle.tabIndex = -1;
    toggle.setAttribute("aria-label", strings.toggle);
    toggle.innerHTML =
      '<svg class="asc-icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
    var clear = null;
    if (hasEmpty && !select.required) {
      clear = document.createElement("button");
      clear.type = "button";
      clear.className = "asc-combobox-clear";
      clear.tabIndex = -1;
      clear.hidden = true;
      clear.setAttribute("aria-label", strings.clear);
      clear.innerHTML =
        '<svg class="asc-icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    }

    var list = document.createElement("ul");
    list.id = listId;
    list.className = "asc-combobox-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    if (label) list.setAttribute("aria-labelledby", label.id || (label.id = input.id + "_label"));

    var status = document.createElement("p");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(input);
    wrapper.appendChild(toggle);
    if (clear) wrapper.appendChild(clear);
    wrapper.appendChild(list);
    wrapper.appendChild(status);
    wrapper.appendChild(select);
    select.hidden = true;
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    var items = [];
    var active = -1;
    var dirty = false;

    function selectedOption() {
      var option = select.options[select.selectedIndex];
      return option && option.value !== "" ? option : null;
    }

    function syncDisplay() {
      var option = selectedOption();
      input.value = option ? option.textContent.trim() : "";
      if (clear) clear.hidden = !option || select.disabled;
      dirty = false;
    }

    function setActive(index) {
      if (active >= 0 && items[active]) items[active].removeAttribute("data-active");
      active = index;
      if (index >= 0 && items[index]) {
        items[index].setAttribute("data-active", "true");
        input.setAttribute("aria-activedescendant", items[index].id);
        items[index].scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function render(query) {
      list.textContent = "";
      items = [];
      var needle = normalise(query.trim());
      var chosen = selectedOption();
      choices.forEach(function (option, position) {
        if (needle && normalise(option.textContent).indexOf(needle) === -1) return;
        var item = document.createElement("li");
        item.id = listId + "_" + position;
        item.className = "asc-combobox-option";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", chosen && chosen.value === option.value ? "true" : "false");
        item.setAttribute("data-value", option.value);
        item.textContent = option.textContent.trim();
        list.appendChild(item);
        items.push(item);
      });
      if (!items.length) {
        var none = document.createElement("li");
        none.className = "asc-combobox-empty";
        none.setAttribute("role", "presentation");
        none.textContent = strings.empty;
        list.appendChild(none);
      }
      status.textContent = items.length ? strings.count + " " + items.length : strings.empty;
      var selectedItem = items.findIndex(function (item) {
        return item.getAttribute("aria-selected") === "true";
      });
      setActive(!needle && selectedItem >= 0 ? selectedItem : items.length ? 0 : -1);
    }

    function open(query) {
      render(query === undefined ? "" : query);
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      if (active >= 0) items[active].scrollIntoView({ block: "nearest" });
    }

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      setActive(-1);
    }

    function commit(value) {
      if (select.value !== value) {
        select.value = value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      syncDisplay();
      close();
    }

    input.addEventListener("input", function () {
      dirty = true;
      open(input.value);
    });
    input.addEventListener("focus", function () {
      input.select();
    });
    input.addEventListener("blur", function () {
      // An emptied box clears the choice when the field allows it; otherwise
      // the box returns to the current selection.
      if (dirty && input.value.trim() === "" && hasEmpty && !select.required) {
        commit("");
      } else {
        syncDisplay();
        close();
      }
    });
    input.addEventListener("keydown", function (event) {
      var key = event.key;
      var isOpen = !list.hidden;
      if (key === "ArrowDown" || key === "ArrowUp") {
        event.preventDefault();
        if (!isOpen) {
          open(dirty ? input.value : "");
          return;
        }
        if (!items.length) return;
        var step = key === "ArrowDown" ? 1 : -1;
        setActive((active + step + items.length) % items.length);
      } else if ((key === "Home" || key === "End") && isOpen && items.length) {
        event.preventDefault();
        setActive(key === "Home" ? 0 : items.length - 1);
      } else if (key === "Enter") {
        if (isOpen && active >= 0 && items[active]) {
          event.preventDefault();
          commit(items[active].getAttribute("data-value"));
        } else if (isOpen) {
          event.preventDefault();
        }
      } else if (key === "Escape") {
        if (isOpen) {
          event.preventDefault();
          syncDisplay();
          close();
        }
      } else if (key === "Tab") {
        if (isOpen && active >= 0 && items[active] && dirty) {
          commit(items[active].getAttribute("data-value"));
        }
      }
    });
    list.addEventListener("mousedown", function (event) {
      // Keep the focus in the box, so a click on an option is not a blur first.
      event.preventDefault();
    });
    list.addEventListener("click", function (event) {
      var item = event.target.closest(".asc-combobox-option");
      if (item) commit(item.getAttribute("data-value"));
    });
    toggle.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });
    toggle.addEventListener("click", function () {
      if (input.disabled) return;
      input.focus();
      if (list.hidden) open(""); else close();
    });
    if (clear) {
      clear.addEventListener("mousedown", function (event) {
        event.preventDefault();
      });
      clear.addEventListener("click", function () {
        commit("");
        input.focus();
      });
    }
    // A change made elsewhere (restoring a preserved value, another script)
    // is reflected in the box.
    select.addEventListener("change", syncDisplay);
    // The identity-document script disables the controls of the inactive panel.
    function mirrorDisabled() {
      input.disabled = select.disabled;
      toggle.disabled = select.disabled;
      if (clear) clear.hidden = select.disabled || !selectedOption();
    }
    if (typeof MutationObserver === "function") {
      new MutationObserver(mirrorDisabled).observe(select, {
        attributes: true,
        attributeFilter: ["disabled"]
      });
    }
    mirrorDisabled();
    syncDisplay();
  }

  // ---------------------------------------------------------- char counter
  // "n / max" under a long text (UX-3, S-12). It counts code points of the
  // text after CR LF becomes LF and NFC, exactly as the server does, so the
  // two never disagree. It never blocks typing; a polite status speaks at
  // 80% and at the limit only.
  function countCodePoints(text) {
    var normalized = (text || "").replace(/\r\n?/g, "\n");
    if (normalized.normalize) normalized = normalized.normalize("NFC");
    normalized = normalized.trim();
    return Array.from(normalized).length;
  }

  function setupCharCounter(field) {
    if (field.getAttribute("data-char-counter-ready") === "true") return;
    field.setAttribute("data-char-counter-ready", "true");
    var max = parseInt(field.getAttribute("data-asc-char-counter"), 10);
    if (!max) return;
    var visible = document.createElement("p");
    visible.className = "form-text asc-char-counter";
    visible.setAttribute("aria-hidden", "true");
    var bdi = document.createElement("bdi");
    bdi.setAttribute("dir", "ltr");
    visible.appendChild(bdi);
    var status = document.createElement("p");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    field.insertAdjacentElement("afterend", visible);
    visible.insertAdjacentElement("afterend", status);
    var lastBand = "";
    function update() {
      var count = countCodePoints(field.value);
      bdi.textContent = count + " / " + max;
      var band = count > max ? "over" : count >= Math.ceil(max * 0.8) ? "near" : "";
      visible.classList.toggle("is-near", band === "near");
      visible.classList.toggle("is-over", band === "over");
      if (band !== lastBand) {
        status.textContent = band ? count + " / " + max : "";
        lastBand = band;
      }
    }
    field.addEventListener("input", update);
    update();
  }

  // ------------------------------------------------------ accommodation
  // The support details are shown only for a "Yes" answer (UX-3). With
  // scripting off every control stays visible; the server keeps details for
  // "Yes" only either way.
  function setupAccommodation(section) {
    if (section.getAttribute("data-accommodation-ready") === "true") return;
    section.setAttribute("data-accommodation-ready", "true");
    var details = section.querySelector("[data-accommodation-details]");
    var radios = section.querySelectorAll('input[name="accommodation_answer"]');
    if (!details || !radios.length) return;
    function sync() {
      var chosen = section.querySelector('input[name="accommodation_answer"]:checked');
      details.hidden = !(chosen && chosen.value === "YES");
    }
    Array.prototype.forEach.call(radios, function (radio) {
      radio.addEventListener("change", sync);
    });
    sync();
  }

  // --------------------------------------------------------- identity panels
  // The identity step shows only the panel of the selected document (NIN or
  // passport); the controls of the hidden panel are `disabled`, so they are
  // never submitted. Without scripting both panels stay visible and the
  // server validates only the selected path (UXR-C1, UXR-F01).
  //
  // This runs in the shared lifecycle: on load and again after every htmx
  // `htmx:after:swap`. In the vendored htmx 4.0.0 that event fires after the
  // settle step has restored the new radios' `checked` state; an inline script
  // in the swapped content ran before it, saw no selected radio and hid both
  // panels. The radio listeners are bound once per element, and the panels are
  // re-synchronized on every call, so a repeated setup is harmless.
  function setupIdentityPanels(form) {
    var name = form.getAttribute("data-identity-panels");
    if (!name) return;
    var radios = form.querySelectorAll('input[type="radio"][name="' + name + '"]');
    if (!radios.length) return;
    function sync() {
      var chosen = form.querySelector('input[type="radio"][name="' + name + '"]:checked');
      var active = chosen ? chosen.value : null;
      Array.prototype.forEach.call(form.querySelectorAll("[data-identity-panel]"), function (panel) {
        var isActive = panel.getAttribute("data-identity-panel") === active;
        panel.classList.toggle("d-none", !isActive);
        Array.prototype.forEach.call(panel.querySelectorAll("input, select, textarea"), function (control) {
          control.disabled = !isActive;
        });
      });
    }
    Array.prototype.forEach.call(radios, function (radio) {
      if (radio.getAttribute("data-identity-ready") === "true") return;
      radio.setAttribute("data-identity-ready", "true");
      radio.addEventListener("change", sync);
    });
    sync();
  }

  // --------------------------------------------------------- topic picker
  // Grouped interest topics (UX-2, M20): a filter box and removable chips for
  // the selected topics. The checkboxes stay the submitted value; the chips
  // only mirror them. Each chip is a button named "Remove <topic>" and can be
  // reached and used with the keyboard; chips wrap on small screens.
  function setupTopicPicker(fieldset) {
    if (fieldset.getAttribute("data-topic-picker-ready") === "true") return;
    fieldset.setAttribute("data-topic-picker-ready", "true");
    var boxes = Array.prototype.slice.call(fieldset.querySelectorAll('input[type="checkbox"]'));
    if (!boxes.length) return;
    var legend = fieldset.querySelector("legend");
    var filterWrap = document.createElement("div");
    filterWrap.className = "asc-topic-filter";
    var filterId = "asc-topic-filter-" + Math.random().toString(36).slice(2, 8);
    var filterLabel = document.createElement("label");
    filterLabel.className = "visually-hidden";
    filterLabel.setAttribute("for", filterId);
    filterLabel.textContent = fieldset.getAttribute("data-label-filter") || "Filter";
    var filter = document.createElement("input");
    filter.type = "search";
    filter.id = filterId;
    filter.className = "form-control";
    filter.autocomplete = "off";
    filter.placeholder = fieldset.getAttribute("data-label-filter") || "";
    filterWrap.appendChild(filterLabel);
    filterWrap.appendChild(filter);
    var chips = document.createElement("ul");
    chips.className = "asc-topic-chips";
    chips.setAttribute("aria-label", fieldset.getAttribute("data-label-selected") || "Selected");
    var empty = document.createElement("p");
    empty.className = "asc-muted asc-topic-empty";
    empty.textContent = fieldset.getAttribute("data-label-none") || "";
    empty.hidden = true;
    var status = document.createElement("p");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    legend.insertAdjacentElement("afterend", filterWrap);
    filterWrap.insertAdjacentElement("afterend", chips);
    chips.insertAdjacentElement("afterend", empty);
    empty.insertAdjacentElement("afterend", status);

    function labelOf(box) {
      var label = box.closest("label");
      return label ? label.textContent.replace(/\s+/g, " ").trim() : box.value;
    }

    function renderChips() {
      chips.textContent = "";
      boxes.forEach(function (box) {
        if (!box.checked) return;
        var item = document.createElement("li");
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "asc-topic-chip";
        var text = document.createElement("bdi");
        text.textContent = labelOf(box);
        chip.appendChild(text);
        var cross = document.createElement("span");
        cross.setAttribute("aria-hidden", "true");
        cross.textContent = "\u00d7";
        chip.appendChild(cross);
        chip.setAttribute(
          "aria-label",
          (fieldset.getAttribute("data-label-remove") || "Remove") + " " + labelOf(box)
        );
        chip.addEventListener("click", function () {
          box.checked = false;
          box.dispatchEvent(new Event("change", { bubbles: true }));
          renderChips();
          status.textContent = "";
          var next = chips.querySelector("button");
          (next || filter).focus();
        });
        item.appendChild(chip);
        chips.appendChild(item);
      });
      chips.hidden = !chips.children.length;
    }

    filter.addEventListener("input", function () {
      var query = normalise(filter.value.trim());
      var shown = 0;
      Array.prototype.forEach.call(fieldset.querySelectorAll("[data-topic-group]"), function (group) {
        var visibleInGroup = 0;
        Array.prototype.forEach.call(group.querySelectorAll("[data-topic]"), function (label) {
          var match = !query || normalise(label.textContent).indexOf(query) !== -1;
          label.hidden = !match;
          if (match) visibleInGroup += 1;
        });
        group.hidden = visibleInGroup === 0;
        shown += visibleInGroup;
      });
      empty.hidden = shown !== 0;
    });
    boxes.forEach(function (box) {
      box.addEventListener("change", renderChips);
    });
    renderChips();
  }

  // ------------------------------------------------------- calling code
  // A pasted or typed "+..." or "00..." number selects its country (UX-2,
  // S-10) when exactly one listed country has that calling code and the
  // current choice does not already match it (+1, +7 and +262 are shared, so
  // those keep the current choice). The change is announced politely. The
  // server parses the number again and stores its own region either way.
  function setupCallingCodeSwitch(input) {
    if (input.getAttribute("data-calling-ready") === "true") return;
    input.setAttribute("data-calling-ready", "true");
    var codes;
    try {
      codes = JSON.parse(input.getAttribute("data-calling-codes") || "{}");
    } catch (e) { return; }
    var form = input.form;
    var source = form && form.elements.namedItem(input.getAttribute("data-region-source"));
    if (!source || source instanceof RadioNodeList) return;
    var byCode = {};
    Object.keys(codes).forEach(function (region) {
      var code = String(codes[region]);
      (byCode[code] = byCode[code] || []).push(region);
    });
    var status = document.createElement("p");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    input.insertAdjacentElement("afterend", status);
    function check() {
      var raw = asciiDigits(input.value).replace(/[\s.\-()\/]/g, "");
      if (raw.indexOf("00") === 0) raw = "+" + raw.slice(2);
      if (raw.charAt(0) !== "+") return;
      var digits = raw.slice(1);
      for (var length = 3; length >= 1; length--) {
        var candidate = digits.slice(0, length);
        var regions = byCode[candidate];
        if (!regions) continue;
        if (regions.indexOf(source.value) !== -1) return;
        if (regions.length !== 1) return;
        source.value = regions[0];
        source.dispatchEvent(new Event("change", { bubbles: true }));
        var option = source.options[source.selectedIndex];
        status.textContent = (input.getAttribute("data-switched-text") || "").replace(
          "%(country)s",
          option ? option.textContent.trim() : regions[0]
        );
        return;
      }
    }
    input.addEventListener("input", check);
    input.addEventListener("change", check);
  }

  function setupEnhancements(root) {
    var scope = root || document;
    clearParsedNoscript(scope);
    Array.prototype.forEach.call(scope.querySelectorAll("[data-asc-picker]"), setupPicker);
    Array.prototype.forEach.call(scope.querySelectorAll("select[data-group-follows]"), setupGroupFollow);
    Array.prototype.forEach.call(scope.querySelectorAll("input[data-asc-otp]"), setupOtp);
    Array.prototype.forEach.call(scope.querySelectorAll("input[data-asc-digit-counter]"), setupDigitCounter);
    Array.prototype.forEach.call(scope.querySelectorAll("select[data-asc-combobox]"), setupCombobox);
    Array.prototype.forEach.call(scope.querySelectorAll("input[data-region-examples]"), setupRegionExample);
    Array.prototype.forEach.call(scope.querySelectorAll("textarea[data-asc-char-counter]"), setupCharCounter);
    Array.prototype.forEach.call(scope.querySelectorAll("[data-accommodation]"), setupAccommodation);
    Array.prototype.forEach.call(scope.querySelectorAll("form[data-identity-panels]"), setupIdentityPanels);
    Array.prototype.forEach.call(scope.querySelectorAll("[data-asc-topic-picker]"), setupTopicPicker);
    Array.prototype.forEach.call(scope.querySelectorAll("input[data-calling-codes]"), setupCallingCodeSwitch);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setupEnhancements(document); });
  } else {
    setupEnhancements(document);
  }
  document.addEventListener("htmx:after:swap", function () { setupEnhancements(document); });
})();
