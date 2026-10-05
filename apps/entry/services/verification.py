"""Checkpoint lookups, in the approved priority order (Flow §10.4):

1. signed Digital Entry Pass QR          (`verify_qr`)
2. NIN / 3. passport, by blind index     (`lookup_identity`)
4. registration or fallback reference    (`lookup_reference`)
5. controlled manual search              (`manual_search`)

Every method follows the same rules (FR-ENT-010): the device scope must
list it, the user must hold its permission at this exact checkpoint, the
search is bounded to the checkpoint's own Event Edition, and it is audited.

Privacy guarantees, each covered by a regression test:

* a QR token, NIN, passport number, or search string is never logged,
  audited, stored, or echoed -- audit rows carry only the method, the
  outcome, and counts;
* NIN and passport values are matched ONLY through the versioned HMAC
  blind index (`apps.people.selectors.identifiers_for_value`); no external
  identity service is called (FR-ENT-016);
* a generic physical badge is not a lookup method at all (FR-ENT-015).

These functions never write an Entry Event. They return a
`VerificationOutcome` the operator then acts on.

Phase 3 Prompt 5 (ADR-0022) adds, around every lookup and without changing
any result: the per-operator lookup budget (`apps.entry.services.limits`,
checked after the permission check and before any lookup work), one
identifier-free latency sample (`apps.entry.observability`), and the
anomaly-signal check. The path is indexed end to end and calls no external
provider synchronously: signature verification uses the locally held
verification keys, and identity matching uses the local blind index.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import EntryReasonCode, EntryResult, VerificationMethod
from apps.entry.observability import record_verification, start_timer
from apps.entry.policies import method_is_permitted
from apps.entry.services import EntryPermissionError, audit
from apps.entry.services.access import Assessment, assess_context
from apps.entry.services.limits import enforce_lookup_budget, note_lookup_outcome
from apps.registrations.models import Registration

logger = logging.getLogger(__name__)

#: Credential-verification codes that mean "this token is not a credential
#: we can trust". They collapse into ONE operator-facing result so the
#: presenter cannot distinguish an unknown key from a bad signature.
_UNTRUSTED_TOKEN_CODES = frozenset(
    {
        "MALFORMED",
        "UNKNOWN_KEY",
        "INVALID_KEY",
        "INVALID_SIGNATURE",
        "CREDENTIAL_NOT_FOUND",
        "CREDENTIAL_MISMATCH",
        # A claimed `eid` that disagrees with the stored credential is a
        # forged or corrupted token, not a genuine other-event pass (that
        # case verifies and is then caught by the evaluator as WRONG_EVENT).
        "WRONG_EVENT",
    }
)

IDENTITY_METHODS = (VerificationMethod.NIN, VerificationMethod.PASSPORT)
NIN_COUNTRY_CODE = "DZ"
_MAX_IDENTITY_INPUT = 64
_MAX_REFERENCE_INPUT = 64
_MAX_SEARCH_INPUT = 100
_ISSUING_COUNTRY_LENGTH = 2


def normalize_issuing_country(value: object) -> str:
    """The exact two-letter issuing-country code, or "" when invalid.

    Exactly two ASCII letters after stripping surrounding whitespace, then
    upper-cased. Nothing is truncated: "FRA" is invalid rather than quietly
    becoming "FR" (P8-07). Shared by the lookup service, which must stay
    safe for non-form callers, and by `IdentityLookupForm`, which turns the
    same rule into a localized field error.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip().upper()
    if (
        len(candidate) != _ISSUING_COUNTRY_LENGTH
        or not candidate.isascii()
        or not candidate.isalpha()
    ):
        return ""
    return candidate


