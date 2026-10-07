/*
 * Staff sign-in security image: "New image" without leaving the page
 * (apps.accounts.captcha_guard; template accounts/_staff_captcha.html).
 *
 * Progressive enhancement: the control is a real link to the sign-in page, so
 * without JavaScript it reloads the page, which issues a new image. With
 * JavaScript it POSTs (CSRF token from the surrounding form) to the refresh
 * endpoint, swaps the hidden key and the image, keeps the typed email, clears
 * and focuses the answer and announces the change in a polite live region.
 * Any failure falls back to the reload. The server binds the new challenge to
 * this session; the answer never reaches the browser.
 *
 * One delegated document listener (bound once), so it also works after an
 * htmx boosted swap re-renders the form. No inline script (CSP).
 */
(function () {
  "use strict";

  if (window.ascCaptchaBound) {
    return;
  }
  window.ascCaptchaBound = true;

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("[data-captcha-refresh]") : null;
    if (!link) {
      return;
    }
    var box = link.closest("[data-staff-captcha]");
    var form = box ? box.closest("form") : null;
    var token = form ? form.querySelector('input[name="csrfmiddlewaretoken"]') : null;
    if (!box || !token || !window.fetch) {
      return; // follow the link: the page reloads with a new image
    }
    event.preventDefault();
    var body = new URLSearchParams({ form: box.getAttribute("data-captcha-form") || "" });
    fetch(box.getAttribute("data-refresh-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "X-CSRFToken": token.value,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: body,
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then(function (data) {
        box.querySelector("[data-captcha-key]").value = data.key;
        box.querySelector("[data-captcha-image]").setAttribute("src", data.image_url);
        var answer = box.querySelector('input[name="captcha_answer"]');
        answer.value = "";
        answer.removeAttribute("aria-invalid");
        answer.classList.remove("is-invalid");
        answer.focus();
        var status = box.querySelector("[data-captcha-status]");
        if (status) {
          status.textContent = box.getAttribute("data-text-refreshed") || "";
        }
      })
      .catch(function () {
        window.location.assign(link.getAttribute("href"));
      });
  });
})();
