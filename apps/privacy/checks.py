"""Legal-notice release blockers (UX-3, M22, C-09).

The published notices are complete drafts whose unconfirmed institutional
facts carry explicit markers. `legal_release_blockers()` lists them, and the
database-tagged system check `privacy.W001` reports them with
`manage.py check --database default`, so a release cannot go out with an
unresolved controller address, contact, ANPDP reference, recipient list,
transfer statement or retention period.

`privacy.W002` (P4-4) reports the unresolved retention facts the same way:
missing categories or periods, and the absence of any participant-data
retention job.

P4-4-C1 (review finding R-03): when either assessment cannot run (for example
before `migrate`, or with an unreachable database), `privacy.W003` or
`privacy.W004` says so; an unassessable state is no longer silent. A plain
`manage.py check` without `--database` still does not assess them (Django's
convention for database checks). `manage.py release_readiness` is the
explicit READY / BLOCKED / NOT_ASSESSED result, and a warning is never a legal
approval.
"""

from __future__ import annotations

import re

from django.core.checks import Tags, Warning, register

#: Markers used in the EN, FR and AR drafts.
MARKER_PATTERN = re.compile(r"\[(?:TO BE CONFIRMED|À CONFIRMER|يُستكمل)\s*:[^\]]*\]")


def legal_release_blockers() -> list[tuple[str, str, str]]:
    """`(document code, language, marker)` for every marker in the currently
    effective PUBLISHED notices."""
    from apps.privacy.selectors import effective_published_version

    blockers = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        for language in ("en", "fr", "ar"):
            version = effective_published_version(code, language)
            if version is None:
                blockers.append((code, language, "missing effective version"))
                continue
            for match in MARKER_PATTERN.finditer(version.content):
                blockers.append((code, language, match.group(0)))
    return blockers


def _not_assessed(subject: str, check_id: str) -> Warning:
    return Warning(
        f"{subject} could not be assessed (NOT_ASSESSED): the database or its schema is not "
        "available. This is never a release approval; run `manage.py release_readiness`.",
        id=check_id,
    )


def _assess(function):
    """Run one assessment in its own savepoint, so a failed query (tables may
    not exist before migrate) leaves the connection usable."""
    from django.db import transaction

    with transaction.atomic():
        return function()


@register(Tags.database)
def legal_notice_markers(app_configs=None, databases=None, **kwargs):
    if not databases:
        return []
    try:
        blockers = _assess(legal_release_blockers)
    except Exception:  # noqa: BLE001 - reported as NOT_ASSESSED, never as no blocker
        return [_not_assessed("Legal-notice release blockers (C-09)", "privacy.W003")]
    if not blockers:
        return []
    return [
        Warning(
            f"{len(blockers)} unresolved legal-notice item(s), e.g. {blockers[0][0]} "
            f"({blockers[0][1]}): {blockers[0][2][:80]}. These are release blockers (C-09).",
            id="privacy.W001",
        )
    ]


#: P4-4: no job purges or anonymizes participant data. Retention periods are
#: an owner decision (OD-007, C-09); until they are approved and a reviewed,
#: legal-hold-safe job exists, this stays a release blocker.
PARTICIPANT_RETENTION_JOB_IMPLEMENTED = False


def retention_release_blockers() -> list[str]:
    """Unresolved retention facts, stated without any data value."""
    from apps.privacy.models import RetentionCategory

    blockers = []
    active = RetentionCategory.objects.filter(is_active=True)
    if not active.exists():
        blockers.append("no active retention category is configured")
    pending = sorted(
        active.filter(retention_period_days__isnull=True).values_list("code", flat=True)
    )
    if pending:
        blockers.append(f"{len(pending)} retention category(ies) without an approved period")
    if not PARTICIPANT_RETENTION_JOB_IMPLEMENTED:
        blockers.append("no approved participant-data retention or purge job exists")
    return blockers


@register(Tags.database)
def retention_markers(app_configs=None, databases=None, **kwargs):
    if not databases:
        return []
    try:
        blockers = _assess(retention_release_blockers)
    except Exception:  # noqa: BLE001 - reported as NOT_ASSESSED, never as no blocker
        return [_not_assessed("Retention release blockers (OD-007)", "privacy.W004")]
    if not blockers:
        return []
    return [
        Warning(
            "Retention is unresolved: " + "; ".join(blockers) + ". These are release blockers "
            "(OD-007, C-09); a warning is not a legal approval.",
            id="privacy.W002",
        )
    ]
