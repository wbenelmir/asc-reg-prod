"""Phase 3 Prompt 8 (P8-07): every post-authorization lookup outcome is completed.

`_begin()` checks the method permission and charges the lookup budget.
Before this correction several early returns that followed it -- an empty
identity value, a malformed passport issuing country, an empty reference --
returned without `_complete()`, so they left no outcome audit, no latency
sample and no anomaly accounting (FR-ENT-010: every lookup method follows
the same audit rules). All values are synthetic; none may reach the audit
trail.
"""

from __future__ import annotations

import json

import pytest
from django.utils import translation

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry.forms import IdentityLookupForm
from apps.entry.models import EntryReasonCode, EntryResult, VerificationMethod, VerificationSample
from apps.entry.services.limits import EntryLookupThrottled
from apps.entry.services.verification import (
    lookup_identity,
    lookup_reference,
    normalize_issuing_country,
)

pytestmark = pytest.mark.django_db

SYNTHETIC_PASSPORT = "SYNTHPASS778899"

IDENTITY_EARLY_RETURNS = [
    pytest.param(VerificationMethod.NIN, "", None, "EMPTY_VALUE", id="nin-empty"),
    pytest.param(VerificationMethod.NIN, "    ", None, "EMPTY_VALUE", id="nin-whitespace"),
    pytest.param(VerificationMethod.NIN, None, None, "EMPTY_VALUE", id="nin-not-text"),
    pytest.param(VerificationMethod.PASSPORT, "", "FR", "EMPTY_VALUE", id="passport-empty"),
    pytest.param(VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, "F", "INVALID_COUNTRY", id="c1"),
    pytest.param(
        VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, "FRA", "INVALID_COUNTRY", id="c3"
    ),
    pytest.param(VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, "1A", "INVALID_COUNTRY", id="cd"),
    pytest.param(VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, "", "INVALID_COUNTRY", id="c0"),
    pytest.param(
        VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, None, "INVALID_COUNTRY", id="cnone"
    ),
    pytest.param(
        VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, " f ", "INVALID_COUNTRY", id="c-pad"
    ),
    pytest.param(
        VerificationMethod.PASSPORT, SYNTHETIC_PASSPORT, "É1", "INVALID_COUNTRY", id="c-nonascii"
    ),
]

REFERENCE_EARLY_RETURNS = [
    pytest.param("", id="empty"),
    pytest.param("   \t ", id="whitespace"),
    pytest.param(None, id="not-text"),
]


def _audit_rows(checkpoint, action_code):
    return AuditEvent.objects.filter(actor_user_id=checkpoint.user.pk, action_code=action_code)


def _assert_no_raw_value(row, *values):
    """No submitted identity/reference value appears anywhere in the row.

    Values shorter than three characters (a one- or two-letter country
    attempt) are not meaningful to search for: they occur by chance inside
    random identifiers. The identity value itself is always checked.
    """
    rendered = json.dumps(row.after_summary, default=str) + str(row.reason_code)
    for value in values:
        if value and len(value.strip()) >= 3:
            leaked = value.strip() in rendered
            assert not leaked, "a raw lookup value reached the audit trail"


@pytest.mark.parametrize(("method", "value", "country", "input_check"), IDENTITY_EARLY_RETURNS)
def test_an_early_identity_outcome_is_audited_measured_and_safe(
    checkpoint, method, value, country, input_check
):
    audits_before = _audit_rows(checkpoint, action_codes.ENTRY_IDENTITY_LOOKUP).count()
    samples_before = VerificationSample.objects.count()

    outcome = lookup_identity(
        checkpoint=checkpoint, method=method, raw_value=value, country_code=country
    )

    # The same safe public result as before.
    assert outcome.result == EntryResult.DENIED
    assert outcome.reason_code == EntryReasonCode.NO_MATCH
    assert outcome.assessment is None and not outcome.candidates
    # Exactly one outcome audit -- never zero, never two.
    rows = _audit_rows(checkpoint, action_codes.ENTRY_IDENTITY_LOOKUP)
    assert rows.count() == audits_before + 1
    row = rows.order_by("-occurred_at").first()
    assert row.after_summary["method"] == method
    assert row.after_summary["result"] == EntryResult.DENIED
    assert row.after_summary["reason"] == EntryReasonCode.NO_MATCH
    assert row.after_summary["matches"] == 0
    assert row.after_summary["input_check"] == input_check
    _assert_no_raw_value(row, SYNTHETIC_PASSPORT, value or "", country or "")
    # Exactly one latency sample.
    assert VerificationSample.objects.count() == samples_before + 1
    sample = VerificationSample.objects.order_by("-occurred_at").first()
    assert sample.method == method
    assert sample.reason_code == EntryReasonCode.NO_MATCH