@dataclass(frozen=True)
class VerificationOutcome:
    """The result of one lookup, before any operator decision.

    Exactly one of these holds:

    * `assessment` is set -- one context was resolved and evaluated;
    * `candidates` is non-empty -- several contexts match and the operator
      must select the exact one (FR-ENT-004);
    * neither -- nothing was resolved; `result`/`reason_code` explain why
      (INVALID_CREDENTIAL, NO_MATCH, UNSUPPORTED, TECHNICAL_ERROR).
    """

    method: str
    result: str
    reason_code: str
    assessment: Assessment | None = None
    candidates: tuple = ()
    identity_verified: bool | None = None
    truncated: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.assessment is not None


def _require_method(checkpoint, method: str) -> None:
    if not method_is_permitted(checkpoint.user, method, checkpoint):
        with transaction.atomic():
            audit(
                action_code=action_codes.ENTRY_LOOKUP_DENIED,
                actor=checkpoint.user,
                target_type="EntryDeviceSession",
                target_uuid=checkpoint.device_session.pk,
                event_edition_id=checkpoint.event_edition.pk,
                result="DENIED",
                reason_code=method,
                after_summary={
                    "method": method,
                    "gate": checkpoint.gate.code,
                    "gate_id": str(checkpoint.gate.pk),
                },
            )
        raise EntryPermissionError("This lookup method is not permitted at this checkpoint.")


def _begin(checkpoint, method: str) -> float:
    """Permission, then budget, then start the clock (Prompt 5 order)."""
    _require_method(checkpoint, method)
    enforce_lookup_budget(checkpoint=checkpoint, method=method)
    return start_timer()


def _complete(
    checkpoint,
    outcome: VerificationOutcome,
    *,
    action_code: str,
    started: float,
    credential_code: str = "",
    **summary,
) -> VerificationOutcome:
    """Audit the outcome, record its latency sample, check for anomalies."""
    _audit_outcome(checkpoint, outcome, action_code=action_code, **summary)
    record_verification(
        checkpoint=checkpoint,
        method=outcome.method,
        result=outcome.result,
        reason_code=outcome.reason_code,
        started=started,
        credential_code=credential_code,
    )
    note_lookup_outcome(
        checkpoint=checkpoint, method=outcome.method, reason_code=outcome.reason_code
    )
    return outcome


def _audit_outcome(checkpoint, outcome: VerificationOutcome, *, action_code: str, **summary):
    registration = outcome.assessment.registration if outcome.assessment else None
    with transaction.atomic():
        audit(
            action_code=action_code,
            actor=checkpoint.user,
            target_type="Registration" if registration is not None else "EntryDeviceSession",
            target_uuid=registration.pk
            if registration is not None
            else checkpoint.device_session.pk,
            event_edition_id=checkpoint.event_edition.pk,
            result="SUCCESS" if outcome.result != EntryResult.TECHNICAL_ERROR else "FAILURE",
            reason_code=outcome.reason_code,
            after_summary={
                "method": outcome.method,
                "result": outcome.result,
                "reason": outcome.reason_code,
                # The code is a display label only: it is unique within a
                # venue, not within an event. `gate_id` is the isolation key
                # every monitor filter uses (P8-05).
                "gate": checkpoint.gate.code,
                "gate_id": str(checkpoint.gate.pk),
                "zone": checkpoint.zone.code,
                "device": checkpoint.device.public_id,
                **summary,
            },
        )


def _unresolved(method: str, result: str, reason: str, **kwargs) -> VerificationOutcome:
    return VerificationOutcome(method=method, result=result, reason_code=reason, **kwargs)


def _technical_error(method: str) -> VerificationOutcome:
    return _unresolved(method, EntryResult.TECHNICAL_ERROR, EntryReasonCode.TECHNICAL_ERROR)


def _resolved(method, assessment: Assessment, **kwargs) -> VerificationOutcome:
    return VerificationOutcome(
        method=method,
        result=assessment.result,
        reason_code=assessment.reason_code,
        assessment=assessment,
        **kwargs,
    )


