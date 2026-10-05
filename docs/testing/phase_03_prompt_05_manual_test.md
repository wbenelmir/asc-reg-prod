# Phase 3 Prompt 5 — manual test script

Status: **NOT EXECUTED BY A HUMAN.** The screenshots under
`var/test_artifacts/phase3/prompt5-ui/` were produced automatically by the
Playwright suites; they are evidence of rendering, not of manual testing or
approval. Use synthetic data only.

## Setup

```bash
uv run --env-file .env python manage.py migrate
```

```bash
uv run --env-file .env python manage.py runserver
```

Prepare, through the service layer or the local admin, as in the Prompt 4
manual test (`phase_03_prompt_04_manual_test.md`): one event edition with a
venue, gate and zones, an access profile allowed at the zone, one approved
registration with an active pass, an enrolled device, an Entry Operator and
an Entry Supervisor scoped to the gate, and an Entry Device Administrator
scoped to the event.

## A. Entry & Security screen (tablet, then phone)

| # | Step | Expected |
| --- | --- | --- |
| A1 | Open `/entry/` on the enrolled device, choose a zone, start | Navy app bar with the official logo (undistorted), "Online" indicator, gate/zone/device and device-scope chips, "Verify entry" navigation |
| A2 | Without clicking the field, type or scan a pass code | Focus jumps to the QR field; no character is lost; status reads "Ready for the scanner" |
| A3 | Submit a valid pass | Green "Verified" result with a check icon, "Admit" focused, participant card; a screen reader announces the heading politely |
| A4 | Press Esc | Result clears, back to the scanner |
| A5 | Scan an invalid code | Red "Do not admit" with an X icon and "What to do next"; announced assertively; no participant data |
| A6 | Leave a result untouched for `ENTRY_RESULT_CLEAR_SECONDS` | Participant card and decision buttons disappear; the heading stays |
| A7 | Identity tab: choose Passport, enter a number, no country, submit | Focused error summary linked to the country field; the number field is empty (never echoed) |
| A8 | Scan invalid codes repeatedly (defaults: signal at 5, pause at 15 in 5 min) | Supervisor monitor shows "Repeated invalid scans"; then "Lookups paused" with a 429 response; a valid pass scanned by another operator still verifies |
| A9 | Disconnect the network | Indicator "Connection lost", red banner, lookup buttons disabled; reconnect restores "Online" |
| A10 | End the operator session from another browser (or wait for inactivity) with the page open | Within the poll interval the indicator reads "Session ended" and the page offers set-up; leaving the page open does **not** keep the session alive |
| A11 | Repeat A1–A7 in French and Arabic | Full translation; Arabic is right-to-left with the Thmanyah font; references, gate codes and tokens stay left-to-right; parentheses in labels render correctly |
| A12 | Keyboard only | Every control reachable with Tab; visible focus ring on navy and white surfaces |
| A13 | 200% browser zoom and a 390 px wide window | No horizontal page scroll; tables scroll inside their card |

## B. Participant pass

| # | Step | Expected |
| --- | --- | --- |
| B1 | Sign in as the participant, open "My entry passes" | Card with status chip, display name, registration reference, fallback reference, validity, identity-check notice, QR, print button |
| B2 | Arabic | QR not mirrored; caption text right-to-left and readable |
| B3 | Participant without a pass | Empty state with a link to "My registrations" |

## C. Badge operations and devices

| # | Step | Expected |
| --- | --- | --- |
| C1 | Stock dashboard | KPI tiles (on hand, allocated, available, discrepancies), balances with inline actions, allocations, batches, ledger; no participant data |
| C2 | Badge issuance page | Registration context card; issue form only with an assigned badge type; change actions after issuance |
| C3 | Device list → device detail | Status chips, scope chips, actions only for a manager |
| C4 | Entry observability (Device Administrator) | p95 against the 1.5 s target, results, pass failures, gates, device health, outbox queue, stock exceptions, anomaly signals; window selector works |
| C5 | Open the observability URL as an Entry Operator | Refused |

## D. Performance (optional, local)

```bash
uv run --env-file .env python -m pytest tests/performance -q
```

Read `var/test_artifacts/phase3/prompt5-perf/entry_verification_load.json`.
Volumes can be raised with `ASC_PERF_PARTICIPANTS`, `ASC_PERF_FILLER`,
`ASC_PERF_DEVICES`, `ASC_PERF_SCANS_PER_DEVICE`.