@pytest.mark.parametrize("value", REFERENCE_EARLY_RETURNS)
def test_an_early_reference_outcome_is_audited_measured_and_safe(checkpoint, value):
    audits_before = _audit_rows(checkpoint, action_codes.ENTRY_REFERENCE_LOOKUP).count()
    samples_before = VerificationSample.objects.count()

    outcome = lookup_reference(checkpoint=checkpoint, raw_reference=value)

    assert outcome.result == EntryResult.DENIED
    assert outcome.reason_code == EntryReasonCode.NO_MATCH
    rows = _audit_rows(checkpoint, action_codes.ENTRY_REFERENCE_LOOKUP)
    assert rows.count() == audits_before + 1
    row = rows.order_by("-occurred_at").first()
    assert row.after_summary["method"] == VerificationMethod.REFERENCE
    assert row.after_summary["input_check"] == "EMPTY_REFERENCE"
    assert VerificationSample.objects.count() == samples_before + 1


def test_early_outcomes_count_once_toward_the_budget_and_anomaly_signal(checkpoint, settings):
    """Each early outcome is charged exactly once: the budget and the
    anomaly threshold see them, and a completed lookup is never counted
    twice."""
    settings.ENTRY_LOOKUP_ANOMALY_THRESHOLD = 3
    settings.ENTRY_LOOKUP_MAX_PER_WINDOW = 4

    lookup_identity(checkpoint=checkpoint, method=VerificationMethod.NIN, raw_value=" ")
    lookup_identity(
        checkpoint=checkpoint,
        method=VerificationMethod.PASSPORT,
        raw_value=SYNTHETIC_PASSPORT,
        country_code="FRA",
    )
    assert not _audit_rows(checkpoint, action_codes.ENTRY_ANOMALY_SIGNAL).exists()
    lookup_reference(checkpoint=checkpoint, raw_reference="")
    # The third early outcome crossed the anomaly threshold.
    signal = _audit_rows(checkpoint, action_codes.ENTRY_ANOMALY_SIGNAL).get()
    assert signal.after_summary["count"] == 3
    lookup_reference(checkpoint=checkpoint, raw_reference="  ")
    # Four charged lookups exhaust a budget of four: the next is refused.
    with pytest.raises(EntryLookupThrottled):
        lookup_identity(checkpoint=checkpoint, method=VerificationMethod.NIN, raw_value="")
    counted = AuditEvent.objects.filter(
        actor_user_id=checkpoint.user.pk,
        action_code__in=[action_codes.ENTRY_IDENTITY_LOOKUP, action_codes.ENTRY_REFERENCE_LOOKUP],
    ).count()
    assert counted == 4


def test_a_valid_padded_country_code_still_proceeds_to_matching(checkpoint):
    outcome = lookup_identity(
        checkpoint=checkpoint,
        method=VerificationMethod.PASSPORT,
        raw_value=SYNTHETIC_PASSPORT,
        country_code=" fr ",
    )
    assert outcome.reason_code == EntryReasonCode.NO_MATCH
    row = (
        _audit_rows(checkpoint, action_codes.ENTRY_IDENTITY_LOOKUP).order_by("-occurred_at").first()
    )
    assert "input_check" not in row.after_summary
    _assert_no_raw_value(row, SYNTHETIC_PASSPORT)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("FR", "FR"), (" dz ", "DZ"), ("F", ""), ("FRA", ""), ("1A", ""), ("É1", ""), (None, "")],
)
def test_the_shared_issuing_country_rule_is_exact(value, expected):
    assert normalize_issuing_country(value) == expected


# ---------------------------------------------------------------------------
# Form: exact two-letter validation with a localized error
# ---------------------------------------------------------------------------

COUNTRY_ERRORS = {
    "en": "Enter the two-letter issuing country code, for example FR.",
    "fr": "Saisissez le code à deux lettres du pays de délivrance, par exemple FR.",
    "ar": "أدخل الرمز المكوّن من حرفين لبلد الإصدار، مثل FR.",
}


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
@pytest.mark.parametrize("country", ["F", "1A", "É1"])
def test_the_form_refuses_a_malformed_issuing_country_in_every_language(language, country):
    with translation.override(language):
        form = IdentityLookupForm(
            {
                "method": VerificationMethod.PASSPORT,
                "value": SYNTHETIC_PASSPORT,
                "country_code": country,
            }
        )
        assert not form.is_valid()
        assert form.errors["country_code"] == [COUNTRY_ERRORS[language]]


def test_the_form_normalizes_a_valid_issuing_country():
    form = IdentityLookupForm(
        {"method": VerificationMethod.PASSPORT, "value": SYNTHETIC_PASSPORT, "country_code": "fr"}
    )
    assert form.is_valid()
    assert form.cleaned_data["country_code"] == "FR"


def test_the_nin_form_ignores_the_country_field():
    form = IdentityLookupForm(
        {"method": VerificationMethod.NIN, "value": "123456789012345678", "country_code": "X"}
    )
    assert form.is_valid()
