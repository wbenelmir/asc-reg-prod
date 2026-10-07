# Ministry NIN service diagnostics

An operations page for checking the Ministry of Interior NIN service without
submitting a registration: **`/ops/integrations/ministry-nin/`** (navigation:
"Ministry NIN service"). Code: `apps/people/nin_diagnostics.py` (rules),
`apps/people/diagnostics_views.py`, `templates/people/ministry_nin_diagnostics.html`.
It works in every environment where the official adapter is configured,
including production; it is not a debug tool.

## 1. Who may use it

The permission `people.run_ministry_nin_diagnostics`, granted through the role
**Integration diagnostics operator** (group "Integration Diagnostics
Operators"). The role is platform-wide only: the staff accounts area grants it
only with "Every event edition" and every organization, which only an account
administrator without limits can delegate. The page and each check also refuse
any membership narrowed to an event, an organization or a checkpoint. A
superuser may use it. Participants, reviewers, intake and gate staff and
event-scoped account administrators are refused (403; anonymous visitors go to
staff sign-in). Staff sign in as usual (email, password, security image).

Grant it to the few technical operators who own the integration, for as long
as they need it, with an end of validity when possible.

## 2. The three checks

| Check | What it does | What it proves |
| --- | --- | --- |
| **1. Configuration** | Shown when the page opens. Reads this server's settings; sends nothing. Lists the NAMES of missing or invalid settings (never a value, host or path), and whether this web process holds a cached token. | Only that the settings are present and well formed. A cached token is not proof that authentication works now; each web and worker process keeps its own. |
| **2. Authentication** ("Test authentication") | One new authentication request with the configured credentials, never the cached token. The token received is discarded (not shown, not cached). Shows the outcome, HTTP status, duration, time and the token lifetime in seconds. | That the authentication endpoint accepted the credentials just now. Not that lookups work. |
| **3. Lookup** ("Test lookup") | One lookup of an identity number you are authorized to use for testing, through the same adapter as registrations (validation, transport, token, parsing). Creates no registration, changes no identity, verifies nobody, sends no notification. The number is not stored, shown again or logged; identity data in the answer is discarded. | That the lookup endpoint answers for that number. |

Nothing runs when the page is opened or reloaded; each network check is a
separate button (POST with CSRF). Results are shown once, in that response,
and never kept in the session or the URL.

## 3. Result meanings and next steps

| Result | Meaning | Next step |
| --- | --- | --- |
| Not the official service | `NIN_PROVIDER_BACKEND` is not the official adapter. Nothing sent. | Deployment team configures the official adapter. |
| Configuration incomplete | Required settings missing or invalid. Nothing sent. | Complete the settings named under Configuration. |
| Authentication succeeded | Fresh authentication accepted. | Test a lookup. |
| Authentication failed | Credentials refused (401/403, `credentials_rejected`), an unexpected status (for example HTTP 400, `auth_unexpected_status`), or an answer that does not match the configured token or expiry mapping (`auth_contract_mismatch`, `auth_expiry_mismatch`). | Deployment team checks the credentials and the authentication request/response contract with the provider. Do not retry in a loop. |
| Lookup answered: identity found | The lookup endpoint returned an identity. Nothing recorded. | None. |
| Lookup answered: no identity found | HTTP 200 with `identite: null`: the service works and holds no identity for that number. Not an authentication failure and not a decision about anyone. | Use a number known to exist if a positive check is needed. |
| Service unavailable or too slow | Timeout, connection or TLS failure, or a provider server error (5xx). | Retry later; if persistent, check network routing, TLS and the provider's status. |
| Redirect refused | The service answered with a redirect (for example HTTP 301, `redirect_refused`). Redirects are never followed, so credentials and numbers never go to another address. | Deployment team confirms the canonical address and corrects `MINISTRY_NIN_API_BASE_URL`, `MINISTRY_NIN_API_AUTH_PATH` or `MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE` (including any trailing slash), or the proxy routing. Never enable redirect following or disable TLS checks. |
| Unreadable or unexpected answer | Malformed JSON, an oversized answer or an unexpected status; discarded. | Check the response contract with the provider. |
| The provider asked to slow down | HTTP 429 from the provider. | Wait several minutes. |
| Too many checks | The platform's own limit: `NIN_DIAGNOSTICS_MAX_PER_OPERATOR` (default 5) per operator and `NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT` (default 20) for the environment in each `NIN_DIAGNOSTICS_WINDOW_SECONDS` (default 600) window, counted on the shared counter (Redis in staging and production). Nothing sent. | Wait for the window to pass. |
| Another check is running | Only one check runs at a time across every process. Nothing sent. | Try again in a moment. |
| Check the test number | Not 18 digits. Nothing sent. | Enter the 18 digits. |

## 4. Records and retention

Each authentication or lookup check (including a throttled or busy one) writes
one audit event `IDV_NIN_DIAGNOSTIC_RUN`: operator, environment, provider
code, action, outcome, detail code, HTTP status, duration and whether the
provider was reached. Never the number, a credential, a token, a request or
response body, or identity data. Nothing is logged. Configuration views and
refused input are not recorded. The "Recent checks" list shows the last ten
events of this kind to authorized operators only; it is history, never the
current state of the service. These events follow the audit trail's retention
(append-only audit table; no separate store).

## 5. Live verification after a release (deployment team)

The automated tests use a scripted transport only; no request to the Ministry
was made while building this page. After the release and configuration, an
authorized operator holding the role, with an authorized test number supplied
by the Ministry:

1. Open the page; check that Configuration shows "Settings complete" and the
   official adapter.
2. Press "Test authentication": expect "Authentication succeeded". On HTTP 400
   (`auth_unexpected_status`) or 401/403, stop and check the authentication
   contract and credentials with the provider.
3. Enter the authorized test number and press "Test lookup": expect "identity
   found" (or "no identity found" for a number without an identity). On
   "Redirect refused" (HTTP 301), correct the configured address or path; do not
   follow the redirect.
4. Record the outcome codes, HTTP statuses and durations (never the number or
   any identity data) in the release report.
