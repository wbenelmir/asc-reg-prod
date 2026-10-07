# Staff accounts and scoped access

The operations area `/ops/staff-accounts/` creates staff accounts, sends
password setup links, changes account status and validity, and grants or ends
scoped roles. It does not depend on the Django admin. Code:
`apps/accounts/administration.py` (rules), `apps/accounts/administration_views.py`,
`apps/accounts/roles.py` (role catalogue).

## 1. Who may administer

The **Account Administrators** role (`accounts.manage_operational_accounts`).
An administrator's scope is the event and organization of that role's
membership; an empty event or organization means every event or every
organization. Every page and every command checks it on the server.

* A role can be granted or ended only inside the administrator's scope.
* An account's status, validity and credentials can be changed only when the
  administrator's scope covers every current role of that account.
* Nobody changes their own account, status or roles.
* A superuser account is administered only by a superuser; the area never sets
  staff or superuser flags.
* The last account administrator with full scope can never be suspended,
  disabled, expired or lose that role. Commands that could change it are
  serialized, so two administrators removing each other at the same time
  leave one administrator.

## 2. Account lifecycle

Three separate steps, each audited:

1. **Create** -- status Invited, no password, no role.
2. **Credentials** -- "Send a setup link" (or "Send a password reset link"):
   a single-use link, valid `OPERATIONAL_CREDENTIAL_SETUP_TTL_SECONDS`
   (default 48 hours), emailed to the account's own address. Only a digest is
   stored; a newer link replaces an older one; the link appears in no page,
   audit event or message table. Choosing the password neither signs in nor
   activates the account. A password change ends the account's other sessions.
3. **Activate** and **grant roles** -- the account opens an area only when it
   is active, inside its validity window, has a password and has a current
   role.

The account page shows the status separately from the effective access ("can
sign in", "no password yet", "active but no current role", "cannot sign in").

**Suspend** and **Disable** take effect on the account's next request (its
sessions end) and end its entry checkpoint sessions now. Ending a role takes
effect on the next request. Nothing is deleted: roles and statuses stay in
the history.

## 3. Roles

A fixed catalogue of the existing groups (each with its own fixed
permissions). Each role states which narrowing it accepts:

* every role needs an explicit event edition, or "Every event edition" chosen
  on purpose with a confirmation box;
