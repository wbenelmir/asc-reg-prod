# Ministry NIN lookup: contract, client policy and matching rules

Date: 2026-10-01. Language: English. Code: `apps/people/identity_contract.py` (pure functions),
`apps/people/nin_provider.py` (adapters). Decisions: amendment A-13 (A13-03, A13-04, A13-08 to
A13-13). Tests: `apps/people/tests/test_idv_contract.py`, `test_idv_ministry_adapter.py`
(synthetic fake transport, **not provider evidence**).

## 1. What is observed and what is not

| Topic | Status | Source |
| --- | --- | --- |
| `POST https://miclat.mkesm.gov.dz/api/auth/` with JSON `username` and `password` | Observed | Owner screenshots (addendum §3) |
| `GET https://miclat.mkesm.gov.dz/api/get/{nin}` with Bearer authorization | Observed | Owner screenshots |
| Lookup body: JSON object with `identite`; relevant fields `nin`, `nom_f`, `pren_f`, `d_nais`, `presume` | Observed | Owner example |
| `d_nais` in DD/MM/YYYY; `presume` as the string `"False"` | Observed (one example) | Owner example |
| HTTP 200 with `identite: null` means NOT_FOUND | Observed + owner confirmation | Owner |
| The malformed sample (trailing comma) was a copy artifact | Owner confirmation | A13-10 |
| `presume` true means the day and/or month is unknown | Owner definition | A13-08 |
| No service usage limits | Owner report | A13-11 |
| **Authentication response** (token field, lifetime, renewal, errors) | **NOT SUPPLIED** (API-01) | — |
| Other date formats; network access; availability; error contract | **Unknown** (API-02, API-04) | — |

Nothing in this project has called the real service. The real backend stays unavailable until
its settings are present and valid.

## 2. Client (adapter) behaviour

* Only the NIN is transmitted, and only in the lookup path, after checking it is exactly 18 ASCII
  digits (leading zeros kept; no checksum invented).
* HTTPS only, to the configured origin (`MINISTRY_NIN_API_BASE_URL`, an `https://` origin without
  credentials, path, query or fragment). The platform trust store, or `MINISTRY_NIN_API_CA_BUNDLE`;
  certificate and host-name verification; TLS 1.2 or newer. Redirects are never followed (a 3xx is
  INVALID_RESPONSE), so credentials and the bearer token never reach another host.
* Bounded: connect timeout (default 5 s), read timeout (10 s), a total deadline (20 s) and a
  maximum body (64 KiB), checked against `Content-Length` and while reading.
  * IDV-C1 (R-IDV-03): the total deadline is ABSOLUTE for one request. It covers name resolution,
    TCP connect, the TLS handshake, sending, the status line and headers, and the body.
  * Every socket call is given only the time left (and at most the connect or read timeout), so
    the repeated reads that `http.client` makes for one buffered read cannot add up past it.
  * A call after the deadline fails at once, and a response that completes after the deadline is
    refused (`timeout`, so UNAVAILABLE and retryable). The connection is always closed and the
    provider slot released.
  * Name resolution cannot be interrupted by Python. It runs on a daemon thread that is abandoned
    at the deadline (it ends when the system resolver gives up); the request itself still ends on
    time.
  * The settings validation also requires the total to be at least the connect and the read
    timeout.
  * Evidence: real TLS against a synthetic local peer in
    `apps/people/tests/test_idv_c1_transport_deadline.py`. This is not evidence about the ministry
    service.
* Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity, no trailing data. Malformed bodies are
  never repaired.
* Concurrency: at most `IDENTITY_PROVIDER_MAX_CONCURRENCY` (default 4) calls at a time across every
  worker process (PostgreSQL session advisory locks); a busy slot defers the job by 15 s without
  counting an attempt.
* Nothing is logged by the adapter: no URL, header, body or exception text. Outcomes carry fixed
  machine codes only. `apps.core.redaction` also masks any `/api/get/<value>` path segment, any run
  of exactly 18 digits, `Authorization: Bearer` values and the configured ministry credentials, in
  every log line and formatted exception.

### Authentication (configurable, validated; API-01 open)

| Setting | Meaning | Default |
| --- | --- | --- |
| `MINISTRY_NIN_API_USERNAME`, `MINISTRY_NIN_API_PASSWORD` | Secret environment values | none |
| `MINISTRY_NIN_API_AUTH_TOKEN_PATH` | Dotted path to the token in the authentication JSON, e.g. `token` or `data.access` | none (required) |
| `MINISTRY_NIN_API_AUTH_EXPIRY_PATH` + `..._EXPIRY_FORMAT` | Optional: dotted path to an integer expiry, `relative_seconds` or `unix_epoch_seconds` | unset |
| `MINISTRY_NIN_API_TOKEN_CACHE_SECONDS` | Local client policy: the longest a token is reused (and the cap on a declared expiry) | 300 |

