"""Backfill participation review cases for registrations verified before the
identity -> review link existed (`apps.reviews.intake`).

    uv run --env-file .env python manage.py backfill_review_queue --event ASC2026
    uv run --env-file .env python manage.py backfill_review_queue --event ASC2026 --apply

The default is a DRY RUN: it classifies every registration in scope and
writes nothing. `--apply` creates only the missing STANDARD / GENERAL cases of
eligible registrations and makes their SUBMITTED -> UNDER_REVIEW transition,
one short transaction per registration, rechecked under the Registration lock
by the same service the identity success paths use. It never deletes,
renumbers or rewrites a registration, a submission, an identity result, a
document or a decision; never approves; and sends no communication, invitation
or badge and calls no Ministry service. Running it again changes nothing more.

Output: aggregate counts only (no reference, name or identifier). Exit code 0;
2 when `--apply` left registrations unchanged because their rows stayed locked
(run it again).
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.reviews.intake import DEFAULT_BACKFILL_BATCH_SIZE, run_backfill


class Command(BaseCommand):
    help = (
        "Dry run (default) or --apply: enqueue verified, submitted registrations that await a "
        "participation decision but have no review case. Reports counts only."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--event",
            dest="event_code",
            default="",
            help="Restrict to one event edition, by its code (EventEdition.code).",
        )
        parser.add_argument(
            "--all-events",
            action="store_true",
            help="Examine every event edition (required when --event is not given).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create the missing cases and transitions. Without it nothing is written.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BACKFILL_BATCH_SIZE,
            help="Registrations read per batch (1-1000).",
        )
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options) -> None:
        from apps.events.models import EventEdition

        event_code = (options["event_code"] or "").strip()
        if not event_code and not options["all_events"]:
            raise CommandError("Pass --event <code> (recommended) or --all-events.")
        event_edition = None
        if event_code:
            event_edition = EventEdition.objects.filter(code=event_code).first()
            if event_edition is None:
                raise CommandError("No event edition has this code.")
        report = run_backfill(
            event_edition=event_edition,
            apply=options["apply"],
            batch_size=options["batch_size"],
        ).as_dict()
        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            self.stdout.write(f"Review queue backfill ({report['mode']}), event {report['event']}")
            for key in (
                "examined",
                "eligible_missing_case",
                "existing_case",
                "status_transitions",
                "excluded_total",
            ):
                self.stdout.write(f"  {key}: {report[key]}")
            for reason, count in report["excluded"].items():
                self.stdout.write(f"    excluded {reason}: {count}")
            if options["apply"]:
                for key in ("created", "transitioned", "conflicts"):
                    self.stdout.write(f"  {key}: {report[key]}")
                for reason, count in report["excluded_at_apply"].items():
                    self.stdout.write(f"    changed before apply, excluded {reason}: {count}")
            else:
                self.stdout.write("  Dry run: nothing was written. Re-run with --apply to apply.")
        if options["apply"] and report["conflicts"]:
            raise SystemExit(2)
