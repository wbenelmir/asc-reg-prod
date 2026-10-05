# Operational MFA: options comparison and enforcement contract (MFA-01)

Package: P4-4-C1. Date: 2026-10-01. Status: **proposal for owner decision**. It selects nothing,
installs nothing and approves nothing.

> **Status update (P4-4-C3, 2026-10-01).** The owner revised MFA-01: operational users sign in with
> email and password in staging and production (decision gate §17.9, amendment A-11). Sign-in MFA is
> therefore no longer a requirement, and `release_readiness` reports `operational_sign_in` (READY,
> MFA not enforced) instead of `operational_mfa: BLOCKED`. This comparison is kept unchanged below
> as the historical record. It stays relevant only for a future step-up provider for sensitive
> operations (the emergency wipe, open question STEPUP-01), which the revision does not cover.

* Mandatory MFA for operational users is already decided (TRD §16.1, PRD FR-AUTH-009,
  AF-AUTH-03). Only the provider family and its operating rules are open.
* Finding P44-F06 (P4-4) and review finding R-01 stay **OPEN** and are a **release blocker**.
  `manage.py release_readiness` reports `operational_mfa: BLOCKED` until enforcement exists.
* No dependency was added, no provider was contacted, and no authenticator protocol was written.
  Research used official package metadata, official documentation and official repositories only,
  read on 2026-10-01. No project data was sent anywhere.

## 1. Current state (verified in source, 2026-10-01)

| Fact | Evidence |
| --- | --- |
| Operational authentication has one entry point: `/accounts/ops/sign-in/`. A correct password calls `login()` and creates the operational session; no second factor is asked. | `apps/accounts/views.py` `operational_sign_in`; regression `test_the_mfa_fact_agrees_with_the_actual_sign_in_behaviour` |
| Django admin is installed only in local settings, so it is not a staging or production entry point. | `config/settings/local.py` |
| Entry device and operator sessions build on that operational session; they are not a separate sign-in. | `apps/entry/api/views.py` `OperationalSessionUser`, `apps/entry/session_state.py` |
| Participants sign in by email OTP into a separate participant session; they are not operational users and are out of MFA-01 scope. | `apps/accounts/participant_auth.py` |
| Protected operational routes check `request.user.is_authenticated` plus permission and scope (146 routes in the P4-4 route inventory guard). They have no notion of an incomplete authentication. | `apps/core/tests/test_p4_4_route_access.py` |
| `OperationalSessionExpiryMiddleware` re-checks account status and the inactivity and absolute lifetimes on every request. | `apps/accounts/middleware.py` |
| The MFA boundary (`apps/accounts/mfa.py`) is used only for the emergency-wipe step-up. With no provider it fails closed. | `apps/entry/services/offline_devices.py` |
| Staging and production refuse to start without `MFA_BACKEND`, but a configured name does not enforce anything at sign-in. | `config/settings/validation.py` |
| Password sign-in has per-email and per-network attempt limits and is audited (P44-F03). | `apps/accounts/operational_sign_in.py` |

## 2. Options

### Option A: institutional SSO / identity provider with MFA

Availability: **UNKNOWN** (owner statement, 2026-10-01). Nothing below assumes that it exists or
that it does not.

| Aspect | Assessment |
| --- | --- |
| Python 3.14 / Django 5.2 | Depends on the client library chosen for the protocol (OIDC or SAML). **Not assessed**: no protocol is known. |
| Maintenance and security | Factor storage, recovery and lockout move to the institution. The application still owns session creation, the claim checks and the authorization mapping. |
| Factors | Whatever the IdP enforces. **UNKNOWN**. |
| Proof of MFA per sign-in | Requires the IdP to assert it for each authentication (OIDC `amr` / `acr`, or the SAML `AuthnContextClassRef`) and the application to reject an assertion without it. **UNKNOWN** whether the IdP does this. |
| Enrollment, recovery, reset | Institutional helpdesk process. **UNKNOWN**. |
| Coverage | **Risk:** external security users (named, time-bounded, gate-scoped accounts from other organizations) and temporary operators may have no institutional identity. A second, local path would then still be needed, so a local MFA option may be required anyway. |
| Emergency-wipe step-up | Needs forced reauthentication with a freshness bound (OIDC `prompt=login` with `max_age`, then checking `auth_time` and `amr`). **UNKNOWN** whether supported. |
| Audit | Application audits the assertion outcome (no token content); the IdP keeps its own log. |
| Integration effort | Medium to high: a new client dependency, redirect and callback views, account linking to `OperationalUser` by a stable subject (never by a mutable email alone), group mapping or local groups, logout. |
| Operating requirements | IdP availability becomes a sign-in dependency at the event; an outage blocks all operational sign-in unless a break-glass path is approved. |