* The token must be a string of RFC 6750 token68 characters, 16 to 4096 long; otherwise
  `auth_contract_mismatch` (AUTH_ERROR, manual review). These names are configuration, not
  claimed provider facts.
* With an expiry configured, the token is reused for `min(declared - 30 s, TOKEN_CACHE_SECONDS)`;
  a configured expiry that is missing or not an integer is `auth_expiry_mismatch`.
* Without an expiry, `TOKEN_CACHE_SECONDS` applies — a conservative local policy, not a provider
  expiry claim.
* One token per process, single-flight (one thread authenticates, the others wait and reuse it).
  Authentications are also bounded by the concurrency slots.
* A lookup answered **401** (a confirmed rejection) invalidates that exact token, re-authenticates
  once and retries the lookup once; a second 401 ends the call (`rejected_after_reauthentication`,
  retried later by the job's backoff). At most two authentications and two lookups per call; no
  renewal loop.
* Authentication 401/403: credentials rejected (no immediate retry). 429/5xx/transport errors:
  transient.

### Status mapping of the lookup

| HTTP / transport | Outcome | Retried by the job | Manual-review reason |
| --- | --- | --- | --- |
| 200, `identite` object, valid shape | FOUND, then compared | — | depends on the comparison |
| 200, `identite: null` (any `result` text ignored) | NOT_FOUND | no | NOT_FOUND |
| 200, missing `identite`, wrong type, invalid JSON, missing or wrong-typed facts | INVALID_RESPONSE | no | INVALID_RESPONSE |
| 401 after one renewal | AUTH_ERROR | yes, bounded | PROVIDER_AUTH_ERROR (when exhausted) |
| 403 | AUTH_ERROR | no | PROVIDER_AUTH_ERROR |
| 429, 500-599, timeout, connection, TLS | UNAVAILABLE | yes, bounded | PROVIDER_UNAVAILABLE (when exhausted) |
| 3xx, 404, any other status, oversized body | INVALID_RESPONSE | no | INVALID_RESPONSE |
| Backend disabled or settings missing | NOT_CONFIGURED | no | PROVIDER_NOT_CONFIGURED |

A technical failure is never NOT_FOUND and never a rejection.

## 3. Matching (local, conservative)

1. The returned `nin` must equal the requested NIN exactly, as a string. Otherwise
   PROVIDER_IDENTITY_MISMATCH, and **nothing** from that response is retained (it may be another
   person).
2. `nom_f` is compared with the family name and `pren_f` with the given names: equal → `match`;
   equal after NFC, whitespace collapsing and Unicode case folding → `normalized_match`; anything
   else → `mismatch` (accents, transliteration, spelling, token order, swapped or missing names).
   There is no fuzzy threshold, and the participant's submitted names are never rewritten.
3. `presume`: a real boolean, or `"True"`/`"False"` after trimming and case folding. Absent, null
   or anything else → PRESUME_FLAG_UNRECOGNIZED (manual review).
4. `presume` true (owner rule A13-08): the official date is ignored entirely — not validated, not
   compared, not retained. Comparison `skipped_presumed`, policy basis
   `OWNER_RULE_PRESUMED_BIRTH_DATE_2026_10_01`. The participant's date is kept and is never shown as
   ministry-confirmed. NIN and names decide.
5. `presume` false: `d_nais` is required and must be a string in DD/MM/YYYY (two-digit day and
   month, four-digit year, ASCII digits). Another format → `unrecognized_format`; an impossible
   date → `impossible_date`; both are AMBIGUOUS_DATE. A parsed date different from the entered one
   → DATA_MISMATCH. The order is never guessed, 1 January is never invented, and the entered date
   is never changed.
6. Verification needs all of the above **and** the final duplicate recheck.

## 4. Retained official facts (A13-03)

Stored on the attempt, encrypted at rest: the official Latin family and given names (only when the
returned NIN matched), the normalized official date only when it was compared (`presume` false and
parsed), the raw official date text only when it could not be parsed (for the reviewer), the
parsed `presume` flag, the per-field comparison and the policy basis. Never stored: the response
body, Arabic names, parental names, civil-status data, any other field, headers or tokens.
Retention follows the identity data and the unresolved legal retention policy (OD-007).
