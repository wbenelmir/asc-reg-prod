"""Print the identifier-free entry observability snapshot as JSON (Prompt 5).

For an operator or an external monitoring job (TRD §23.1). The output holds
aggregates, codes and checkpoint coordinates only -- the same read model as
the dashboard, never a participant, credential, token, or identity value.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.entry.observability import snapshot
from apps.events.models import EventEdition


class Command(BaseCommand):
    help = "Print the entry observability snapshot for one event edition as JSON."

    def add_arguments(self, parser):
        parser.add_argument("event_code", help="Event edition code, for example ASC2026.")
        parser.add_argument("--window", type=int, default=None, help="Look-back window in seconds.")

    def handle(self, *args, event_code, window, **options):
        event = EventEdition.objects.filter(code=event_code).first()
        if event is None:
            raise CommandError(f"No event edition with code {event_code!r}.")
        data = snapshot(event_edition=event, window_seconds=window)
        self.stdout.write(json.dumps(data, indent=2, default=str))
