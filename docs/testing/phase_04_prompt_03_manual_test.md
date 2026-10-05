# Phase 4 Prompt 3: local verification record

Date: 2026-09-28. Status: NOT EXECUTED unless explicitly recorded below.
All scenarios use synthetic participants, devices and keys. This document is a
procedure, not evidence that its scenarios passed.

## Recorded environment

Continuation host: Linux x86_64, Python 3.14.7, frozen project dependencies.
The user's Windows PostgreSQL configuration was not supplied or accessed.
No PostgreSQL server is present here. A targeted database test failed during
setup with connection refused at localhost:5433. Chromium 153.0.8010.12 was
downloaded, but launch failed because the host denies its socket operations.
No browser test body or device benchmark ran on this host.

## Windows preparation and commands

Keep your existing local `.env` in place. Never copy it into the review archive.
The supplied `.env.example` is a placeholder-only recovery template, not a
replacement for local configuration. No Git or Docker command is required.

Before browser tests, copy existing synthetic review screenshots to a separate
evidence directory. The important existing paths are
`var/test_artifacts/phase3/` and
`var/test_artifacts/phase4/prompt2-offline/`. Preserve their checksums and do not
overwrite an approved archive. Copy only review evidence, not runtime/private data.

Run from the project root in PowerShell:

```powershell
Remove-Item Env:DJANGO_ALLOW_ASYNC_UNSAFE -ErrorAction SilentlyContinue
uv sync --frozen
uv run --env-file .env python manage.py check
uv run --env-file .env python manage.py showmigrations entry
uv run --env-file .env python manage.py sqlmigrate entry 0005
uv run --env-file .env python manage.py makemigrations --check --dry-run
uv run --env-file .env pytest -q apps/entry/tests/test_offline_sync.py apps/entry/tests/test_offline_conflicts.py apps/entry/tests/test_offline_sync_api.py apps/entry/tests/test_offline_reconciliation.py
uv run --env-file .env pytest -q apps/entry/tests/test_offline_sync_concurrency.py tests/concurrency/test_offline_concurrency.py
uv run --env-file .env pytest -q apps/entry/tests/test_offline_migration.py apps/entry/tests/test_offline_sync_migration.py
uv run --env-file .env python -m playwright install chromium
uv run --env-file .env pytest -q tests/browser/test_offline_runtime.py --browser chromium
uv run --env-file .env pytest -q tests/performance/test_offline_verification.py --browser chromium
```

Migration round trips belong in the test database. Do not reverse `entry.0005`
on a database containing durable offline records. The guard must refuse this.
If 0005 is already applied locally, do not regenerate or rewrite it.

After targeted checks pass, run the required complete gate:

```powershell
Remove-Item Env:DJANGO_ALLOW_ASYNC_UNSAFE -ErrorAction SilentlyContinue
uv run --env-file .env python scripts/check.py all
```

The gate no longer enables `DJANGO_ALLOW_ASYNC_UNSAFE` in its browser subprocess.
If a legacy synchronous browser test encounters Django's async safety guard,
record the failure and correct that test's database/thread boundary; do not
restore the bypass. Full-suite compatibility has not been established here.

## Manual scenario checklist

| Scenario | Expected observation | Result |
| --- | --- | --- |
| Enroll and prepare | Two non-exportable P-256 keys; verified scoped encrypted package; authorized grant | NOT EXECUTED |
| Application unavailable while browser says online | Debounced Offline Active, no reliance on browser flag alone | NOT EXECUTED |
| Brief network flap | No immediate offline admission after a single failed probe | NOT EXECUTED |
| Recovery | Stable heartbeat, ordered upload, signed acknowledgements, critical refresh, then online | NOT EXECUTED |
| Valid signed QR | Correct minimal result; durable local record before success | NOT EXECUTED |
| Forged, malformed, unsupported or wrong-event QR | Explicit denial/referral without admission or identity search | NOT EXECUTED |
| Wrong checkpoint/grant or blocking delta | Fail closed; no override admission | NOT EXECUTED |
| Stale package | Manual review or refusal only | NOT EXECUTED |
| Expired package | Manual referral only, no resolved verification | NOT EXECUTED |
| Disk quota/write failure | No local admission success; evidence remains intact | NOT EXECUTED |
| Two tabs / double click | No stale second admission; serialized append and upload | NOT EXECUTED |
| Reload / installed-PWA restart | Pending encrypted evidence and keys survive | NOT EXECUTED |
| Interrupted/lost acknowledgement | Retry same operation; exactly one server Entry Event | NOT EXECUTED |
| Misbound/unsigned acknowledgement | Operation remains pending | NOT EXECUTED |
| Revocation/suspension during synchronization | Quarantine after current-state revalidation | NOT EXECUTED |
| Pass replaced/revoked and mutable-rule relaxation | Explicit reconciliation, no silent acceptance | NOT EXECUTED |
| Event/venue/gate authorization | Unauthorized queue/case/action denied; no foreign participant details | NOT EXECUTED |
| Supervisor note and closure | Version-checked audit; original evidence unchanged | NOT EXECUTED |
| Ordinary cleanup / sign-out / package refresh | Unacknowledged records retained | NOT EXECUTED |
| Emergency wipe blocked by another tab | No success report before IndexedDB deletion succeeds | NOT EXECUTED |
| EN/FR/AR; phone/tablet/desktop | Correct translations, RTL, bidi identifiers, logo and Thmanyah font | NOT EXECUTED |
| Keyboard / screen reader | Visible focus, accessible names and correct live announcements | NOT EXECUTED |
| Managed current/previous Chromium hardware | Repeat benchmark; record OS/CPU/browser and cold/warm median/p95/max | NOT EXECUTED |

