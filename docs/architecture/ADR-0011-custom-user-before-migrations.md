# ADR-0011: Custom-user-before-migrations sequencing

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | Schema §12.1, §20 step 1; accepted plan §6.2 |

## Context

Schema §12.1 requires a custom Django user model from the first migration.
If `django.contrib.auth`'s migrations were ever applied against a database
before `AUTH_USER_MODEL` is swapped to the project's own
`accounts.OperationalUser`, Django would create the default `auth.User`
table; swapping the user model afterward is not a clean, supported
operation and would leave the development database unusable for it.

## Decision

- **Prompt 2 creates no project model, no project migration, and runs no
  `manage.py migrate` at all** -- not even Django's own contrib migrations.
  `AUTH_USER_MODEL` is deliberately left unset in `config/settings/base.py`
  (it stays at Django's default, `auth.User`) precisely because
  `apps.accounts.OperationalUser` does not exist as a class yet; setting it
  to a nonexistent class would break every management command.
- **Prompt 3 opens with a read-only pre-flight gate** (see the accepted
  plan §6.2 and the Prompt 3 task list) that confirms no project migration
  and no unexpected contrib migration was applied during Prompt 2, before
  defining `OperationalUser` and generating `accounts.0001` -- which must
  be the first project migration in the whole graph.
- Every subsequent migration that references an actor declares
  `migrations.swappable_dependency(settings.AUTH_USER_MODEL)`, so Django's
  own migration-graph resolution enforces the ordering, not just code
  review.

## Consequences

- `tests/foundation/test_settings_smoke.py` pins
  `settings.AUTH_USER_MODEL == "auth.User"` as a Prompt 2 invariant --
  this assertion is expected to change to
  `"accounts.OperationalUser"` in Prompt 3, and that test updated
  accordingly at that point.
- If Prompt 2 experimentation ever accidentally ran `migrate`, Prompt 3's
  pre-flight gate detects it and **stops as a genuine blocker** rather than
  silently dropping/recreating the database -- no automatic destructive
  remediation is ever performed by this project.
