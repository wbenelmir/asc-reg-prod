# Staff sign-in security image (CAPTCHA)

The operational (staff) sign-in asks for the email address, the password and
the characters of a distorted-letter image. The participant email code and its
automatic ALTCHA check are unchanged.

## Design

* Self-hosted: `django-simple-captcha` 0.7 stores challenges in the
  application database (`captcha_captchastore`) and renders the PNG locally
  with Pillow and its bundled font. No external service, account or key.
* Only its store and renderer are used. Its form field, URLconf, refresh and
  audio views are not mounted (system check `accounts.E003`). The project's
  guard `apps/accounts/captcha_guard.py` issues and verifies challenges;
  routes: `/accounts/ops/sign-in/security-image/<key>/` (the image, served only
  to the session it was issued to) and
  `/accounts/ops/sign-in/security-image-refresh/` (CSRF-protected POST, returns
  the new key and image URL only).
* Answers come from `secrets` over an alphabet without look-alike characters;
  the answer exists only in the database row, never in markup, URLs, the
  session or logs, and is compared in constant time.
* Bound to the browser session and to the staff sign-in purpose. One attempt
  per image: every submission consumes the image, whatever the outcome (empty,
  malformed, wrong or correct). The consumption locks the row, deletes it and
  commits before the answer is compared and before the password is checked,
  so simultaneous submissions of one image succeed at most once and a failed
  sign-in can never restore it. `ATOMIC_REQUESTS` must stay off (system check
  `accounts.E001`, deployment validation).
* Order: the image is checked first. A refused image never reaches the password
  check and never counts towards the per-email or per-network sign-in limits;
  a correct image with a wrong password counts as before.
* Expiry: `STAFF_CAPTCHA_TIMEOUT_MINUTES` (default 5, accepted 1-30). Length:
  `STAFF_CAPTCHA_LENGTH` (default 5, accepted 4-8). `CAPTCHA_TEST_MODE` is
  always False (system check `accounts.E002`, deployment validation); tests
  read answers from their own test database.
* Issuance (page and refresh) is limited per client network by the shared
  challenge counter already used by the participant ALTCHA (Redis in staging
  and production), in its own bucket; a refused request creates no row and no
  session. Expired rows are removed when a new image is issued and by the
  periodic task `accounts.purge_expired_staff_captchas` (every 15 minutes, in
  bounded batches). `python manage.py captcha_clean` remains available.
* Image, page and refresh responses are not cacheable.

## Use

* After a refused answer the page shows a new image with an empty answer field
  and keeps the email address; the error is linked from the error summary.
* "New image" replaces the image in place with JavaScript (keeps the typed
  email, focuses the answer, announces the change); without JavaScript it is a
  link that reloads the page with a new image.
* Two tabs, or a reload, replace each other's image: submitting the older page
  shows "not valid for this page".

## Limitations

* **No non-visual alternative.** The image has a text alternative stating
  only its length; there is no audio or other non-visual option. A person who
  cannot read the image cannot sign in alone. No accessibility conformance is
  claimed for this control.
* Basic bot deterrence only: OCR or human-solving services can defeat it. The
  password, the sign-in limits, CSRF, authorization and session lifetimes
  remain the real controls.
* One attempt per image: a typo costs a new image.
* The renderer seeds Python's global `random` with the key while drawing (the
  library's own behaviour); the challenge text itself comes from `secrets`.