The provisional benchmark uses 10,000 synthetic entries and production package
activation, one cold scan and 100 warm scans, without CPU throttling. It writes
`var/test_artifacts/phase4/prompt3-offline-performance.json` only after measurement.
Host execution is not a managed-device validation. The formal Prompt 5 benchmark,
capacity rehearsal, broker/provider integrations and deployment remain outside scope.

## User-local automated evidence and correction 1 (2026-09-28)

Screenshots supplied by the user show:

* Django system check: no issues; migration drift check: no changes detected.
* Migration plan: only entry.0005 pending on the development database.
* Migration test modules: 6 passed in 163.78 seconds.
* Sync/conflict/API/reconciliation run: 61 passed, 1 failed in 46.86 seconds;
  stopped at the stale-package test because of `--maxfail=1`.

`scripts/check.py db-state` is a legacy clean-pre-Prompt-3 preflight, not a general
health gate for an already migrated project. Its clean-state rejection is not a
reason to delete tables or reverse migrations. Use the migration plan and the
current acceptance gate for this verification.

After applying correction 1, run the focused checks first:

```powershell
uv run --env-file .env pytest -q tests/foundation/test_offline_conflict_timing.py apps/entry/tests/test_offline_conflicts.py -k "temporal_rejection_precedence or an_admission_on_stale_data or stale_data_with_an_expired_grant" --maxfail=1
```

This selects 17 cases: 14 classifier cases and 3 real PostgreSQL cases. Once they
pass, rerun the four-module sync/conflict/API/reconciliation command above, then
concurrency, browser and full acceptance in that order. Do not apply migration
0005 to the development database as a workaround for a test failure. No manual
scenario in this document has been executed or marked passed by this update.

## User-local correction 1 result and correction 2 follow-up (2026-09-28)

User-supplied screenshots confirm correction 1: **17 passed, 26 deselected in
22.27 seconds**. The four-module suite subsequently completed with **100 passed,
1 failed in 71.01 seconds**. Its sole failure was the permitted offline override
fixture, which created its reason after the original package snapshot.

After applying correction 2, run:

```powershell
uv run --env-file .env pytest -q apps/entry/tests/test_offline_conflicts.py -k "permitted_offline_override or override_catalogue_change_after_package" --tb=short
```

This selects 2 PostgreSQL tests: the corrected positive case and the new
outdated-catalogue conflict case. They have not passed on the correction host.
Once both pass, rerun the complete four-module group without maxfail:

```powershell
uv run --env-file .env pytest -q apps/entry/tests/test_offline_sync.py apps/entry/tests/test_offline_conflicts.py apps/entry/tests/test_offline_sync_api.py apps/entry/tests/test_offline_reconciliation.py --tb=short
```

The new regression increases that group from 101 to 102 collected tests. Continue
to concurrency and browser checks only after it passes. The six successful
migration tests need not be repeated solely for this test-only correction.
Development migration 0005 remains pending according to the latest supplied
migration plan. No manual scenario is marked passed by these automated results.

## Correction 3 local acceptance procedure (2026-09-28)

Actual user Windows evidence before correction 3:

