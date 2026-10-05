"""Self-hosted ALTCHA human check (UX-4, M01, D-13, S-16; UX-C2 decision
UX-D01 option M).

The maintained ALTCHA widget (vendored in `static/vendor/altcha/`) solves a
proof-of-work challenge in a Web Worker. The official `altcha` Python library
creates and verifies it here, locally, with an HMAC key derived from
`SECRET_KEY`. There is no ALTCHA Cloud, Sentinel, remote verification or other
external service, and no visual puzzle.

What this module adds to the library:

1. **Action binding.** The challenge's signed `data` names the action (for
   example `otp_request`), and the verifier requires that action.
2. **Session binding.** The signed `data` carries an HMAC of a random nonce
   kept in the issuing session. A solution solved in one session is refused
   in another. It is never bound to an e-mail address, so the check reveals
   nothing about accounts.
3. **One-time consumption.** The library has no replay protection. After the
   complete challenge (signature, expiry, solution, action and binding) is
   verified, the digest of its signature is inserted into
   `core.HumanChallengeUse`. Its unique constraint refuses replays, including
   concurrent ones.

The check supplements the OTP throttles (ADR-0007); it never replaces them. It
fails closed. The only way around it is the documented recovery switch
`HUMAN_CHECK_ENABLED=false` in the deployment environment, which needs
deployment access, logs a warning on every use and is reported by system check
`core.W001`. Payloads, nonces and signatures are never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.crypto import salted_hmac

logger = logging.getLogger(__name__)

#: Version of this project's signed `data` layout inside the ALTCHA challenge.
VERSION = 2
_SIGNING_SALT = "asc.human-check.v2.signing"
_BINDING_SALT = "asc.human-check.v2.session-binding"
#: Session key of the issuing session's random nonce.
SESSION_NONCE_KEY = "human_check_nonce"
#: Upper bound of an encoded payload; anything longer is malformed.
MAX_PAYLOAD_LENGTH = 4096
#: Rows purged per consumption, so the replay store stays small without a scheduler.
_PURGE_BATCH = 200

ACTION_OTP_REQUEST = "otp_request"


class HumanCheckFailed(Exception):
    """The posted solution was missing, malformed, forged, stale, bound to
    another session or replayed.

    `reason` is a stable code for tests and logs; callers show one generic
    message whatever it is.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def is_enabled() -> bool:
    return bool(getattr(settings, "HUMAN_CHECK_ENABLED", True))


def _signing_key() -> bytes:
    return salted_hmac(_SIGNING_SALT, "altcha-challenge", algorithm="sha256").digest()


def _binding(nonce: str) -> str:
    return salted_hmac(_BINDING_SALT, nonce, algorithm="sha256").hexdigest()


def _session_nonce(session, *, create: bool) -> str | None:
    nonce = session.get(SESSION_NONCE_KEY)
    if not isinstance(nonce, str) or len(nonce) != 64:
        nonce = None
    if nonce is None and create:
        nonce = secrets.token_hex(32)
        session[SESSION_NONCE_KEY] = nonce
    return nonce


def issue_challenge(action: str, *, session, now: float | None = None) -> dict:
    """A signed ALTCHA challenge (the widget's JSON), bound to `action` and to
    `session`. Creates the session nonce on first use; reuses it afterwards,
    so a page with an earlier, unexpired challenge still works."""
    import altcha

    now = time.time() if now is None else now
    nonce = _session_nonce(session, create=True)
    challenge = altcha.create_challenge(
        algorithm=settings.HUMAN_CHECK_ALGORITHM,
        cost=int(settings.HUMAN_CHECK_COST),
        expires_at=int(now) + int(settings.HUMAN_CHECK_TTL_SECONDS),
        data={"v": VERSION, "a": action, "b": _binding(nonce)},
        hmac_secret=_signing_key(),
    )
    return challenge.to_dict()


def _decode(payload: str):
    """Parse the widget payload into an `altcha.Payload`, refusing any shape
    the library would not handle safely (for example a non-integer counter)."""
    import altcha

    if not payload:
        raise HumanCheckFailed("MISSING")
    if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD_LENGTH:
        raise HumanCheckFailed("MALFORMED")
    try:
        raw = json.loads(base64.b64decode(payload.encode("ascii"), validate=True))
        parsed = altcha.Payload.from_dict(raw)
    except ValueError, KeyError, TypeError, UnicodeError, binascii.Error:
        raise HumanCheckFailed("MALFORMED") from None
    params = parsed.challenge.parameters
    solution = parsed.solution
    counter = solution.counter
    if (
        not isinstance(counter, int)
        or isinstance(counter, bool)
        or not 0 <= counter < 2**32
        or not isinstance(solution.derived_key, str)
        or not isinstance(parsed.challenge.signature, str)
        or not isinstance(params.expires_at, int)
        or isinstance(params.expires_at, bool)
        or not isinstance(params.data, dict)
        or params.algorithm != settings.HUMAN_CHECK_ALGORITHM
    ):
        raise HumanCheckFailed("MALFORMED")
    return parsed


