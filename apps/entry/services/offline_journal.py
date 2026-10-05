"""Durable invalidation for Offline Package projections (Phase 4 Prompt 2,
independent re-review correction C, ADR-0023, entry.0004).

Why not `updated_at`: `QuerySet.update()`, bulk operations and raw SQL can
change a row without touching `updated_at`, and a timestamp says nothing
about COMMIT order -- a transaction that started before a package was built
can commit after it with an older timestamp. Either would let a
risk-increasing change silently miss both the package and its deltas.

Instead:

1. PostgreSQL triggers (entry.0004) append one `OfflineChangeJournal` row
   for every INSERT, UPDATE or DELETE on every table in `TRACKED_TABLES`,
   whoever issues it, and a statement row for a TRUNCATE. Nothing an
   application service does -- or forgets to do -- can bypass them.
2. A package records the PostgreSQL snapshot taken BEFORE its first
   projection query (`capture_watermark`). A journal row is covered by the
   package iff its transaction is visible in that snapshot (or, for the
   building transaction itself, iff the row precedes the capture). The
   projection reads a state at least as new as the snapshot, so everything
   the snapshot does not cover is re-sent by the next delta -- in commit
   order, with no lock and therefore no deadlock with the writers.
3. `journal_changes` translates the uncovered rows into the targets a delta
   re-states from CURRENT authoritative state. Anything it cannot translate
   precisely fails closed: a TRUNCATE, a deleted row the delta would need,
   an unknown table, more rows than `ENTRY_OFFLINE_DELTA_MAX_CHANGES`, or a
   package without a watermark all require a full rebuild, and the
   blocking reasons (`REBUILD_BLOCKING_REASONS`) Block the device offline.

Residual limit (documented in ADR-0023): a database superuser can disable
triggers (`session_replication_role = replica`); that is outside what an
application can enforce and is covered by database-role separation.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.db import connection

#: Every table whose rows can change a package or a delta, with the id
#: columns the trigger records (old and new values). Keep in step with
#: entry.0004 `_TRACKED_TABLES`.
TRACKED_TABLES: dict[str, tuple[str, ...]] = {
    "badges_digital_entry_pass": ("registration_id",),
    "badges_verification_key": (),
    "entry_security_restriction": ("registration_id", "person_id"),
    "accreditation_badge_type_assignment": ("registration_id",),
    "accreditation_access_profile_assignment": ("registration_id",),
    "accreditation_access_rule_assignment": ("registration_id", "access_rule_id"),
    "accreditation_access_rule": ("access_profile_id",),
    "accreditation_access_profile": (),
    "registrations_registration": ("person_id",),
    "events_venue": (),
    "events_gate": ("venue_id",),
    "events_zone": ("venue_id",),
    "entry_override_reason": (),
}

_ASSIGNMENT_TABLES = frozenset(
    {
        "accreditation_badge_type_assignment",
        "accreditation_access_profile_assignment",
        "accreditation_access_rule_assignment",
    }
)
_CHECKPOINT_TABLES = frozenset({"events_venue", "events_gate", "events_zone"})


@dataclass(frozen=True)
class Watermark:
    snapshot: str
    xmin: int
    txid: int
    high_id: int


def capture_watermark() -> Watermark:
    """The current PostgreSQL snapshot, its xmin, this transaction's id and
    the highest journal id visible now. Call inside the projection's
    transaction, BEFORE the first projection query."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_current_snapshot()::text, "
            "pg_snapshot_xmin(pg_current_snapshot())::text::bigint, "
            "pg_current_xact_id()::text::bigint, "
            "COALESCE((SELECT max(id) FROM entry_offline_change_journal), 0)"
        )
        snapshot, xmin, txid, high_id = cursor.fetchone()
    return Watermark(snapshot=snapshot, xmin=xmin, txid=txid, high_id=high_id)


@dataclass
class JournalChanges:
    """Targets a delta must re-state, and the reasons it cannot."""

    rebuild_reasons: set[str] = field(default_factory=set)
    pass_ids: set[str] = field(default_factory=set)
    assignment_registration_ids: set[str] = field(default_factory=set)
    rule_ids: set[str] = field(default_factory=set)
    profile_ids: set[str] = field(default_factory=set)
    restriction_registration_ids: set[str] = field(default_factory=set)
    restriction_person_ids: set[str] = field(default_factory=set)
    key_ids: set[str] = field(default_factory=set)
    #: Override reasons changed since the snapshot (Phase 4 Prompt 3
    #: correction 4, R-01): offline override revalidation can then tell a
    #: catalogue entry still exactly as the package carried it from one the
    #: device's package may have shown differently.
    override_reason_ids: set[str] = field(default_factory=set)
    #: The uncovered operation kinds ("I", "U", "D") of each of those rows
    #: (correction 5, developer decision D2, option A2). A row whose uncovered
    #: history is INSERT-only has only ever had its current values; the
    #: journal records no previous value for anything else.
    override_reason_operations: dict[str, set[str]] = field(default_factory=dict)
    row_count: int = 0


