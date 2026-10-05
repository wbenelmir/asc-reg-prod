"""Server-side session state for one checkpoint browser.

Everything a checkpoint must remember BETWEEN requests -- the current
device/operator session ids, pending verifications awaiting a decision,
candidate lists awaiting a selection, and short-lived photo grants -- is
held in the server-side Django session under `entry_*` keys. The browser
only ever holds random opaque handles, never a registration id, a person
id, a credential id, or any participant value; a forged or replayed handle
simply matches nothing.

Every collection is size-bounded and time-bounded, and the whole state is
dropped on operator sign-out, on session end, and on every expiry path
(`clear_entry_session_state`), so participant details never outlive the
operator who looked them up (Flow §15.3).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

DEVICE_SESSION_KEY = "entry_device_session_id"
OPERATOR_SESSION_KEY = "entry_operator_session_id"
PENDING_KEY = "entry_pending"
CANDIDATES_KEY = "entry_candidates"
PHOTO_KEY = "entry_photo"

_MAX_PENDING = 5
_MAX_CANDIDATE_LISTS = 3
_MAX_PHOTOS = 3


def _now_iso() -> str:
    return timezone.now().isoformat()


def _age_seconds(stamp: object) -> float:
    try:
        return (timezone.now() - datetime.fromisoformat(str(stamp))).total_seconds()
    except ValueError:
        return float("inf")


def _bounded(mapping: dict, *, limit: int, max_age: int) -> dict:
    fresh = {k: v for k, v in mapping.items() if _age_seconds(v.get("created_at")) <= max_age}
    newest = sorted(fresh.items(), key=lambda item: item[1].get("created_at", ""))[-limit:]
    return dict(newest)


# -- checkpoint session ids ---------------------------------------------------


def set_checkpoint_ids(request, *, device_session_id, operator_session_id) -> None:
    request.session[DEVICE_SESSION_KEY] = str(device_session_id)
    request.session[OPERATOR_SESSION_KEY] = str(operator_session_id)
    clear_participant_state(request)


def checkpoint_ids(request) -> tuple[str | None, str | None]:
    return request.session.get(DEVICE_SESSION_KEY), request.session.get(OPERATOR_SESSION_KEY)


# -- pending verifications ------------------------------------------------------


def store_pending(request, pending) -> None:
    data = dict(request.session.get(PENDING_KEY) or {})
    entry = pending.to_session()
    entry["created_at"] = _now_iso()
    data[pending.operation_id] = entry
    # Kept past the decision itself (until expiry), so a double-submitted
    # decision resolves to the SAME operation and is answered as a replay.
    request.session[PENDING_KEY] = _bounded(
        data, limit=_MAX_PENDING, max_age=settings.ENTRY_DECISION_TICKET_SECONDS * 5
    )


def get_pending(request, operation_id: str):
    from apps.entry.services.decisions import PendingVerification

    data = request.session.get(PENDING_KEY) or {}
    return PendingVerification.from_session(data.get(operation_id))


# -- candidate lists --------------------------------------------------------------


def store_candidates(request, *, method: str, registration_ids, verified_ids=()) -> str:
    token = secrets.token_urlsafe(18)
    data = dict(request.session.get(CANDIDATES_KEY) or {})
    data[token] = {
        "method": method,
        "ids": [str(pk) for pk in registration_ids],
        "verified": sorted(str(pk) for pk in verified_ids),
        "created_at": _now_iso(),
    }
    request.session[CANDIDATES_KEY] = _bounded(
        data, limit=_MAX_CANDIDATE_LISTS, max_age=settings.ENTRY_DECISION_TICKET_SECONDS
    )
    return token


def take_candidate(request, selection, index: int):
    """Return (method, registration_id, identity_verified) or None."""
    data = request.session.get(CANDIDATES_KEY) or {}
    entry = data.get(selection)
    if not isinstance(entry, dict):
        return None
    if _age_seconds(entry.get("created_at")) > settings.ENTRY_DECISION_TICKET_SECONDS:
        return None
    ids = entry.get("ids") or []
    if not 0 <= index < len(ids):
        return None
    registration_id = ids[index]
    identity_verified = None
    if entry.get("method") in ("NIN", "PASSPORT"):
        identity_verified = registration_id in set(entry.get("verified") or [])
    return entry.get("method"), registration_id, identity_verified


# -- photo grants -----------------------------------------------------------------


def grant_photo(request, registration_id) -> str:
    nonce = secrets.token_urlsafe(18)
    data = dict(request.session.get(PHOTO_KEY) or {})
    data[nonce] = {"registration_id": str(registration_id), "created_at": _now_iso()}
    request.session[PHOTO_KEY] = _bounded(
        data, limit=_MAX_PHOTOS, max_age=settings.ENTRY_DECISION_TICKET_SECONDS
    )
    return nonce


def photo_registration_id(request, nonce: str) -> str | None:
    entry = (request.session.get(PHOTO_KEY) or {}).get(nonce)
    if not isinstance(entry, dict):
        return None
    if _age_seconds(entry.get("created_at")) > settings.ENTRY_DECISION_TICKET_SECONDS:
        return None
    return entry.get("registration_id")


# -- clearing ---------------------------------------------------------------------


def clear_participant_state(request) -> None:
    """Drop candidate lists and photo grants (after a decision or clear)."""
    request.session.pop(CANDIDATES_KEY, None)
    request.session.pop(PHOTO_KEY, None)


def clear_entry_session_state(request, *, reason: str) -> None:
    """End this browser's operator session and drop every entry key."""
    _, operator_session_id = checkpoint_ids(request)
    if operator_session_id:
        from django.core.exceptions import ValidationError

        from apps.entry.models import EntryOperatorSession
        from apps.entry.services.sessions import end_operator_session

        try:
            operator_session = EntryOperatorSession.objects.filter(pk=operator_session_id).first()
        except (ValueError, ValidationError):  # fmt: skip
            operator_session = None
        if operator_session is not None:
            end_operator_session(operator_session=operator_session, reason=reason)
    for key in (DEVICE_SESSION_KEY, OPERATOR_SESSION_KEY, PENDING_KEY):
        request.session.pop(key, None)
    clear_participant_state(request)


def decision_window() -> timedelta:
    return timedelta(seconds=settings.ENTRY_DECISION_TICKET_SECONDS)