### Option B: django-allauth with its MFA app (`allauth.account` + `allauth.mfa`)

| Aspect | Verified fact (source) | Unknown or to verify |
| --- | --- | --- |
| Version and licence | 65.19.6, uploaded 2026-09-30; MIT; pure-Python wheel (PyPI JSON) | — |
| Python 3.14 / Django 5.2 | Classifiers list Python 3.14 and Django 5.2. The upstream `noxfile.py` sets `DJANGO_LTS = "5.2"` and tests it on Python 3.10 to 3.14 (Codeberg, main branch). `requires_python >=3.10`. | Not installed or run in this project. Compatibility of the `mfa` extra's `fido2` (<3, >=1.1.2) with the locked `cryptography` 50.x is **not established**. |
| Maintenance and security | Frequent releases (65.19.1 on 2026-08-13 to 65.19.6 on 2026-09-30). Recent security notices include MFA: TOTP enrollment verification rate limiting (65.19.2) and TOTP and recovery-code race conditions (65.19.3) (ChangeLog). | The recent MFA fixes show active response and also an area with recent defects: pin exactly and monitor releases. |
| Factors | TOTP, recovery codes (view, download, regenerate), WebAuthn credentials and passkey login; WebAuthn is off by default. `MFA_SUPPORTED_TYPES` defaults to recovery codes and TOTP (official docs). | — |
| Sign-in flow | Login passes through "login stages" with `ACCOUNT_LOGIN_TIMEOUT` (900 s) (official docs). | Whether the Django session user is set only after the MFA stage must be **verified in an integration spike** before relying on it. |
| Mandatory enrollment | **No setting forces MFA enrollment** for all users (official MFA settings list). Enforcement must be added in the project (an adapter or middleware that confines an un-enrolled operational user to enrollment). | — |
| Recovery and reset | Recovery codes; `MFA_RECOVERY_CODES_SHOW_ONCE` (default False). | Administrative reset of another user's authenticators: process and permission must be designed here. |
| Reauthentication (step-up) | `ACCOUNT_REAUTHENTICATION_TIMEOUT` (300 s) and reauthentication flows (official docs). | Mapping to the emergency-wipe `MfaStepUpBackend` must be built and tested here. |
| Trust-this-browser | `MFA_TRUST_ENABLED` (default False) would skip MFA for a cookie lifetime. It must stay False unless the owner approves otherwise. | — |
| Session and auth backend | Requires `allauth.account`, `AccountMiddleware`, and recommends `allauth.account.auth_backends.AuthenticationBackend` (quickstart). | Coexistence with the custom `OperationalUser`, the P44-F03 audit-based limits, the participant OTP session and the shared session: integration spike. |
| Dependencies | `django-allauth[mfa]` adds `qrcode` and `fido2` (PyPI `requires_dist`). | Their transitive dependencies; the lock file impact. |
| Integration effort | High: new apps, models and migrations (account e-mail addresses, authenticators), replacing or wrapping the operational sign-in view, re-templating every used allauth page in Bootstrap 5 with EN/FR/AR and RTL, keeping the no-inline CSP, disabling public sign-up and unused flows. | Exact template set and migration count. |

### Option C: django-otp (optionally with django-two-factor-auth as the UI layer)

