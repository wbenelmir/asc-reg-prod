"""Participant fallback reference: format, check symbol, and controlled lookup."""

from __future__ import annotations

import pytest

from apps.badges import references
from apps.badges.services import (
    FallbackLookupThrottled,
    lookup_series_by_fallback_reference,
)

# ---------------------------------------------------------------------------
# Format (no database)
# ---------------------------------------------------------------------------


def test_generated_reference_has_the_documented_shape():
    value = references.generate_fallback_reference()
    assert len(value) == references.NORMALIZED_LENGTH == 9
    assert len(value[:-1]) == references.PAYLOAD_CHARACTERS == 8
    assert all(character in references.PAYLOAD_ALPHABET for character in value[:-1])
    assert value[-1] in references.CHECK_ALPHABET


def test_payload_alphabet_excludes_the_ambiguous_letters():
    for letter in ("I", "L", "O", "U"):
        assert letter not in references.PAYLOAD_ALPHABET


def test_display_form_has_the_documented_length():
    value = references.generate_fallback_reference()
    displayed = references.format_fallback_reference(value)
    assert displayed.startswith("ASC-")
    assert len(displayed) == references.DISPLAY_LENGTH == 15
    assert displayed.count("-") == 3


def test_generated_references_validate():
    for _ in range(200):
        value = references.generate_fallback_reference()
        assert references.validate_fallback_reference(value) == value


def test_display_form_round_trips_through_normalization():
    value = references.generate_fallback_reference()
    displayed = references.format_fallback_reference(value)
    assert references.parse_fallback_reference(displayed) == value


@pytest.mark.parametrize(
    "variant",
    [
        lambda v: references.format_fallback_reference(v).lower(),
        lambda v: references.format_fallback_reference(v).replace("-", ""),
        lambda v: references.format_fallback_reference(v).replace("-", " "),
        lambda v: f"  {references.format_fallback_reference(v)}  ",
        lambda v: v,
    ],
)
def test_normalization_accepts_the_documented_input_variants(variant):
    value = references.generate_fallback_reference()
    assert references.parse_fallback_reference(variant(value)) == value


def test_crockford_input_aliasing_is_applied_to_the_payload():
    """`I`/`L` read as `1` and `O` reads as `0` -- a typing fix, not a value."""
    value = "1" + references.generate_fallback_reference()[1:]
    value = references.validate_fallback_reference(
        value[:-1] + references._check_symbol(references._decode_payload(value[:-1]))
    )
    typed = "I" + value[1:]
    assert references.parse_fallback_reference(typed) == value


def test_a_single_character_typo_is_caught_by_the_check_symbol():
    value = references.generate_fallback_reference()
    payload = value[:-1]
    swapped_index = next(
        index for index, character in enumerate(payload) if character not in ("I", "L", "O")
    )
    replacement = "2" if payload[swapped_index] != "2" else "3"
    corrupted = payload[:swapped_index] + replacement + payload[swapped_index + 1 :] + value[-1]
    with pytest.raises(references.InvalidFallbackReferenceError):
        references.validate_fallback_reference(corrupted)


@pytest.mark.parametrize(
    "value",
    ["", "ASC", "ASC-1234", "A" * 100, 12345, None, "ASC-!!!!-!!!!-!"],
)
def test_malformed_references_are_rejected(value):
    with pytest.raises(references.InvalidFallbackReferenceError):
        references.parse_fallback_reference(value)


def test_masking_never_leaks_a_usable_lookup_term():
    value = references.generate_fallback_reference()
    masked = references.mask_fallback_reference(value)
    assert masked.startswith("ASC-")
    assert "*" in masked
    assert value[4:] not in masked


# ---------------------------------------------------------------------------
# Controlled lookup (database)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lookup_resolves_an_exact_reference(eligible_registration, pass_admin, active_key):
    from apps.badges.services import generate_pass, new_operation_id

    credential = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=new_operation_id()
    ).credential
    series = credential.series

    found = lookup_series_by_fallback_reference(
        raw_reference=references.format_fallback_reference(series.fallback_reference),
        actor=pass_admin,
    )
    assert found is not None
    assert found.pk == series.pk


@pytest.mark.django_db
def test_lookup_is_exact_match_only(eligible_registration, pass_admin, active_key):
    """No prefix, partial, wildcard, or empty-term search exists."""
    from apps.badges.services import generate_pass, new_operation_id

    credential = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=new_operation_id()
    ).credential
    reference = credential.series.fallback_reference

    for partial in (reference[:4], reference[:-1], f"{reference[:4]}%", ""):
        assert lookup_series_by_fallback_reference(raw_reference=partial, actor=pass_admin) is None


@pytest.mark.django_db
def test_every_lookup_is_audited_with_a_masked_reference(
    eligible_registration, pass_admin, active_key
):
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent
    from apps.badges.services import generate_pass, new_operation_id

    credential = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=new_operation_id()
    ).credential
    reference = credential.series.fallback_reference
    lookup_series_by_fallback_reference(raw_reference=reference, actor=pass_admin)

    entry = AuditEvent.objects.filter(action_code=action_codes.FALLBACK_REFERENCE_LOOKUP).latest(
        "occurred_at"
    )
    assert entry.result == "SUCCESS"
    assert "*" in entry.after_summary["reference"]
    assert reference not in entry.after_summary["reference"]


@pytest.mark.django_db
def test_a_failed_lookup_is_also_audited(eligible_registration, pass_admin, active_key):
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent

    lookup_series_by_fallback_reference(raw_reference="not-a-reference", actor=pass_admin)
    entry = AuditEvent.objects.filter(action_code=action_codes.FALLBACK_REFERENCE_LOOKUP).latest(
        "occurred_at"
    )
    assert entry.result == "FAILURE"
    assert entry.reason_code == "INVALID_REFERENCE"


@pytest.mark.django_db
def test_lookup_is_rate_limited(pass_admin, settings):
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 3
    valid = references.generate_fallback_reference()
    for _ in range(3):
        lookup_series_by_fallback_reference(raw_reference=valid, actor=pass_admin)
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(raw_reference=valid, actor=pass_admin)


@pytest.mark.django_db
def test_the_reference_is_stable_across_replacement(eligible_registration, pass_admin, active_key):
    from apps.badges.models import PassReasonCode
    from apps.badges.services import (
        activate_pass,
        generate_pass,
        new_operation_id,
        replace_pass,
    )

    credential = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=new_operation_id()
    ).credential
    reference = credential.series.fallback_reference
    activate_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    replace_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_jti=credential.jti,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )

    found = lookup_series_by_fallback_reference(raw_reference=reference, actor=pass_admin)
    assert found is not None
    assert found.fallback_reference == reference


@pytest.mark.django_db
def test_a_reference_match_grants_nothing_by_itself(eligible_registration, pass_admin, active_key):
    """A lookup returns a series; it never admits and writes no entry event."""
    from apps.badges.services import generate_pass, new_operation_id

    credential = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=new_operation_id()
    ).credential
    found = lookup_series_by_fallback_reference(
        raw_reference=credential.series.fallback_reference, actor=pass_admin
    )
    assert found.__class__.__name__ == "PassCredentialSeries"
    # The credential itself is still INACTIVE; nothing about the lookup
    # changed its state or produced an admission decision.
    credential.refresh_from_db()
    assert credential.status == "INACTIVE"