* organization-scoped roles may be limited to one organization ("Every
  organization" is an explicit choice);
* the entry checkpoint roles may be limited to one checkpoint;
* edition-wide roles (attendance policy, badge stock administration, device
  administration, ...) cannot be limited to an organization;
* the **Integration diagnostics operator** role (the
  [Ministry NIN diagnostics page](ministry_nin_diagnostics.md)) is
  platform-wide only: it is granted only for every event edition and every
  organization, so only an administrator without limits can grant it.

The External Security (Temporary) group is not granted here. A temporary
external-security account needs an end of validity, and any role granted to it
ends no later than the account.

## 4. The first administrator

On the server, once, with the runtime settings:

```bash
python manage.py bootstrap_account_administrator --email <address> --display-name "<name>"
```

* Refused as soon as an account administrator with full scope exists.
* Creates the account active, without a password, with the Account
  Administrators role for every event and organization, and emails a
  single-use setup link (`--deliver terminal` prints it once instead, for an
  operator who cannot receive the email; `--existing` promotes an existing
  account and sends a reset link).
* **If the email cannot be sent** the command ends with an error naming the
  failure; the account is set up but its link was revoked (and the failure
  audited as `ACC_STAFF_CREDENTIAL_SETUP_DELIVERY_FAILED`). Recover with:

  ```bash
  python manage.py bootstrap_account_administrator --email <same address> --resend
  python manage.py bootstrap_account_administrator --email <same address> --resend --deliver terminal
  ```

  `--resend` issues a new single-use link (the previous one stops working)
  for that same pending account only. It is refused for any other address,
  once that account has set its password, when the account is no longer an
  active administrator with full scope, or when another administrator with a
  password exists (that administrator sends links from the operations area).
  It never creates or promotes an account. Audited as
  `ACC_STAFF_ACCOUNT_ADMINISTRATOR_SETUP_RESENT`.
* In the operations area, a link whose email cannot be sent is cancelled and
  the administrator sees "The email with the link could not be sent"; sending
  another link later is the recovery.

## 5. Sign-in

Staff sign in at `/accounts/ops/sign-in/` with their email address, their
password and a security image ([staff sign-in CAPTCHA](../security/staff_sign_in_captcha.md)).
There is no sign-in code and no mandatory sign-in MFA. The existing per-email
and per-network sign-in limits, CSRF, session lifetimes and the MFA step-up of
sensitive operations are unchanged.

## 6. Setup links and logs

A setup link carries its single-use secret in the path of its FIRST request
(`/accounts/setup/<secret>/`). Nothing that stores a log may keep that
segment.

**In the application** (verified by `apps/accounts/tests/test_setup_link_redaction.py`):
the secret-bearing request renders no page: it keeps only the link's id in
the session (or clears it for an unknown, used or expired link) and redirects
at once to `/accounts/setup/` (`Cache-Control: no-store`,
`Referrer-Policy: no-referrer`); the password form and the "link not valid"
page live at that clean address. Every application log line masks the
WHOLE segment after `setup/` (`apps/core/redaction.py`): messages, `extra=`
fields (for example a referer), exception text and Django's request log —
also when separators or the secret's own characters are percent-encoded
(first, interior or every character; once or twice; upper or lower case;
mixed). The application writes no access log (waitress) and logs no request
header. More than the secret may be masked; never less.

**In front of the application** — the reverse proxy, a load balancer, a CDN or
a web application firewall — the deployment team must make sure, BEFORE the
first real setup link is sent, that no access log, error log, debug log or
request/header capture stores the segment. Masking the access log alone is not
enough: proxies write the raw request line into their error log when the
application fails, and that log cannot be reformatted. Required for any proxy:

1. **A dedicated route for setup paths.** Requests whose decoded path starts
   with `/accounts/setup/` followed by at least one character (also
   `/accounts%2Fsetup%2F…`) are handled by their own route that
   (a) writes an access-log line with a FIXED path text instead of the URI,
   and (b) sends no message about these requests to any stored error log.
2. **Masked request URI and Referer everywhere else** (a setup path can
   appear in a query parameter or a Referer): mask the segment, or drop the
   Referer when it contains a setup path. Never log the raw request line or
   arbitrary request headers.
3. **No debug or request-capture logging** in production (for example nginx
   `debug` level, WAF full-request capture, CDN request logging with full
   URLs), or the same masking applied there.

Example for nginx (adapt the upstream, paths and formats; `http {}` context
for `map`/`log_format`, `server {}` for the locations):

```nginx
# Access-log values with the setup segment masked (requests to other routes).
map $request_uri $asc_log_uri {
    "~*^(?<asc_head>.*?accounts(?:/|%2F|%252F)setup(?:/|%2F|%252F))(?:(?!%2F|%252F)[^/?#&])+(?<asc_tail>.*)$" "${asc_head}***REDACTED***${asc_tail}";
    default $request_uri;
}
map $http_referer $asc_log_referer {
    "~*accounts(?:/|%2F|%252F)setup(?:/|%2F|%252F)." "-";
    default $http_referer;
}
log_format asc_masked '$remote_addr - $remote_user [$time_local] '
                      '"$request_method $asc_log_uri $server_protocol" $status '
                      '$body_bytes_sent "$asc_log_referer" "$http_user_agent"';
log_format asc_setup_fixed '$remote_addr - $remote_user [$time_local] '
                           '"$request_method /accounts/setup/[link] $server_protocol" '
                           '$status $body_bytes_sent "-" "$http_user_agent"';

server {
    # ... TLS, server_name, the existing proxy settings ...
    access_log /var/log/nginx/asc_access.log asc_masked;
    error_log  /var/log/nginx/asc_error.log error;   # never "info" or "debug"

    # Setup links: matched on the decoded path; nothing about them in the error log.
    location ~* "^/accounts(?:/|%2F)setup(?:/|%2F)." {
        access_log /var/log/nginx/asc_access.log asc_setup_fixed;
        error_log  /dev/null crit;
        proxy_pass http://asc_app;          # the same upstream and proxy headers
        # include the same proxy_set_header lines as the main location
    }

    location / {
        proxy_pass http://asc_app;
        # ... existing settings ...
    }
}
```

Never use `$request`, `$request_uri` or `$http_referer` directly in any
`log_format`. The cost of the dedicated route: an upstream failure on a setup
link leaves only its status (502/504) in the fixed access line; diagnose with
the application's own logs, which are masked.

**Pre-issuance check (deployment team, on the real proxy, with synthetic
values only):**

```bash
SYN="prxchk$(date +%s)abcdefghijkl"          # synthetic, never a real link
ENC="%$(printf '%02X' "'${SYN:0:1}")${SYN:1}"  # first character percent-encoded
curl -s -o /dev/null "https://<host>/accounts/setup/${SYN}/"
curl -s -o /dev/null "https://<host>/accounts/setup/${ENC}/"
curl -s -o /dev/null "https://<host>/accounts%2Fsetup%2F${ENC}%2F"
curl -s -o /dev/null -H "Referer: https://<host>/accounts/setup/${SYN}/" "https://<host>/legal/"
# Upstream failure: repeat the first two requests while the application is
# stopped (or the upstream points to a closed port) during a maintenance window.
grep -c "${SYN:1:12}" /var/log/nginx/asc_access.log /var/log/nginx/asc_error.log   # must print 0 for each
```

Repeat the `grep` on every other log the proxy, load balancer, CDN or WAF
keeps. Record the result (counts only) in the release report. If any count is
not 0, do not send setup links; fix the configuration first. A link that did
reach a log stops working when it is used, replaced or expires (48 hours by
default); send a new one if in doubt.

What was verified in LOCAL: the application side (tests above); the nginx
expressions with synthetic paths in a Python equivalent
(`ASC2026_UPDATE_REVIEW/logs/a02/a02_01_nginx_expressions_check.log` in the
review package). nginx itself, a load balancer, a CDN or a WAF were not
available and were not run.

## 7. What a scoped administrator can read

An administrator limited to an event or an organization lists, searches,
counts and opens only:

* accounts that have (or had) a role inside their scope;
* their own account;
* an account they created themselves, as long as it has no current role
  (so they can give it its first role).

Other accounts, including new accounts created by someone else, answer "not
found" -- the same answer as an unknown address. On a shared account they see
only the roles inside their scope, and a note that other roles exist; the
setup-link state is shown only to an administrator who may send links to that
account. An administrator without an event or organization limit reads every
account.

## 8. Audit codes

`ACC_STAFF_ACCOUNT_CREATED`, `ACC_STAFF_ACCOUNT_UPDATED`,
`ACC_STAFF_ACCOUNT_STATUS_CHANGED`, `ACC_STAFF_ACCOUNT_ROLE_GRANTED`,
`ACC_STAFF_ACCOUNT_ROLE_REVOKED`, `ACC_STAFF_ACCOUNT_ACTION_REFUSED`,
`ACC_STAFF_CREDENTIAL_SETUP_ISSUED`, `ACC_STAFF_CREDENTIAL_SETUP_COMPLETED`,
`ACC_STAFF_ACCOUNT_ADMINISTRATOR_BOOTSTRAPPED`, `ACC_STAFF_ACCOUNT_ADMINISTRATOR_SETUP_RESENT`,
`ACC_STAFF_CREDENTIAL_SETUP_DELIVERY_FAILED` (error class only). They carry ids, statuses, role
keys, scope ids and dates, never an email address, a password, a token or a link.
