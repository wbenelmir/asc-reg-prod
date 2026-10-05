/*
 * Project glue for the vendored ALTCHA widget (UX-C2, owner decision UX-D01
 * option M). Loaded as a module after `vendor/altcha/altcha.min.js`, the
 * strict-CSP build that ships no worker of its own.
 *
 * 1. Registers the one algorithm the server issues (PBKDF2/SHA-256) with the
 *    vendored worker file, served from this origin: no blob or remote URL.
 * 2. Starts the check once the widget is ready. The server fetches nothing
 *    else, and the widget posts its payload in its own `human_check` input.
 * 3. Holds a submission made before the check completes, then sends it once.
 * 4. When a challenge expires, asks the widget for a new one (a new signed,
 *    session-bound challenge from the same URL) without reloading the page,
 *    so the typed email is kept.
 * The server verifies everything and fails closed; nothing here can bypass it.
 */
const box = document.querySelector("[data-asc-altcha]");

if (box && globalThis.$altcha) {
  const form = box.closest("form");
  const widget = box.querySelector("altcha-widget");
  const status = box.querySelector("[data-human-check-status]");
  const workerUrl = box.getAttribute("data-worker-url");
  let waiting = null;

  globalThis.$altcha.algorithms.set("PBKDF2/SHA-256", () => new Worker(workerUrl));

  const setState = (state) => form.setAttribute("data-human-check-state", state);
  const say = (text) => {
    if (status) status.textContent = text || "";
  };

  const whenReady = () =>
    customElements.whenDefined("altcha-widget").then(
      () =>
        new Promise((resolve) => {
          let tries = 0;
          const poll = () => {
            if (typeof widget.verify === "function" || tries > 200) {
              resolve();
              return;
            }
            tries += 1;
            window.setTimeout(poll, 25);
          };
          poll();
        })
    );

  const start = () => {
    setState("running");
    say("");
    try {
      widget.verify();
    } catch (error) {
      setState("failed");
      say(status && status.getAttribute("data-text-failed"));
    }
  };

  widget.addEventListener("statechange", (event) => {
    const state = event.detail && event.detail.state;
    if (state === "verified") {
      setState("solved");
      say("");
      if (waiting) {
        const submitter = waiting;
        waiting = null;
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit(submitter instanceof HTMLElement ? submitter : undefined);
        } else {
          form.submit();
        }
      }
    } else if (state === "expired") {
      // A new challenge, never a page reload: the email field keeps its value.
      setState("expired");
      start();
    } else if (state === "error") {
      setState("failed");
      say(status && status.getAttribute("data-text-failed"));
    } else if (state === "verifying") {
      setState("running");
    }
  });

  form.addEventListener("submit", (event) => {
    if (form.getAttribute("data-human-check-state") === "solved") return;
    event.preventDefault();
    waiting = event.submitter || true;
    const current = form.getAttribute("data-human-check-state");
    if (current === "failed" || current === "expired" || current === "pending") start();
  });

  whenReady().then(start);
}
