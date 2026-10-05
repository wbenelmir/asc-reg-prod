# Beta backlog

What is still open for the staging Beta, sorted by who must act. Work deferred
here resumes after Beta feedback.

## A. Issues that prevent safe deployment or meaningful testing

None known in the repository. If a deployment step or a live check below fails
because of the application, record it here with the check identifier.

## B. External configuration and live checks owned by operators (and the owner)

| Item | Owner | Notes |
| --- | --- | --- |
| Infrastructure values: PostgreSQL roles, Redis (TLS, two databases), S3 bucket, clamd, SMTP and sender domain (SPF, DKIM, DMARC), TLS certificate, reverse proxy | Operators | [Deployment guide](README.md) §4–§8; [configuration template](../../deploy/staging.env.example) |
| Build and record the runtime artifact from the tagged revision | Operators | Deployment guide §6 |
| `audit-isolation` passes with the runtime role after the first migration | Operators | Deployment guide §5 and §7; not provable locally |
| Live checks INT-01 to INT-14 | Operators, UAT coordinator | [UAT handoff](../testing/phase_04_staging_uat_handoff.md) §3 |
| Live checks RC-01 to RC-14 of the registration corrections (2026-10-04) | Operators, UAT coordinator | [Registration corrections checklist](../testing/registration_corrections_staging_checklist.md) §3; all NOT_EXECUTED |
| The unconfirmed legal facts the official v3 notices still mark: the controller's postal address, the data-protection contact, the ANPDP declaration or authorization reference, the legal basis of each purpose, the other recipients, transfers outside Algeria, the retention periods, the official Arabic name of the ministry, and (Terms) the organizer contact and the applicable law and jurisdiction | Owner, legal function | C-09; publish them as a new version through a reviewed migration (the `privacy.0005` pattern); they block production, not the staging Beta |
| Scanner acceptance with the standard antivirus test file | Operators | `check_malware_scanner --expect infected` |
| Team-controlled test mailboxes for the UAT accounts and participants | Owner, operators | Provisioning `--email-pattern` |
| Backup and a restore test before the first migration | Operators | Deployment guide §7 |
| Real ministry NIN check: written authorization (UAT-02), the authentication response shape (API-01), date formats (API-02), network path (STG-NET-01) | Owner, external | [Identity provider guide](../operations/identity_provider_integration_guide.md); disabled until then |
| Policy for a message left in `SENDING` with an unknown outcome (COMM-01) | Owner | No automatic re-send exists, including after an unconfirmed SMTP attempt (`UNCONFIRMED:<error>`); escalation procedure in the UAT handoff INT-09 |

## C. Non-blocking improvements deferred until after Beta feedback

| Item | Reference |
| --- | --- |
| French and Arabic translations of the staff identity-review and NIN-exemption screens (about 230 strings, still English in FR/AR). The participant-facing identity step, correction page and workspace strings were translated on 2026-10-04 | IDV-Q5 |
| Retention periods with a purge job | OD-007 (block production, not Beta) |
| The assistance path for a stateless participant or one whose country is not listed (the approved catalog `countries-v1` is installed) | C-01 |
| Whether a withdrawn invitation submission should free its campaign place (today it still counts, so a full campaign needs a new invitation to register again) | Owner decision |
| Whether a registration cancelled by the registration team may be re-registered by the participant (today the participant is told to contact the team) | Owner decision |
| The delegation-claim email retry still records an ambiguous SMTP failure as failed; it is not scheduled. Do not schedule it, or add an operator re-send, before it follows the main path's uncertain-outcome rule | Independent C1 review, item 2 |
| A genuine step-up provider for the emergency device wipe | STEPUP-01 |
| Confirmed capacity figures for the challenge counter and the gates | CACHE-02, OD-001, INFRA-001, OD-008 |
| Open identity questions | IDV-Q4, IDV-Q6 to IDV-Q16 |
| A `release_readiness` item and a `/readyz` probe for the malware scanner | Scanner operations |
| SMTP delivery receipts (`DELIVERED` status) | Communications |
| A managed key service for the pass and offline-package signing keys | ADR-0019, ADR-0023 |
| Container image or CI pipeline, if the operations team wants one | Operations choice |
| Later-phase features (the next planned work package) | Project plan |
