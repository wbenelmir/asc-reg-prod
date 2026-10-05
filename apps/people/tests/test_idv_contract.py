"""The ministry lookup contract and the conservative comparison (IDV-1, A-13).

Pure functions, synthetic values only (every NIN, name and date is invented).
Covers the owner's acceptance list: exact match, case/whitespace-only
normalization, substantive mismatches, wrong returned NIN, leading zeros,
explicit null, malformed shapes, the `presume` flag in every encoding, and
the presumed and non-presumed date rules.
"""

from __future__ import annotations

import datetime

import pytest

from apps.people.identity_contract import (
    PRESUMED_DATE_POLICY_BASIS,
    Comparison,
    LookupKind,
    MatchReason,
    OfficialIdentityFacts,
    PresumeFlag,
    classify_lookup_body,
    compare_name,
    evaluate_identity_match,
    normalize_name_for_comparison,
    parse_official_birth_date,
    parse_presume,
)

NIN = "990000000000000010"
BIRTH = datetime.date(1990, 3, 7)


def _identity(**overrides) -> dict:
    identity = {
        "nin": NIN,
        "nom_f": "BENTEST",
        "pren_f": "AMINA",
        "d_nais": "07/03/1990",
        "presume": "False",
    }
    identity.update(overrides)
    return identity


def _facts(**overrides) -> OfficialIdentityFacts:
    classification = classify_lookup_body({"identite": _identity(**overrides)})
    assert classification.kind == LookupKind.FOUND, classification.detail
    return classification.facts


def _evaluate(facts, *, given="Amina", family="Bentest", birth=BIRTH, nin=NIN):
    return evaluate_identity_match(
        requested_nin=nin,
        submitted_given_names=given,
        submitted_family_name=family,
        submitted_birth_date=birth,
        facts=facts,
    )


# ---------------------------------------------------------------------------
# presume (A13-09): explicit parsing, never truthiness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, PresumeFlag.TRUE),
        (False, PresumeFlag.FALSE),
        ("True", PresumeFlag.TRUE),
        ("False", PresumeFlag.FALSE),
        ("  true ", PresumeFlag.TRUE),
        ("FALSE", PresumeFlag.FALSE),
        ("\tFalse\n", PresumeFlag.FALSE),
        (None, PresumeFlag.MISSING),
        ("", PresumeFlag.UNRECOGNIZED),
        ("yes", PresumeFlag.UNRECOGNIZED),
        ("0", PresumeFlag.UNRECOGNIZED),
        (0, PresumeFlag.UNRECOGNIZED),
        (1, PresumeFlag.UNRECOGNIZED),
        ([], PresumeFlag.UNRECOGNIZED),
        ("Faux", PresumeFlag.UNRECOGNIZED),
    ],
)
def test_presume_is_parsed_explicitly(raw, expected) -> None:
    assert parse_presume(raw, present=True) == expected


def test_the_string_false_is_never_true() -> None:
    assert parse_presume("False", present=True) == PresumeFlag.FALSE
    assert bool("False") is True  # the trap that explicit parsing avoids


def test_an_absent_presume_key_is_missing() -> None:
    assert parse_presume(None, present=False) == PresumeFlag.MISSING
    assert parse_presume("True", present=False) == PresumeFlag.MISSING


# ---------------------------------------------------------------------------
# Official date parsing (A13-13): DD/MM/YYYY only, never guessed
# ---------------------------------------------------------------------------


def test_the_observed_format_parses() -> None:
    parsed = parse_official_birth_date("07/03/1990")
    assert parsed.outcome == "parsed" and parsed.value == BIRTH


@pytest.mark.parametrize(
    "text",
    [
        "1990-03-07",  # ISO is unambiguous, but not observed in the contract
        "7/3/1990",  # single digits: not the observed shape
        "07-03-1990",
        "07/03/90",
        " 07/03/1990",
        "07/03/1990 ",
        "03/1990",  # partial
        "1990",
        "",
        "٠٧/٠٣/١٩٩٠",  # Arabic-Indic digits are not the observed ASCII form
    ],
)
def test_other_formats_are_unrecognized_never_guessed(text: str) -> None:
    assert parse_official_birth_date(text).outcome == Comparison.UNRECOGNIZED_FORMAT