def eligible_contexts_queryset(event_edition):
    """Registration Contexts an entry lookup may surface: this edition's
    approved, current, non-withdrawn, non-cancelled contexts only -- the
    shared definition in `apps.registrations.selectors` (P8-02/P8-06)."""
    from apps.registrations.selectors import active_approved_context_q

    return (
        Registration.objects.select_related("person", "event_edition")
        .filter(event_edition=event_edition)
        .filter(active_approved_context_q())
    )


def _from_contexts(checkpoint, method, contexts, *, identity_verified=None, now=None):
    contexts = list(contexts)
    if not contexts:
        return _unresolved(method, EntryResult.DENIED, EntryReasonCode.NO_MATCH)
    if len(contexts) > 1:
        return VerificationOutcome(
            method=method,
            result=EntryResult.MANUAL_REVIEW,
            reason_code=EntryReasonCode.NONE,
            candidates=tuple(contexts),
            identity_verified=identity_verified,
        )
    assessment = assess_context(
        registration=contexts[0],
        checkpoint=checkpoint,
        identity_verified=identity_verified,
        now=now,
    )
    return _resolved(method, assessment, identity_verified=identity_verified)


# ---------------------------------------------------------------------------
# 1. QR
# ---------------------------------------------------------------------------


def verify_qr(*, checkpoint, raw_token: object, now=None) -> VerificationOutcome:
    """Verify a scanned QR online: signature, payload version, event,
    validity, pass status, credential version, registration status,
    access rules, and device scope (Prompt 4)."""
    from apps.badges.credentials.verify import VerificationResultCode, verify_pass_token

    method = VerificationMethod.QR
    started = _begin(checkpoint, method)
    now = now or timezone.now()
    credential_code = ""
    try:
        result = verify_pass_token(raw_token, now=now)
        credential_code = str(result.code or "")
        if result.code == VerificationResultCode.UNSUPPORTED_VERSION:
            outcome = _unresolved(
                method, EntryResult.UNSUPPORTED, EntryReasonCode.UNSUPPORTED_CREDENTIAL
            )
        elif result.code in _UNTRUSTED_TOKEN_CODES or result.credential is None:
            outcome = _unresolved(method, EntryResult.DENIED, EntryReasonCode.INVALID_CREDENTIAL)
        else:
            credential = result.credential
            assessment = assess_context(
                registration=credential.registration,
                checkpoint=checkpoint,
                presented_credential=credential,
                now=now,
            )
            outcome = _resolved(method, assessment)
    except Exception:  # noqa: BLE001 - never a false result; never the token
        logger.exception("Entry QR verification failed with a technical error.")
        outcome = _technical_error(method)
    return _complete(
        checkpoint,
        outcome,
        action_code=action_codes.ENTRY_VERIFICATION_PERFORMED,
        started=started,
        credential_code=credential_code,
    )


# ---------------------------------------------------------------------------
# 2-3. NIN / passport (blind index only)
# ---------------------------------------------------------------------------


