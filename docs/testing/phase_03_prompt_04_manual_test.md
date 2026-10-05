# Phase 3 Prompt 4 — Online entry manual test (local, synthetic data only)

Use only synthetic people, `example.test` addresses, and invented identity
values. Never use a real NIN, passport number, photograph, or pass.

## 0. Preconditions

```text
uv run --env-file .env python manage.py migrate
uv run --env-file .env python manage.py check
uv run --env-file .env python manage.py runserver
```

Local configuration needs a published ACTIVE verification key and an
approved, fully assigned registration with an ACTIVE Digital Entry Pass
(Phase 3 Prompt 2 manual test, §§1–4). There is no venue/rule
administration UI in this prompt: create the synthetic venue structure
and rules from `manage.py shell` (or the local-only Django admin):

```python
from apps.events.models import EventEdition
from apps.events.services import create_venue, create_zone, create_gate
from apps.accreditation.models import AccessRule, AccessProfile
from apps.accounts.models import OperationalUser

admin = OperationalUser.objects.get(email_normalized="<your local superuser>")
event = EventEdition.objects.get(code="<your local event code>")
venue = create_venue(event_edition=event, code="V1", name="Main venue", actor=admin)
main = create_zone(venue=venue, code="MAIN", name="Main area", actor=admin)
vip = create_zone(venue=venue, code="VIP", name="VIP lounge", actor=admin)
gate = create_gate(venue=venue, code="GA", name="Gate A", default_zone=main, actor=admin)
profile = AccessProfile.objects.get(event_edition=event, code="<the assigned profile code>")
AccessRule.objects.create(
    event_edition=event, access_profile=profile, code="MAIN_ALLOW", name="Main area", zone=main
)
```

Create three synthetic operational users and scoped memberships:
`Entry Device Administrators` (event scope), `Entry Operators` (event +
gate GA), `Entry Supervisors` (event + gate GA).

## 1. Device registration and enrollment

1. Sign in as the device administrator; open
   `/ops/entry/events/<event uuid>/devices/`.
2. Register "Gate A tablet 1": gate GA, zones MAIN (+VIP), all methods,
   expiry tomorrow. **Expected:** a one-time activation code page, marked
   single-use and expiring; reloading the device list never shows it again.
3. In a different browser profile (the "device"), sign in as the device
   administrator, open `/entry/device/activate/`, enter the code.
   **Expected:** redirect to `/entry/`; browser dev tools show cookie
   `asc_entry_device` with `HttpOnly`, `SameSite=Strict`, `Path=/entry/`.
4. Enter the same code again. **Expected:** "This activation code cannot be
   used."
5. Sign out on the device.

## 2. Checkpoint set-up and cross-checkpoint denial

1. On the device, sign in as the operator (`/accounts/ops/sign-in/?next=/entry/`).
   **Expected:** lands on `/entry/`, gate GA fixed, zone choice limited to
   the device's permitted zones.
2. Start the session on MAIN. **Expected:** `/entry/verify/` with the status
   bar "Mode: Online", gate, zone, device, operator.
3. In a normal browser (no device cookie), sign in as the operator and open
   `/entry/`. **Expected:** 403 "Entry device not enrolled".
4. Create a second operator scoped to another gate only; try to start a
   session on the device. **Expected:** refused.

## 3. Online verification

1. Paste or scan the synthetic pass token into the QR field and press
   Enter. **Expected:** green "✓ Verified", name, reference, Badge Type,
   Admit / Do not admit / Send to review desk. No token anywhere in the page.
2. Click Admit. **Expected:** "Entry recorded: admitted."; details cleared.
3. Scan again. **Expected:** blue "Verified — advisory", "Previously
   admitted at HH:MM through Gate A".
4. Revoke the pass (credential page, Prompt 2) and scan the old token.
   **Expected:** red "✕ Do not admit — Pass revoked"; no Admit button.
5. Tamper with one character of a token. **Expected:** "Credential not
   valid", no participant details.
6. On a VIP-zone session (MAIN-only profile), scan a valid pass.
   **Expected:** "Not permitted at this checkpoint".
7. Wait `ENTRY_RESULT_CLEAR_SECONDS` (45 s default) on a result without
   touching the device. **Expected:** participant details and decision
   buttons disappear; the result heading stays.

## 4. Identity and reference lookup

1. Give the synthetic person a synthetic verified NIN (18 digits) via
   `apps.people.services.create_identity_identifier` in the shell.
2. Look up the NIN. **Expected:** the same result screen, identity hint
   `***` + last 4 digits; the full value never re-appears.
3. Look up the registration reference, then the pass fallback reference
   (`ASC-XXXX-XXXX-C`). **Expected:** both resolve.
4. As the operator, the manual search form is absent; posting to
   `/entry/lookup/manual/` is refused (403). As the supervisor it returns a
   short candidate list.

## 5. Supervisor override

1. With no `EntryOverrideReason` rows, produce an overrideable denial (e.g.
   set the Access Profile `valid_until` in the past). **Expected:** no
   override section at all.
2. Create a synthetic catalogue entry covering `OUTSIDE_TIME_WINDOW`; verify
   again as the supervisor. **Expected:** "Supervisor override" section;
   admitting records an Entry Event linked to the override.
3. Repeat with a revoked pass and a catalogue entry listing `PASS_REVOKED`.
   **Expected:** no override offered (fixed exclusion).

## 6. Restrictions and the external-security view

1. As a Security Restriction Manager (shell:
   `apps.entry.services.restrictions.create_restriction`), add a person-level
   DENY restriction. **Expected:** every context of that person is denied.
2. Scope an `EXTERNAL_SECURITY` account (with expiry) into `Entry Operators`
   for gate GA and verify the same pass. **Expected:** neutral "call the
   supervisor" wording; no Badge Type, no identity hint, no restriction
   category.
3. Set that account's `active_until` in the past and run
   `uv run --env-file .env python manage.py expire_entry_access`.
   **Expected:** "expired accounts: 1"; the account can no longer sign in.

## 7. Arabic / French

Switch the language selector on the verify and result screens.
**Expected:** Arabic renders right-to-left with translated headings
("التحقق من الدخول", "تم التحقق"); French shows "Vérifier l’entrée".

## 8. Device lifecycle

Suspend, resume, and revoke the device from its detail page. **Expected:**
suspension and revocation immediately end the device's checkpoint session
(the device's next request returns to set-up / "not enrolled"); a stale
form (another tab) returns "This device changed since you loaded it".