@pytest.mark.parametrize("text", ["31/02/1990", "00/03/1990", "07/13/1990", "29/02/1991"])
def test_impossible_dates_are_refused(text: str) -> None:
    assert parse_official_birth_date(text).outcome == Comparison.IMPOSSIBLE_DATE


def test_a_leap_day_is_a_real_date() -> None:
    assert parse_official_birth_date("29/02/1992").value == datetime.date(1992, 2, 29)


def test_an_ambiguous_order_is_read_only_as_documented() -> None:
    """03/04/1990 is 3 April under the documented DD/MM order, never 4 March."""
    assert parse_official_birth_date("03/04/1990").value == datetime.date(1990, 4, 3)


# ---------------------------------------------------------------------------
# Names (A13-04): case and whitespace only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("submitted", "official", "expected"),
    [
        ("Amina", "Amina", Comparison.MATCH),
        ("Amina", "AMINA", Comparison.NORMALIZED_MATCH),
        ("amina leila", "AMINA  LEILA", Comparison.NORMALIZED_MATCH),
        (" Amina ", "AMINA", Comparison.NORMALIZED_MATCH),
        ("Zoé", "ZOE", Comparison.MISMATCH),  # an accent is substantive
        ("Zoé", "ZOÉ", Comparison.NORMALIZED_MATCH),
        ("Mohamed", "MOHAMMED", Comparison.MISMATCH),  # transliteration
        ("Amina Leila", "LEILA AMINA", Comparison.MISMATCH),  # reordered tokens
        ("Ben Ali", "BENALI", Comparison.MISMATCH),
        ("O'Neil", "ONEIL", Comparison.MISMATCH),
        ("Amina", "", Comparison.MISMATCH),
        ("", "AMINA", Comparison.MISMATCH),
    ],
)
def test_name_comparison(submitted: str, official: str, expected: str) -> None:
    assert compare_name(submitted, official) == expected


def test_canonically_equivalent_encodings_match_exactly() -> None:
    decomposed = "Zoé"
    precomposed = "Zoé"
    assert compare_name(decomposed, precomposed) == Comparison.MATCH


def test_normalization_never_removes_accents() -> None:
    assert normalize_name_for_comparison("Zoé") != normalize_name_for_comparison("Zoe")


# ---------------------------------------------------------------------------
# Response classification (addendum §3, A13-10)
# ---------------------------------------------------------------------------


def test_explicit_null_is_not_found() -> None:
    assert classify_lookup_body({"identite": None}).kind == LookupKind.NOT_FOUND


def test_explanatory_text_beside_null_is_ignored() -> None:
    body = {"identite": None, "result": "Synthetic text that mentions a birth abroad."}
    classification = classify_lookup_body(body)
    assert classification.kind == LookupKind.NOT_FOUND
    assert classification.facts is None


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({}, "identite_missing"),
        ({"result": "something"}, "identite_missing"),
        ([], "body_not_object"),
        ("identite", "body_not_object"),
        (None, "body_not_object"),
        ({"identite": "null"}, "identite_wrong_type"),
        ({"identite": []}, "identite_wrong_type"),
        ({"identite": 0}, "identite_wrong_type"),
    ],
)
def test_missing_or_wrongly_shaped_identity_is_invalid(body, detail) -> None:
    classification = classify_lookup_body(body)
    assert classification.kind == LookupKind.INVALID_RESPONSE
    assert classification.detail == detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"nin": 990000000000000010},  # a number would already have lost leading zeros
        {"nin": "99000000000000001"},  # 17 digits
        {"nin": " 990000000000000010"},
        {"nin": None},
    ],
)
def test_an_unusable_returned_nin_is_invalid(overrides) -> None:
    body = {"identite": _identity(**overrides)}
    assert classify_lookup_body(body).detail == "nin_invalid"