| Check | Supplied result |
| --- | --- |
| Correction-2 positive/negative override cases | 2 passed in 35.24 seconds |
| Four synchronization modules | 102 passed in 57.63 seconds |
| PostgreSQL concurrency | 12 passed in 48.24 seconds |
| Offline Chromium runtime | 13 passed in 4.83 seconds |
| Provisional offline performance | 1 passed in 4.02 seconds (whole test runtime) |
| Complete gate | Exit 1; main 2532 passed / 4 failed in 560.11 seconds; browser 13 passed / 175 setup errors in 20.51 seconds |

The benchmark's cold/median/p95/maximum values were not supplied. Keep the actual
`prompt3-offline-performance.json` as the measurement source; 4.02 seconds is not
QR latency. Manual scenarios remain NOT EXECUTED.

After reviewing and overlaying correction 3, remove the unsafe override and run
the small thread/lifecycle/PostgreSQL regression group first (12 cases):

```powershell
Remove-Item Env:DJANGO_ALLOW_ASYNC_UNSAFE -ErrorAction SilentlyContinue
uv run --env-file .env pytest -q tests/foundation/test_browser_database_boundary.py tests/browser/test_database_boundary.py --browser chromium --tb=short
```

Then run the affected database assertions and deprecated-field source guard:

```powershell
uv run --env-file .env pytest -q apps/entry/tests/test_devices.py apps/entry/tests/test_offline_views.py apps/entry/tests/test_prompt5_observability.py apps/entry/tests/test_offline_sync.py apps/entry/tests/test_offline_source_compat.py -k "offline_capable_scope_requires_coherent or shell_html_is_identical or every_lookup_records_one_identifier_free_sample or an_offline_entry_event_must_name or no_code_reads_the_deprecated_gate" --tb=short
```

This selects five cases. If these pass, preserve previous generated evidence
before running the complete browser suite (192 collected cases):

```powershell
uv run --env-file .env pytest -q tests/browser --browser chromium --tb=short
```

Before that browser run and again before full acceptance, preserve existing PNG
and JSON evidence in the established timestamped backup location under
`var/test_artifacts/phase4/`. The writable evidence sources are:

* `var/test_artifacts/phase3/`
* `var/test_artifacts/phase4/prompt2-offline/`
* `var/test_artifacts/phase4/prompt3-offline-performance.json`
* `var/screenshots/phase3_prompt3/named/`
* `var/screenshots/phase3_prompt4/named/`

Copy only generated PNG/JSON test evidence, preserving relative paths. Never
copy environment configuration, runtime private storage, logs or participant data.
Do not package these backups. A previous failed run does not replace prior
approved screenshots with approved new evidence.

Run the complete gate only when the focused and browser checks pass:

```powershell
Remove-Item Env:DJANGO_ALLOW_ASYNC_UNSAFE -ErrorAction SilentlyContinue
uv run --env-file .env python scripts/check.py all
Write-Output "AcceptanceExitCode=$LASTEXITCODE"
```

Use the established `all` command, which separates main, performance and browser
suites into their intended processes. No runserver or manual development migration
is required for these tests. Do not enable the unsafe override to resolve a failure.
Keep the full result and final SUMMARY; do not infer success from progress dots.
No correction-3 PostgreSQL/Chromium page/full-gate pass is claimed yet.

## Correction 3 local automated results (2026-09-29)

These results were executed on the developer's Windows 10 host through
`uv run --env-file .env`, with the unsafe override removed:

| Check | Actual result |
| --- | --- |
| Boundary group (12 cases) | 12 passed in 32.45 seconds |
| Affected database/source cases | 5 passed, 198 deselected in 37.14 seconds |
| Complete browser suite, first run | 191 passed, 1 failed in 1059.69 seconds (lazy `credential.series` read; fixed test-only) |
| `tests/browser/test_digital_entry_pass.py` after the fix | 10 passed in 70.56 seconds |
| `scripts/check.py all` | AcceptanceExitCode=0; main 2550 passed, performance 2 passed, browser 192 passed |
| Offline verifier JSON (test host) | cold 3.40 ms; median 0.20 ms; p95 0.30 ms; maximum 0.40 ms; 10,000 entries |

Evidence was preserved before the browser run and before the gate, then restored
afterwards. See the historical review record `phase_04_prompt_03_correction_03_local_verification_report.md` (kept outside the repository).
These automated results do not execute any scenario in the manual checklist above.
Every manual scenario remains **NOT EXECUTED**.

## Correction 4 automated results (2026-09-29)