| Aspect | Verified fact (source) | Unknown or to verify |
| --- | --- | --- |
| Version and licence | 1.7.3, uploaded 2026-09-06; the Unlicense (PyPI JSON) | — |
| Python 3.14 / Django 5.2 | `requires_python >=3.8`, `django >=4.2`; classifiers name no version. The upstream test matrix (`pyproject.toml`) runs Python 3.10 with Django 5.2, Python 3.12 with Django 6.0 and Python 3.14 with Django 6.1. | **The exact pair Python 3.14 + Django 5.2 is not in the upstream matrix.** Compatibility is plausible (pure Python) but **not established**. |
| Maintenance and security | The README states the project "is stable and maintained, but is no longer actively used by the author and is not seeing much ongoing investment". No `SECURITY.md` and no published advisory on GitHub. Three bug-fix releases in September 2026 (ChangeLog). | Long-term maintenance capacity. |
| Factors | Built-in plugins: TOTP, HOTP, static (backup) tokens, e-mail tokens. YubiKey and Twilio SMS are separate plugins; WebAuthn is not built in (official docs). | — |
| Sign-in flow | The documented model logs the user in with the password first, then verifies; `request.user.is_verified()` and `otp_required` gate views; `OTPAdminSite` gates the admin (official docs). | **Risk for this project:** an authenticated but unverified session exists. Every operational route would need a global verified-session check, or an unverified session would pass today's `is_authenticated` checks. |
| Throttling | Exponential failure throttling per device (`OTP_TOTP_THROTTLE_FACTOR`, ...) (official docs). | — |
| Dependencies | Core: Django only. QR rendering through the `segno` extra, and `segno` is already a project dependency. | — |
| UI layer | django-two-factor-auth 1.18.1, uploaded 2025-09-27, MIT; classifiers list Django 4.2 to 5.2 and Python 3.9 to 3.13, **not 3.14**; it adds `django-phonenumber-field`, `django-formtools` and `qrcode`. | Python 3.14 support not declared. Without it, project-owned views (calling the library, never re-implementing TOTP) are needed. |
| Integration effort | Medium: models and migrations per plugin; own enrollment, verification and reset views; the global verified-session gate; templates in EN/FR/AR with RTL. | — |

## 3. Recommendation (conditional, not a selection)

1. **If** the owner confirms an institutional IdP that (a) asserts MFA per authentication in a
   machine-checkable claim, (b) supports forced reauthentication with a freshness bound, (c) covers
   every operational population including external security and temporary accounts, and (d) has an
   availability commitment suitable for event days, **prefer Option A**, and keep a reviewed local
   break-glass path.
2. **Otherwise, or if (c) fails, prefer Option B (django-allauth MFA, TOTP plus recovery codes;
   WebAuthn later by separate decision)** because:
   * its upstream matrix tests exactly Django 5.2 on Python 3.14;
   * it is actively maintained and publishes security notices, including MFA ones;
   * its staged login and reauthentication model fits "no privileged session before the second
     factor" and the emergency-wipe step-up better than a verified-after-login model.

   This is conditional on a time-boxed integration spike in an isolated environment that verifies
   the unknowns above (session-user timing, `fido2` and `cryptography` resolution, coexistence with
   the custom user and the participant session).
3. Option C is the fallback if Option B's spike fails. Its main costs are the verified-after-login
   model, which needs a global gate, the untested Python 3.14 + Django 5.2 pair, and the stated low
   maintenance investment.

## 4. Approvals and facts needed (MFA-01)

1. **Fact:** does an institutional IdP with MFA exist? If yes: protocol, MFA claim, reauthentication
   support, population coverage, availability, and a technical contact.
2. **Decision:** the option (A, B or C), and approval to run a bounded integration spike.
3. **Dependency approval:** the exact packages and versions, and a `uv.lock` update (none exists
   today).
4. **Migration approval:** new authentication tables (isolated PostgreSQL first; no development
   migrate without separate approval).
5. **Factor policy:** TOTP plus recovery codes as the baseline? WebAuthn or passkeys? E-mail or SMS
   codes are not recommended for operational staff.