def test_missing_names_are_invalid() -> None:
    for overrides in ({"nom_f": ""}, {"pren_f": None}, {"nom_f": 12}, {"pren_f": "   "}):
        assert classify_lookup_body({"identite": _identity(**overrides)}).kind == (
            LookupKind.INVALID_RESPONSE
        )
    identity = _identity()
    del identity["pren_f"]
    assert classify_lookup_body({"identite": identity}).kind == LookupKind.INVALID_RESPONSE


def test_presume_false_requires_a_string_birth_date() -> None:
    assert classify_lookup_body({"identite": _identity(d_nais=None)}).detail == (
        "birth_date_missing"
    )
    assert classify_lookup_body({"identite": _identity(d_nais="")}).detail == "birth_date_missing"
    assert classify_lookup_body({"identite": _identity(d_nais=19900307)}).detail == (
        "birth_date_wrong_type"
    )
    identity = _identity()
    del identity["d_nais"]
    assert classify_lookup_body({"identite": identity}).detail == "birth_date_missing"


def test_presume_true_does_not_require_or_read_the_birth_date() -> None:
    for value in (None, "", "not a date", 19900307):
        facts = _facts(presume="True", d_nais=value)
        assert facts.presume == PresumeFlag.TRUE
        assert facts.birth_date_text is None
    identity = _identity(presume=True)
    del identity["d_nais"]
    assert classify_lookup_body({"identite": identity}).kind == LookupKind.FOUND


def test_unrelated_official_fields_are_never_read() -> None:
    body = {
        "identite": {
            **_identity(),
            "nom_a": "synthetic-arabic-family",
            "pren_a": "synthetic-arabic-given",
            "nom_pere": "synthetic-parent",
            "date_deces": "01/01/2000",
        }
    }
    facts = classify_lookup_body(body).facts
    assert set(vars(facts)) == {
        "nin",
        "family_name_latin",
        "given_names_latin",
        "presume",
        "birth_date_text",
    }


def test_leading_zeros_are_kept_as_a_string() -> None:
    facts = _facts(nin="009900000000000044")
    assert facts.nin == "009900000000000044"


# ---------------------------------------------------------------------------
# Matching (IDV-02, A13-04, A13-08, A13-13)
# ---------------------------------------------------------------------------


def test_exact_match_verifies() -> None:
    result = _evaluate(_facts(nom_f="Bentest", pren_f="Amina"))
    assert result.verified and result.reason_code == ""
    assert result.comparison == {
        "nin": Comparison.MATCH,
        "family_name": Comparison.MATCH,
        "given_names": Comparison.MATCH,
        "birth_date": Comparison.MATCH,
    }
    assert result.retained_birth_date == BIRTH


def test_case_and_whitespace_only_differences_verify_without_rewriting() -> None:
    result = _evaluate(_facts(), given="amina", family="  bentest ")
    assert result.verified
    assert result.comparison["family_name"] == Comparison.NORMALIZED_MATCH
    assert result.comparison["given_names"] == Comparison.NORMALIZED_MATCH
    # The official form is retained for provenance; nothing is rewritten here.
    assert result.retained_family_name_latin == "BENTEST"


@pytest.mark.parametrize(
    ("given", "family"),
    [("Amina", "Bentesti"), ("Amyna", "Bentest"), ("Bentest", "Amina"), ("Amina Leila", "Bentest")],
)
def test_a_substantive_name_difference_goes_to_manual_review(given, family) -> None:
    result = _evaluate(_facts(), given=given, family=family)
    assert not result.verified
    assert result.reason_code == MatchReason.DATA_MISMATCH


def test_a_different_returned_nin_never_verifies_and_retains_nothing() -> None:
    facts = _facts(nin="990000000000000060")
    result = _evaluate(facts)
    assert not result.verified
    assert result.reason_code == MatchReason.PROVIDER_IDENTITY_MISMATCH
    assert result.retained_family_name_latin == ""
    assert result.retained_given_names_latin == ""
    assert result.retained_birth_date is None