These ran on the developer's Windows 10 host with real PostgreSQL 17 and Chromium,
through `uv run --env-file .env`, with the unsafe override removed. They are
automated evidence only. They do not execute any scenario in the manual checklist.

| Check | Actual result |
| --- | --- |
| `apps/entry/tests/test_offline_conflicts.py` (R-01 regressions included) | 52 passed |
| `test_offline_sync.py` + `test_offline_sync_api.py` (R-02 regressions included) | 64 passed |
| The four sync/conflict/API/reconciliation modules | 129 passed |
| New live-server browser synchronization module (G-01, G-02) | 5 passed in the targeted runs, and again inside the gate |
| Affected offline browser modules and the database boundary group | 56 passed |
| `scripts/check.py all` | AcceptanceExitCode 0: main 2577, performance 2, browser 197 passed; every summary check OK |

Automated coverage now exists for two rows of the checklist below. Both rows
remain **NOT EXECUTED** as manual scenarios on real devices and real networks:

* "Interrupted/lost acknowledgement": a live server, with a response dropped
  after commit and a production retry;
* "EN/FR/AR; phone/tablet/desktop": 23 automated captures in
  `var/test_artifacts/phase4/prompt3-visual/`.

Additional manual checks recommended by correction 4 (NOT EXECUTED):

| Scenario | Expected result |
| --- | --- |
| Human review of the 23 correction 4 captures and `INDEX.json` | Branding, Thmanyah Arabic font, RTL, readability and wording accepted. Note UI-OBS-01 (a stale "not synchronized yet" message after acknowledgement) and UI-OBS-02 (state values shown as codes on the case page) |
| Offline override on a managed device, then deactivate or narrow its reason on the server before synchronization | Reconciliation case `ACCESS_CHANGED / HISTORICAL_ACCESS_UNCERTAIN`; never a clean admission and never a policy violation |
| An override using a reason that was inactive when the package was built (a deviating client; test environment only) | `SECURITY_CONFLICT / POLICY_VIOLATION / OVERRIDE_NOT_PERMITTED`, with no Entry Event |
| A burst of malformed or forged quarantine uploads naming a revoked device | Refused. At most one audit event per reason and window; the device's genuine quarantine evidence is still accepted |

Every manual scenario remains **NOT EXECUTED**.

## Correction 5 automated results (2026-09-29)

These ran on the developer's Windows 10 host with real PostgreSQL 17 and
Chromium, through `uv run --env-file .env`, with the unsafe override removed.
They are automated evidence only. They do not execute any scenario in the
manual checklist.

| Check | Actual result |
| --- | --- |
| Focused D2/D3 regressions (`test_offline_conflicts.py`, `test_offline_admission_evidence.py`, foundation timing) | 96 passed |
| New multi-connection module (`test_offline_evidence_concurrency.py`) | 9 passed |
| New regressions against the original correction 4 code | 36 failed / 3 passed (behavioural for S1 and R-02); the A2 behaviour probe failed 4 of 6 on the original code and passed 6 of 6 on the corrected code |
| `tests/concurrency` in its own process | 45 passed |
| Affected browser modules (offline sync live, runtime, PWA, online checkpoint and UI) | 80 passed and 3 transient failures (network change or host pause); all 8 cases of those tests passed on rerun |
| `scripts/check.py all` | AcceptanceExitCode 0: main 2616, performance 2, browser 197 passed; every summary check OK |

Additional manual checks recommended by correction 5 (NOT EXECUTED):

| Scenario | Expected result |
| --- | --- |
| On a managed device, override offline with a packaged reason; on the server, delete or rename that reason; synchronize; then verify the same pass online at another checkpoint under a single-entry Access Profile | Case `ACCESS_CHANGED / HISTORICAL_ACCESS_UNCERTAIN` without an Entry Event; the online result is `DENIED / ALREADY_ADMITTED`, and admission is refused |
| The same, under the default re-entry policy | The online result shows the "Previously admitted" and "Very recent previous admission" advisories |
| Prepare a second managed device after that synchronization, then scan the pass offline | Under single entry, the device refuses (`ALREADY_ADMITTED`) |
| A deviating test client overrides with a reason inserted inactive after the package was built (test environment only) | `SECURITY_CONFLICT / POLICY_VIOLATION / OVERRIDE_NOT_PERMITTED`, with no Entry Event |
| Parallel forged quarantine uploads naming one revoked device, sent from several clients and application workers | One refusal audit row per reason and window; no request waits; the device's genuine quarantine upload is still accepted |

Every manual scenario remains **NOT EXECUTED**.