def _uncovered_rows(package, *, limit: int) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, operation, row_id, refs
            FROM entry_offline_change_journal
            WHERE (txid >= %(xmin)s OR txid = %(own)s)
              AND (event_edition_id IS NULL OR event_edition_id = %(event)s)
              AND CASE
                    WHEN txid = %(own)s THEN id > %(high)s
                    ELSE NOT pg_visible_in_snapshot(
                        txid::text::xid8, %(snapshot)s::pg_snapshot
                    )
                  END
            ORDER BY id
            LIMIT %(limit)s
            """,
            {
                "xmin": package.journal_xmin,
                "own": package.build_txid,
                "high": package.journal_high_id,
                "snapshot": package.data_snapshot,
                "event": str(package.event_edition_id),
                "limit": limit,
            },
        )
        return cursor.fetchall()


def journal_changes(package) -> JournalChanges:
    """Every journaled change the package's snapshot does not cover."""
    changes = JournalChanges()
    if not package.data_snapshot:
        changes.rebuild_reasons.add("SNAPSHOT_MISSING")
        return changes
    cap = settings.ENTRY_OFFLINE_DELTA_MAX_CHANGES
    rows = _uncovered_rows(package, limit=cap + 1)
    changes.row_count = len(rows)
    if len(rows) > cap:
        changes.rebuild_reasons.add("BULK_CHANGE")
        return changes
    for table, operation, row_id, raw_refs in rows:
        refs = json.loads(raw_refs) if isinstance(raw_refs, str) else (raw_refs or {})
        if operation == "T" or table not in TRACKED_TABLES:
            changes.rebuild_reasons.add("UNTRACKED_CHANGE")
        elif table == "badges_digital_entry_pass":
            if operation == "D":
                changes.rebuild_reasons.add("UNTRACKED_CHANGE")
            else:
                changes.pass_ids.add(row_id)
        elif table in _ASSIGNMENT_TABLES:
            changes.assignment_registration_ids.update(refs.get("registration_id") or [])
        elif table == "accreditation_access_rule":
            changes.rule_ids.add(row_id)
            changes.profile_ids.update(refs.get("access_profile_id") or [])
        elif table == "accreditation_access_profile":
            if operation == "D":
                changes.rebuild_reasons.add("UNTRACKED_CHANGE")
            else:
                changes.profile_ids.add(row_id)
        elif table == "entry_security_restriction":
            changes.restriction_registration_ids.update(refs.get("registration_id") or [])
            changes.restriction_person_ids.update(refs.get("person_id") or [])
        elif table == "registrations_registration":
            # Eligibility is re-checked for EVERY active pass on each delta;
            # a context re-linked to another person needs its restrictions
            # re-stated.
            changes.restriction_registration_ids.add(row_id)
        elif table == "badges_verification_key":
            if operation == "D":
                changes.rebuild_reasons.add("KEY_SET_CHANGED")
            else:
                changes.key_ids.add(row_id)
        elif table in _CHECKPOINT_TABLES:
            changes.rebuild_reasons.add("CHECKPOINT_CHANGED")
        elif table == "entry_override_reason":
            changes.rebuild_reasons.add("OVERRIDE_CATALOGUE_CHANGED")
            changes.override_reason_ids.add(row_id)
            changes.override_reason_operations.setdefault(row_id, set()).add(operation)
    return changes


def tables_by_trigger() -> dict[str, set[str]]:
    """Installed journal triggers per table (for checks and tests)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, t.tgname
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            WHERE NOT t.tgisinternal AND t.tgname IN (
                'entry_offline_journal_row', 'entry_offline_journal_truncate'
            )
            """
        )
        result: dict[str, set[str]] = defaultdict(set)
        for table, trigger in cursor.fetchall():
            result[table].add(trigger)
    return dict(result)


def prune_journal(*, now) -> int:
    """Delete journal rows older than the retention that no READY package
    can still need (a row is needed while it is at or above a live
    package's snapshot xmin, or was written by its building transaction)."""
    from datetime import timedelta

    cutoff = now - timedelta(seconds=settings.ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM entry_offline_change_journal AS j
            WHERE j.recorded_at < %s
              AND NOT EXISTS (
                  SELECT 1 FROM entry_offline_package AS p
                  WHERE p.status = 'READY' AND p.data_snapshot <> ''
                    AND (j.txid >= p.journal_xmin OR j.txid = p.build_txid)
              )
            """,
            [cutoff],
        )
        return cursor.rowcount