def test_leading_zero_nin_matches_only_exactly() -> None:
    facts = _facts(nin="009900000000000044", nom_f="ZEROTEST", pren_f="LINA", d_nais="20/11/1988")
    ok = _evaluate(
        facts,
        nin="009900000000000044",
        given="Lina",
        family="Zerotest",
        birth=datetime.date(1988, 11, 20),
    )
    assert ok.verified
    stripped = _evaluate(
        facts,
        nin="99000000000000044",
        given="Lina",
        family="Zerotest",
        birth=ok.retained_birth_date,
    )
    assert stripped.reason_code == MatchReason.PROVIDER_IDENTITY_MISMATCH


@pytest.mark.parametrize("official_date", [None, "", "not-a-date", "31/02/1990", "01/01/1985"])
@pytest.mark.parametrize("flag", [True, "True", " true "])
def test_presumed_true_ignores_the_official_date_and_keeps_the_entered_one(
    flag, official_date
) -> None:
    result = _evaluate(_facts(presume=flag, d_nais=official_date))
    assert result.verified
    assert result.comparison["birth_date"] == Comparison.SKIPPED_PRESUMED
    assert result.presume == PresumeFlag.TRUE
    assert result.policy_basis == PRESUMED_DATE_POLICY_BASIS
    # The unused official date is never retained (A13-03).
    assert result.retained_birth_date is None
    assert result.retained_birth_date_text == ""


def test_presumed_true_still_requires_matching_names() -> None:
    result = _evaluate(_facts(presume="True"), family="Other")
    assert not result.verified and result.reason_code == MatchReason.DATA_MISMATCH


def test_presumed_false_with_an_equal_date_verifies() -> None:
    assert _evaluate(_facts(presume=False)).verified


def test_presumed_false_with_a_different_date_goes_to_manual_review() -> None:
    result = _evaluate(_facts(d_nais="08/03/1990"))
    assert not result.verified
    assert result.reason_code == MatchReason.DATA_MISMATCH
    assert result.comparison["birth_date"] == Comparison.MISMATCH
    assert result.retained_birth_date == datetime.date(1990, 3, 8)


@pytest.mark.parametrize(
    ("official_date", "outcome"),
    [
        ("1990-03-07", Comparison.UNRECOGNIZED_FORMAT),
        ("7/3/1990", Comparison.UNRECOGNIZED_FORMAT),
        ("31/02/1990", Comparison.IMPOSSIBLE_DATE),
    ],
)
def test_presumed_false_with_an_unreadable_date_goes_to_manual_review(
    official_date, outcome
) -> None:
    result = _evaluate(_facts(d_nais=official_date))
    assert not result.verified
    assert result.reason_code == MatchReason.AMBIGUOUS_DATE
    assert result.comparison["birth_date"] == outcome
    assert result.retained_birth_date is None
    assert result.retained_birth_date_text == official_date  # restricted, for the reviewer


def test_the_entered_date_is_never_changed_to_resolve_a_mismatch() -> None:
    submitted = datetime.date(1990, 3, 8)
    result = _evaluate(_facts(), birth=submitted)
    assert not result.verified
    assert submitted == datetime.date(1990, 3, 8)


@pytest.mark.parametrize("flag", ["yes", 1, 0, "", ["False"]])
def test_an_unrecognized_presume_flag_goes_to_manual_review(flag) -> None:
    result = _evaluate(_facts(presume=flag))
    assert not result.verified
    assert result.reason_code == MatchReason.PRESUME_FLAG_UNRECOGNIZED
    assert result.comparison["birth_date"] == Comparison.NOT_COMPARED


def test_an_absent_presume_flag_goes_to_manual_review() -> None:
    identity = _identity()
    del identity["presume"]
    facts = classify_lookup_body({"identite": identity}).facts
    result = _evaluate(facts)
    assert not result.verified
    assert result.reason_code == MatchReason.PRESUME_FLAG_UNRECOGNIZED


def test_existence_alone_never_verifies() -> None:
    """A found identity with entirely different names does not verify."""
    result = _evaluate(_facts(nom_f="OTHERTEST", pren_f="NADIA"))
    assert not result.verified