6. **Lifecycle policy:** who may reset another user's MFA (a dedicated permission, audited, with
   session revocation); the break-glass procedure for the superuser; recovery codes shown once.
7. **Step-up freshness** for the emergency wipe (proposal: at most 5 minutes, bound to the wipe
   purpose).
8. **Trust-this-browser:** off (proposed).
9. **Scope:** every `OperationalUser`, including superusers, external security accounts and
   temporary accounts (proposed: no exception).
10. **Schedule:** the implementation package must finish before release. A deferral to P4-8 is **not
    approved** by any record; it needs an explicit owner decision.

## 5. Enforcement contract for any future approved integration

Whichever option is approved, the integration must satisfy all of the following, each with a
direct regression. Absent, unavailable, rejected or stale MFA must never create a privileged
session.

1. **No privileged session before the second factor.** The password step stores only a pending
   state, never the Django authenticated user. The pending state is bound to the user id, that
   user's session authentication hash, a single-use flow nonce and a creation time. It expires
   after a short timeout, and is discarded on success, on failure past the attempt bound, on
   sign-out and on timeout. `login()` runs once, after the second factor, and rotates the session
   key.
2. **If a library logs in before verification** (Option C), a global middleware treats an
   authenticated but unverified operational session as unauthenticated on every route except
   verification, enrollment and sign-out. The route inventory guard asserts this for every
   operational access class.
3. **Fail closed.** Provider unavailable, misconfigured, raising, or answering anything other than an
   explicit success is a refusal. No setting may disable enforcement in staging or production, and
   settings validation refuses one. The release-readiness assessment stays BLOCKED until enforcement
   and a loadable provider exist.
4. **Binding.** A verification completes only the pending login of the same user and the same flow.
   It cannot be replayed or moved to another user, browser session or purpose. Code reuse within the
   validity window is refused, using the library's replay protection.
5. **Freshness.** The time of the last second-factor verification is stored in the session. The
   emergency wipe requires a verification newer than the approved bound and bound to the wipe
   purpose, through the existing `MfaStepUpBackend` boundary. The operational inactivity and absolute
   lifetimes still apply.
6. **Enrollment.** An operational user with no factor reaches only the enrollment flow, after the
   password step, and gets no operational permission until enrollment completes and is verified once.
7. **Reset and recovery.** A dedicated permission, a reason, audit, revocation of all of that user's
   sessions, and immediate re-enrollment. Recovery codes are hashed, single use and shown once.
8. **Audit.** Enrollment, success, failure, throttle, reset and recovery-code use are audited
   without codes, secrets or seeds, using the P44-F03 fingerprint conventions.
9. **Throttling.** The P44-F03 password limits stay. Second-factor attempts are bounded per user and
   per pending flow, and audited.
10. **Truthful status.** `apps.accounts.mfa.OPERATIONAL_SIGN_IN_MFA_ENFORCED` changes to True only in
    the same change that adds enforcement; the consistency regression fails otherwise.

## 6. Sources (read-only, 2026-10-01)

* PyPI JSON: `https://pypi.org/pypi/django-allauth/json`, `.../django-allauth/65.19.6/json`,
  `https://pypi.org/pypi/django-otp/json`, `.../django-otp/1.7.3/json`,
  `https://pypi.org/pypi/django-two-factor-auth/json`, `.../1.18.1/json`.
* django-allauth: `https://docs.allauth.org/en/latest/mfa/introduction.html`,
  `.../mfa/configuration.html`, `.../account/configuration.html`,
  `.../installation/quickstart.html`; `noxfile.py` on `codeberg.org/allauth/django-allauth`; the
  ChangeLog through its GitHub mirror (`github.com/pennersr/django-allauth`).
* django-otp: `https://django-otp-official.readthedocs.io/en/stable/overview.html`, `.../auth.html`;
  `github.com/django-otp/django-otp` (README, `pyproject.toml`, `CHANGES.rst`, the security page).

These were read through a summarising fetch tool. Facts that would decide the selection (versions,
dates, matrices, settings) should be re-read by the reviewer before approval. Nothing here is a
compatibility test result.
