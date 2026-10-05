"""Release-readiness assessment (P4-4-C1, review finding R-03).

A passing local regression gate is not release readiness. This module states,
for a fixed set of release prerequisites, one of three results:

* ``READY``: assessed, and satisfied;
* ``BLOCKED``: assessed, and a known release dependency is unresolved;
* ``NOT_ASSESSED``: the assessment could not run (the database or schema is
  unavailable, a query failed). It is never treated as READY.

The overall result is READY only when every item is READY. It is read-only:
no row is written, no migration is applied, and no purge job runs. Every
reason is a fixed sentence or a count; no exception text, host, database name,
marker content or personal value is ever included.

READY here is necessary, not sufficient, for release. The go/no-go stays a
separate human decision (playbook §9), and several release prerequisites are
outside this assessment (``NOT_COVERED``).

P4-4-C3: owner decision MFA-01 was revised on 2026-10-01 (amendment A-11):
operational sign-in is email and password in staging and production. The
former ``operational_mfa`` blocker is therefore replaced by
``operational_sign_in``, which states that MFA is NOT enforced at sign-in
(``facts``) and is satisfied by the revised requirement. The MFA step-up for
sensitive operations and the shared challenge counter (CACHE-01) are assessed
as separate items; neither is affected by the revision.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

READY = "READY"
BLOCKED = "BLOCKED"
NOT_ASSESSED = "NOT_ASSESSED"

#: Release prerequisites this assessment does not check. They are listed in
#: every report so that a READY result cannot be read as covering them.
NOT_COVERED = (
    "approved country catalog (C-01)",
    "hosting, data location, key custody and backup commitments (OD-006)",
    "capacity, gate and device figures (OD-001, INFRA-001, OD-008)",
    "real providers: the MFA step-up provider's operation, the Celery broker, S3 storage, "
    "mail, malware scanner, NIN",
    "database-role isolation for the audit table (audit-isolation Fact B)",
    "staging evidence for the shared challenge counter under concurrency, outage and recovery "
    "(CACHE-01; the item here checks only its configuration and one counter write)",
    "deployment evidence: proxies, HSTS, restore drill, rehearsals",
)


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    reference: str
    status: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadinessReport:
    overall: str
    items: tuple[ReadinessItem, ...]
    not_covered: tuple[str, ...] = field(default=NOT_COVERED)
    #: Plain facts stated beside the items, so a reader never has to infer them
    #: from a status (P4-4-C3: MFA at sign-in is not enforced).
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "overall": self.overall,
            "items": [asdict(item) for item in self.items],
            "facts": dict(self.facts),
            "not_covered": list(self.not_covered),
            "note": (
                "READY is necessary, not sufficient, for release; it is not a legal, "
                "security or go-live approval. NOT_ASSESSED is never READY."
            ),
        }


def _overall(items: tuple[ReadinessItem, ...]) -> str:
    statuses = {item.status for item in items}
    if BLOCKED in statuses:
        return BLOCKED
    if NOT_ASSESSED in statuses or not items:
        return NOT_ASSESSED
    return READY


def _guarded(key: str, reference: str, assess) -> ReadinessItem:
    """Run one database-dependent assessment inside its own savepoint, so a
    failed query neither poisons the connection nor reveals its text."""
    from django.db import transaction

    try:
        with transaction.atomic():
            status, reasons = assess()
    except Exception:  # noqa: BLE001 - any failure is NOT_ASSESSED, never READY
        return ReadinessItem(key, reference, NOT_ASSESSED, ("the assessment could not run",))
    return ReadinessItem(key, reference, status, tuple(reasons))


def _assess_database_schema() -> tuple[str, list[str]]:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    # Read-only: the loader reads `django_migrations` if it exists and never
    # creates it.
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if plan:
        return NOT_ASSESSED, [
            f"{len(plan)} migration(s) are not applied; this database does not represent "
            "the release schema"
        ]
    return READY, ["the database answers and every migration is applied"]


def _assess_legal_notices() -> tuple[str, list[str]]:
    from apps.privacy.checks import legal_release_blockers

    blockers = legal_release_blockers()
    if not blockers:
        return READY, ["no unresolved marker in the effective published notices"]
    per_document = Counter((code, language) for code, language, _marker in blockers)
    return BLOCKED, [
        f"{count} unresolved item(s) in {code} ({language})"
        for (code, language), count in sorted(per_document.items())
    ]


def _assess_retention() -> tuple[str, list[str]]:
    from apps.privacy.checks import retention_release_blockers

    blockers = retention_release_blockers()
    if not blockers:
        return READY, ["approved retention periods and an approved retention job exist"]
    return BLOCKED, list(blockers)


#: The operational sign-in requirement in force (owner decision MFA-01 as
#: revised on 2026-10-01, amendment A-11).
OPERATIONAL_SIGN_IN_REQUIREMENT = (
    "EMAIL_AND_PASSWORD (owner decision MFA-01 revised 2026-10-01, amendment A-11; "
    "staging and production)"
)


def _step_up_provider_problem() -> str | None:
    """None when a step-up provider is configured and loads, else a fixed
    sentence. Never echoes the configured path or an error."""
    from django.conf import settings
    from django.utils.module_loading import import_string

    path = getattr(settings, "MFA_BACKEND", None)
    if not path:
        return "no MFA step-up provider is configured"
    if ".tests." in path:
        return "the configured MFA step-up provider is a test double"
    try:
        import_string(path)
    except Exception:  # noqa: BLE001 - never echo the configured path or error
        return "the MFA step-up provider cannot be loaded"
    return None


def _assess_operational_sign_in() -> ReadinessItem:
    from apps.accounts import mfa

    key, reference = "operational_sign_in", "MFA-01 revised (A-11), P44-F06, AF-AUTH-03"
    if not mfa.OPERATIONAL_SIGN_IN_MFA_ENFORCED:
        return ReadinessItem(
            key,
            reference,
            READY,
            (
                "operational users sign in with email and password, which is the requirement "
                "in force in staging and production (owner decision MFA-01 revised on "
                "2026-10-01, amendment A-11)",
                "MFA is NOT enforced at operational sign-in; this result does not report MFA "
                "as implemented",
                "password attempt limits, permissions, scopes, account expiry, session "
                "lifetimes, sign-out and sign-in audit are unchanged",
            ),
        )
    problem = _step_up_provider_problem()
    if problem:
        return ReadinessItem(
            key,
            reference,
            BLOCKED,
            (f"MFA is declared enforced at sign-in, but {problem}",),
        )
    return ReadinessItem(
        key,
        reference,
        READY,
        (
            "MFA is enforced at sign-in beyond the revised requirement, and a provider is "
            "configured; provider operation itself is proven only by deployment evidence",
        ),
    )


def _assess_sensitive_operation_step_up() -> ReadinessItem:
    key, reference = "sensitive_operation_step_up", "P2-F, ADR-0023, MFA-01 revised (A-11)"
    problem = _step_up_provider_problem()
    if problem:
        return ReadinessItem(
            key,
            reference,
            BLOCKED,
            (
                f"{problem}: the emergency device wipe, which requires an MFA step-up, is "
                "unavailable (it fails closed)",
                "the MFA-01 revision covers sign-in only and does not authorize bypassing "
                "this step-up; a genuine provider, or an owner decision on the unavailable "
                "wipe (STEPUP-01), is needed",
            ),
        )
    return ReadinessItem(
        key,
        reference,
        READY,
        (
            "an MFA step-up provider is configured and loads; its operation is proven only by "
            "deployment evidence",
        ),
    )


def _assess_challenge_counter() -> ReadinessItem:
    from django.conf import settings

    from apps.core import issuance_counter

    key, reference = "challenge_issuance_counter", "CACHE-01, P44-F09, ADR-0025"
    if getattr(settings, "HUMAN_CHECK_COUNTER_STORE", None) != issuance_counter.STORE_REDIS:
        return ReadinessItem(
            key,
            reference,
            BLOCKED,
            (
                "no shared challenge-issuance counter is configured "
                "(HUMAN_CHECK_COUNTER_STORE is not 'redis'); per-process counting is for local "
                "development only",
            ),
        )
    try:
        state = issuance_counter.get_issuance_limiter().probe()
    except Exception:  # noqa: BLE001 - any failure is NOT_ASSESSED, never READY
        return ReadinessItem(key, reference, NOT_ASSESSED, ("the assessment could not run",))
    if state == issuance_counter.STATE_SHARED:
        return ReadinessItem(
            key,
            reference,
            READY,
            (
                "the shared Redis counter is configured and completed a counter write "
                "(INCR and EXPIRE), not merely a PING; its behaviour under "
                "concurrency, outage and recovery is proven only by the staging verification",
            ),
        )
    return ReadinessItem(
        key,
        reference,
        BLOCKED,
        (
            "the shared Redis counter does not answer; only the stricter per-process fallback "
            "limits challenge issuance",
        ),
    )


def _assess_identity_provider() -> ReadinessItem:
    """IDV-2 (amendment A-13, A13-06, A13-12): the ministry NIN adapter.

    READY only when the official adapter is selected and every required
    setting is present and well formed; even then its real operation is proven
    only by authorized provider evidence (UAT-02). The disabled backend and
    every simulation are BLOCKED. Never echoes a configured value."""
    from apps.people.nin_provider import provider_readiness

    key, reference = "identity_verification_provider", "IDV-01, A13-06, API-01, UAT-02"
    try:
        is_official, is_ready, problems = provider_readiness()
    except Exception:  # noqa: BLE001 - any failure is NOT_ASSESSED, never READY
        return ReadinessItem(key, reference, NOT_ASSESSED, ("the assessment could not run",))
    if is_ready:
        return ReadinessItem(
            key,
            reference,
            READY,
            (
                "the official ministry adapter is selected and its configuration is complete; "
                "its real operation (network access, authentication response, error contract) "
                "is proven only by authorized provider evidence (UAT-02)",
            ),
        )
    reason = (
        "the official ministry adapter is selected but its configuration is incomplete "
        f"({len(problems)} problem(s)), so every Algerian identity goes to manual review"
        if is_official
        else "the configured NIN backend is not the official ministry adapter (disabled or a "
        "development simulation), so no identity is verified automatically"
    )
    return ReadinessItem(
        key,
        reference,
        BLOCKED,
        (reason, "the authentication response contract is still open (API-01)"),
    )


def _facts() -> dict:
    from django.conf import settings

    from apps.accounts import mfa

    return {
        "operational_sign_in_requirement": OPERATIONAL_SIGN_IN_REQUIREMENT,
        "operational_sign_in_mfa_enforced": bool(mfa.OPERATIONAL_SIGN_IN_MFA_ENFORCED),
        "mfa_step_up_provider_configured": bool(getattr(settings, "MFA_BACKEND", None)),
        "challenge_counter_store": str(getattr(settings, "HUMAN_CHECK_COUNTER_STORE", "")),
        "nin_provider_official": _nin_provider_is_official(),
    }


def _nin_provider_is_official() -> bool:
    try:
        from apps.people.nin_provider import provider_readiness

        return bool(provider_readiness()[0])
    except Exception:  # noqa: BLE001 - a fact is never an exception
        return False


def assess_release_readiness() -> ReadinessReport:
    schema = _guarded("database_schema", "DEP-005", _assess_database_schema)
    database_items = (
        ("legal_notices", "C-09, privacy.W001", _assess_legal_notices),
        ("retention", "OD-007, C-09, privacy.W002", _assess_retention),
    )
    if schema.status == READY:
        assessed = tuple(_guarded(*item) for item in database_items)
    else:
        # Facts read from an unavailable or outdated schema do not describe the
        # release (an unapplied data migration can hide the current notices),
        # so they are not assessed rather than reported from stale rows.
        assessed = tuple(
            ReadinessItem(
                key,
                reference,
                NOT_ASSESSED,
                ("not assessed: the database schema is unavailable or not current",),
            )
            for key, reference, _assess in database_items
        )
    items = (
        schema,
        *assessed,
        _assess_operational_sign_in(),
        _assess_sensitive_operation_step_up(),
        _assess_challenge_counter(),
        _assess_identity_provider(),
    )
    return ReadinessReport(overall=_overall(items), items=items, facts=_facts())