def lookup_identity(
    *, checkpoint, method: str, raw_value: object, country_code: object = None, now=None
) -> VerificationOutcome:
    from apps.people.models import IdentifierStatus, IdentifierType
    from apps.people.selectors import identifiers_for_value

    if method not in IDENTITY_METHODS:
        raise ValueError("Not an identity lookup method.")
    started = _begin(checkpoint, method)
    # From here on, permission was checked and the lookup budget charged, so
    # EVERY outcome -- including an input refused before any matching --
    # leaves the same evidence through `_complete` exactly once: one audit
    # row, one latency sample, one anomaly check (P8-07). The rejected input
    # itself is never recorded; only a fixed marker saying which check
    # refused it.
    input_check = ""
    if not isinstance(raw_value, str) or not raw_value.strip():
        input_check = "EMPTY_VALUE"
    else:
        raw_value = raw_value[:_MAX_IDENTITY_INPUT]
    if method == VerificationMethod.NIN:
        identifier_type, country = IdentifierType.NIN, NIN_COUNTRY_CODE
    else:
        identifier_type = IdentifierType.PASSPORT
        country = normalize_issuing_country(country_code)
        if not country and not input_check:
            input_check = "INVALID_COUNTRY"
    if input_check:
        return _complete(
            checkpoint,
            _unresolved(method, EntryResult.DENIED, EntryReasonCode.NO_MATCH),
            action_code=action_codes.ENTRY_IDENTITY_LOOKUP,
            started=started,
            matches=0,
            input_check=input_check,
        )
    try:
        identifiers = list(
            identifiers_for_value(
                identifier_type=identifier_type, country_code_id=country, raw_value=raw_value
            )
            .filter(status__in=[IdentifierStatus.VERIFIED, IdentifierStatus.DECLARED])
            .only("person_id", "status")
        )
        verified_person_ids = {i.person_id for i in identifiers if i.status == "VERIFIED"}
        person_ids = {i.person_id for i in identifiers}
        contexts = eligible_contexts_queryset(checkpoint.event_edition).filter(
            person_id__in=person_ids
        )
        contexts = list(contexts.order_by("public_reference"))
        # A context is "identity verified" only when the matching identifier
        # of ITS OWN person is verified.
        identity_verified = (
            all(c.person_id in verified_person_ids for c in contexts) if contexts else None
        )
        outcome = _from_contexts(
            checkpoint, method, contexts, identity_verified=identity_verified, now=now
        )
        if outcome.candidates:
            # Per-candidate verification state, so selecting one context of
            # several never inherits another person's verified identifier.
            outcome = replace(
                outcome,
                extra={
                    "verified_registration_ids": {
                        str(c.pk) for c in contexts if c.person_id in verified_person_ids
                    }
                },
            )
    except Exception:  # noqa: BLE001 - never a false result; never the value
        logger.exception("Entry identity lookup failed with a technical error.")
        outcome = _technical_error(method)
    return _complete(
        checkpoint,
        outcome,
        action_code=action_codes.ENTRY_IDENTITY_LOOKUP,
        started=started,
        matches=len(outcome.candidates) or (1 if outcome.resolved else 0),
    )


# ---------------------------------------------------------------------------
# 4. Registration reference or pass fallback reference
# ---------------------------------------------------------------------------


def lookup_reference(*, checkpoint, raw_reference: object, now=None) -> VerificationOutcome:
    from apps.badges.models import PassCredentialSeries
    from apps.badges.references import InvalidFallbackReferenceError, parse_fallback_reference

    method = VerificationMethod.REFERENCE
    started = _begin(checkpoint, method)
    if not isinstance(raw_reference, str) or not raw_reference.strip():
        # Charged and authorized already: completed like any other outcome
        # (P8-07), never returned silently. The input is not recorded.
        return _complete(
            checkpoint,
            _unresolved(method, EntryResult.DENIED, EntryReasonCode.NO_MATCH),
            action_code=action_codes.ENTRY_REFERENCE_LOOKUP,
            started=started,
            input_check="EMPTY_REFERENCE",
        )
    raw_reference = raw_reference[:_MAX_REFERENCE_INPUT]
    try:
        base = eligible_contexts_queryset(checkpoint.event_edition)
        normalized = "".join(raw_reference.split()).upper()
        contexts = list(base.filter(public_reference__iexact=normalized)[:1])
        if not contexts:
            try:
                fallback = parse_fallback_reference(raw_reference)
            except InvalidFallbackReferenceError:
                fallback = None
            if fallback is not None:
                registration_id = (
                    PassCredentialSeries.objects.filter(fallback_reference=fallback)
                    .values_list("registration_id", flat=True)
                    .first()
                )
                if registration_id is not None:
                    contexts = list(base.filter(pk=registration_id))
        outcome = _from_contexts(checkpoint, method, contexts, now=now)
    except Exception:  # noqa: BLE001
        logger.exception("Entry reference lookup failed with a technical error.")
        outcome = _technical_error(method)
    return _complete(
        checkpoint, outcome, action_code=action_codes.ENTRY_REFERENCE_LOOKUP, started=started
    )


