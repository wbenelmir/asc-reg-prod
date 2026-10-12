"""Server-side identity queue search contexts (IDV-C1, R-IDV-05).

A queue search is submitted by POST and resolved ONCE into a bounded list of
case ids (`apps.people.selectors.identity.resolve_queue_search`). This module
keeps that list in the reviewer's server-side session under a random
reference; URLs carry only the reference (`ctx`). Neither the search text nor
a NIN is ever stored here, in a cookie or in a URL.

A context is bound to the reviewer who created it and to the session, expires
after `SEARCH_CONTEXT_TTL`, and at most `SEARCH_CONTEXTS_PER_SESSION` are kept.
It only narrows the queue: scope (and, for a NIN search, the evidence
permission) is applied again by the selectors on every use. HTTP session
state only; no business logic.

UPD-05: the operations registrations list keeps its own private searches the
same way, under its own session key (`namespace`), holding registration ids.
"""

from __future__ import annotations

import datetime
import re
import secrets
from dataclasses import dataclass

from django.utils import timezone

SEARCH_SESSION_KEY = "idv_queue_search"
SEARCH_CONTEXT_TTL = datetime.timedelta(minutes=30)
SEARCH_CONTEXTS_PER_SESSION = 5
REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")


@dataclass(frozen=True)
class SearchContext:
    reference: str
    kind: str  # "nin" or "text"
    case_ids: tuple[str, ...]
    truncated: bool


def _live_contexts(session, user, namespace: str = SEARCH_SESSION_KEY) -> dict:
    """This reviewer's unexpired contexts; everything else is dropped."""
    now = timezone.now()
    live = {}
    for reference, entry in (session.get(namespace) or {}).items():
        try:
            created = datetime.datetime.fromisoformat(entry["created"])
        except KeyError, TypeError, ValueError:
            continue
        if entry.get("user") == str(user.pk) and now - created < SEARCH_CONTEXT_TTL:
            live[reference] = entry
    return live


def store_search_context(
    session, user, *, kind: str, case_ids, truncated: bool, namespace: str = SEARCH_SESSION_KEY
) -> str:
    """Keep a resolved search; returns its new random reference."""
    contexts = _live_contexts(session, user, namespace)
    reference = secrets.token_urlsafe(18)
    contexts[reference] = {
        "user": str(user.pk),
        "kind": kind,
        "ids": [str(case_id) for case_id in case_ids],
        "truncated": bool(truncated),
        "created": timezone.now().isoformat(),
    }
    newest = sorted(contexts.items(), key=lambda item: item[1]["created"])
    session[namespace] = dict(newest[-SEARCH_CONTEXTS_PER_SESSION:])
    return reference


def search_context_for(
    session, user, reference: str, namespace: str = SEARCH_SESSION_KEY
) -> SearchContext | None:
    """The reviewer's own live context `reference`, or None (unknown, expired,
    another reviewer's). Expired and foreign entries are forgotten."""
    contexts = _live_contexts(session, user, namespace)
    if contexts != (session.get(namespace) or {}):
        session[namespace] = contexts
    if not reference or not REFERENCE_PATTERN.fullmatch(reference):
        return None
    entry = contexts.get(reference)
    if entry is None:
        return None
    return SearchContext(
        reference=reference,
        kind=entry["kind"],
        case_ids=tuple(entry["ids"]),
        truncated=bool(entry.get("truncated")),
    )


def clear_search_context(
    session, user, reference: str, namespace: str = SEARCH_SESSION_KEY
) -> None:
    contexts = _live_contexts(session, user, namespace)
    contexts.pop(reference, None)
    session[namespace] = contexts