def verify_and_consume(payload: str, *, action: str, session, now: float | None = None) -> str:
    """Accept a solved challenge once, or raise `HumanCheckFailed`.

    Returns "OK", or "DISABLED" when the recovery switch is on. Order: shape;
    expiry (inclusive); the library's verification (signature in constant
    time, then the solution); this project's signed version and action; the
    session binding; and only then the one-time consumption, in its own
    savepoint.
    """
    import altcha

    if not is_enabled():
        logger.warning("human check skipped: HUMAN_CHECK_ENABLED is false (recovery switch)")
        return "DISABLED"
    now = time.time() if now is None else now
    parsed = _decode(payload)
    params = parsed.challenge.parameters
    if now > params.expires_at:
        raise HumanCheckFailed("EXPIRED")
    try:
        result = altcha.verify_solution(parsed, _signing_key())
    except ValueError, TypeError, KeyError, OverflowError, AttributeError:
        raise HumanCheckFailed("MALFORMED") from None
    if result.expired:
        raise HumanCheckFailed("EXPIRED")
    if result.invalid_signature:
        raise HumanCheckFailed("BAD_SIGNATURE")
    if not result.verified:
        raise HumanCheckFailed("WRONG_SOLUTION")
    data = params.data
    if data.get("v") != VERSION or data.get("a") != action:
        raise HumanCheckFailed("WRONG_ACTION")
    nonce = _session_nonce(session, create=False)
    binding = data.get("b")
    if (
        nonce is None
        or not isinstance(binding, str)
        or not hmac.compare_digest(binding, _binding(nonce))
    ):
        raise HumanCheckFailed("WRONG_SESSION")

    from apps.core.models import HumanChallengeUse

    expires_at = datetime.fromtimestamp(params.expires_at, tz=UTC)
    signature_digest = hashlib.sha256(parsed.challenge.signature.encode("ascii")).hexdigest()
    try:
        with transaction.atomic():
            HumanChallengeUse.objects.create(
                signature_digest=signature_digest, action=action, expires_at=expires_at
            )
    except IntegrityError:
        raise HumanCheckFailed("REPLAYED") from None
    purge_expired_uses(now=now)
    return "OK"


def purge_expired_uses(*, now: float | None = None, batch: int = _PURGE_BATCH) -> int:
    """Delete a bounded batch of expired replay-store rows. Idempotent."""
    from apps.core.models import HumanChallengeUse

    cutoff = datetime.fromtimestamp(time.time() if now is None else now, tz=UTC)
    stale = list(
        HumanChallengeUse.objects.filter(expires_at__lt=cutoff).values_list("pk", flat=True)[:batch]
    )
    if not stale:
        return 0
    deleted, _ = HumanChallengeUse.objects.filter(pk__in=stale).delete()
    return deleted


# ---------------------------------------------------------------------------
# Challenge-endpoint abuse limits (P4-4)
# ---------------------------------------------------------------------------

_ISSUANCE_SALT = "asc.human-check.v2.issuance-throttle"


def _issuance_bucket(network_identity: str) -> str:
    """The throttled unit: one IPv4 address, or one IPv6 /64 (a single client
    usually controls a whole /64). Anything unparsable is its own bucket."""
    import ipaddress

    try:
        address = ipaddress.ip_address(network_identity)
    except ValueError:
        return network_identity or "-"
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def issuance_digest(network_identity: str) -> str:
    """The keyed digest that names a client network in the issuance counter.
    The address itself is never stored or logged."""
    return salted_hmac(
        _ISSUANCE_SALT, _issuance_bucket(network_identity), algorithm="sha256"
    ).hexdigest()[:32]


def challenge_issuance_allowed(network_identity: str, *, now: float | None = None) -> bool:
    """Count one challenge request for this client network in a fixed window
    and say whether it is within the limit.

    Called before the session is touched, so a refused request creates no
    session row. P4-4-C3 (CACHE-01): in staging and production the count is
    shared by every process through Redis, at most
    `HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW` per window. If the shared counter
    fails, a per-process fallback applies the stricter
    `HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW` and an operational alert is
    logged; the request is never allowed merely because counting failed, and
    it is refused if no counter can count. See `apps.core.issuance_counter`.
    It stays a first line of defence: the proof of work and the
    database-backed OTP throttles (ADR-0007) still apply to every OTP request.
    """
    from apps.core.issuance_counter import get_issuance_limiter

    now = time.time() if now is None else now
    return get_issuance_limiter().allow(issuance_digest(network_identity), now=now).allowed


def mark_anonymous_session_short_lived(session) -> None:
    """A session created only to hold a challenge nonce expires after
    `HUMAN_CHECK_ANONYMOUS_SESSION_SECONDS` of inactivity, not the ordinary
    session lifetime. A successful sign-in restores the ordinary lifetime
    (`participant_auth.login_participant`, operational sign-in)."""
    session.set_expiry(int(settings.HUMAN_CHECK_ANONYMOUS_SESSION_SECONDS))


def solve(challenge: dict) -> str:
    """Solve a challenge dict and return the payload the widget would post.
    Test and tooling use only; the server chooses the cost."""
    import altcha

    parsed = altcha.Challenge.from_dict(challenge)
    solution = altcha.solve_challenge(parsed, timeout=60)
    if solution is None:
        raise ValueError("challenge could not be solved in time")
    return altcha.Payload(parsed, solution).to_base64()