# ---------------------------------------------------------------------------
# 5. Controlled manual search
# ---------------------------------------------------------------------------


def manual_search(*, checkpoint, raw_query: object) -> VerificationOutcome:
    """Bounded name search inside the checkpoint's Event Edition.

    Returns candidates only -- never an automatic single-result assessment
    -- so the operator always consciously selects the exact context. The
    query itself is never persisted; the audit row records its length and
    the match count only.
    """
    method = VerificationMethod.MANUAL
    started = _begin(checkpoint, method)
    query = " ".join(raw_query.split())[:_MAX_SEARCH_INPUT] if isinstance(raw_query, str) else ""
    limit = settings.ENTRY_MANUAL_SEARCH_MAX_RESULTS
    if len(query) < settings.ENTRY_MANUAL_SEARCH_MIN_CHARS:
        outcome = _unresolved(method, EntryResult.DENIED, EntryReasonCode.NO_MATCH)
        return _complete(
            checkpoint,
            outcome,
            action_code=action_codes.ENTRY_MANUAL_SEARCH,
            started=started,
            query_length=len(query),
            matches=0,
        )
    try:
        matches = list(
            eligible_contexts_queryset(checkpoint.event_edition)
            .filter(
                Q(person__display_name__icontains=query)
                | Q(profile__submitted_full_name__icontains=query)
            )
            .order_by("person__display_name", "public_reference")[: limit + 1]
        )
        truncated = len(matches) > limit
        matches = matches[:limit]
        if matches:
            outcome = VerificationOutcome(
                method=method,
                result=EntryResult.MANUAL_REVIEW,
                reason_code=EntryReasonCode.NONE,
                candidates=tuple(matches),
                truncated=truncated,
            )
        else:
            outcome = _unresolved(method, EntryResult.DENIED, EntryReasonCode.NO_MATCH)
    except Exception:  # noqa: BLE001
        logger.exception("Entry manual search failed with a technical error.")
        outcome = _technical_error(method)
    return _complete(
        checkpoint,
        outcome,
        action_code=action_codes.ENTRY_MANUAL_SEARCH,
        started=started,
        query_length=len(query),
        matches=len(outcome.candidates),
        truncated=outcome.truncated,
    )


# ---------------------------------------------------------------------------
# Selecting one of several candidates
# ---------------------------------------------------------------------------


def assess_selected_candidate(
    *, checkpoint, method: str, registration_id, identity_verified=None, now=None
) -> VerificationOutcome:
    """Evaluate the exact context the operator selected from a candidate
    list. `registration_id` must come from the server-held candidate list,
    never from the client; it is still re-bounded to this edition here.

    Selecting from a list the operator already obtained is not a new lookup,
    so it is not charged against the lookup budget; it is still measured."""
    _require_method(checkpoint, method)
    started = start_timer()
    context = (
        eligible_contexts_queryset(checkpoint.event_edition).filter(pk=registration_id).first()
    )
    if context is None:
        outcome = _unresolved(method, EntryResult.DENIED, EntryReasonCode.NO_MATCH)
    else:
        assessment = assess_context(
            registration=context,
            checkpoint=checkpoint,
            identity_verified=identity_verified,
            now=now,
        )
        outcome = _resolved(method, assessment, identity_verified=identity_verified)
    _audit_outcome(checkpoint, outcome, action_code=action_codes.ENTRY_VERIFICATION_PERFORMED)
    record_verification(
        checkpoint=checkpoint,
        method=outcome.method,
        result=outcome.result,
        reason_code=outcome.reason_code,
        started=started,
    )
    return outcome
