"""UPD-04: the Ministry NIN lookup tool shows the returned identity details.

Pure display extraction (`diagnostic_identity_from_body`, the birth-date
description) and the page itself through the official adapter with a fake
transport: Latin and Arabic names, the birth date (complete, partial,
unreadable, absent; never completed), the presume flag with its explanation,
`identite: null` distinct from a found identity, service errors, and nothing
else of the response (no token, no number, no unrelated field) shown, logged
or audited. Synthetic data only.
"""

from __future__ import annotations

import datetime
import json
import logging

import pytest

from apps.audit.models import AuditEvent
from apps.people import nin_diagnostics as diagnostics
from apps.people.identity_contract import (
    DATE_ABSENT,
    DATE_COMPLETE,
    DATE_PARTIAL,
    DATE_UNREADABLE,
    describe_official_birth_date,
    diagnostic_identity_from_body,
)

from .test_ministry_nin_diagnostics import (  # noqa: F401 - fixtures
    ISSUED_BEARER,
    LOOKUP_URL,
    RAW_MARKER,
    TEST_NIN,
    _client,
    _json,
    fake_transport,
    operator,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("raw", "kind", "parts"),
    [
        ("07/03/1990", DATE_COMPLETE, (7, 3, 1990)),
        ("00/00/1985", DATE_PARTIAL, (None, None, 1985)),
        ("00/05/1985", DATE_PARTIAL, (None, 5, 1985)),
        ("XX/XX/1985", DATE_PARTIAL, (None, None, 1985)),
        ("1985", DATE_PARTIAL, (None, None, 1985)),
        ("//1985", DATE_PARTIAL, (None, None, 1985)),
        ("31/02/1990", DATE_UNREADABLE, (None, None, None)),
        ("1990-03-07", DATE_UNREADABLE, (None, None, None)),
        ("13/13/1990", DATE_UNREADABLE, (None, None, None)),
        ("", DATE_ABSENT, (None, None, None)),
        (None, DATE_ABSENT, (None, None, None)),
        (19900307, DATE_UNREADABLE, (None, None, None)),
    ],
)
def test_birth_dates_are_described_without_inventing_parts(raw, kind, parts):
    described = describe_official_birth_date(raw)
    assert described.kind == kind
    assert (described.day, described.month, described.year) == parts
    if kind == DATE_COMPLETE:
        assert described.value == datetime.date(1990, 3, 7)
    else:
        assert described.value is None


def test_display_details_keep_only_the_requested_fields():
    body = {
        "identite": {
            "nin": TEST_NIN,
            "nom_f": "SYNTHETIQUE",
            "pren_f": "AMINA",
            "nom_a": "اختبار",
            "pren_a": "أمينة",
            "d_nais": "00/00/1985",
            "presume": "True",
            "pere": "should never be read",
        }
    }
    identity = diagnostic_identity_from_body(body, requested_nin=TEST_NIN)
    assert identity.nin_matches_request is True
    assert (identity.family_name_arabic, identity.given_names_arabic) == ("اختبار", "أمينة")
    assert identity.presume == "true"
    assert identity.birth_date.kind == DATE_PARTIAL
    assert not hasattr(identity, "pere")
    assert diagnostic_identity_from_body({"identite": None}, requested_nin=TEST_NIN) is None


def _lookup(operator, fake_transport, body, caplog=None):  # noqa: F811 - shared fixtures
    fake_transport.lookup = [_json(200, body)]
    response = _client(operator).post(LOOKUP_URL, {"nin": TEST_NIN})
    return response, response.content.decode()


def test_a_found_identity_shows_names_birth_date_and_presume(operator, fake_transport, caplog):  # noqa: F811 - shared fixtures
    body = {
        "identite": {
            "nin": TEST_NIN,
            "nom_f": "SYNTHETIQUE",
            "pren_f": "AMINA",
            "nom_a": "اختبار",
            "pren_a": "أمينة",
            "d_nais": "07/03/1990",
            "presume": "False",
            "mere": RAW_MARKER,
        },
        "note": RAW_MARKER,
    }
    with caplog.at_level(logging.DEBUG):
        response, html = _lookup(operator, fake_transport, body)
    assert response.context["presented"]["result"].outcome == diagnostics.IDENTITY_FOUND
    for value in ("SYNTHETIQUE", "AMINA", "اختبار", "أمينة", "07/03/1990"):
        assert value in html, value
    assert 'data-presume="false"' in html
    for secret in (RAW_MARKER, ISSUED_BEARER, TEST_NIN):
        assert secret not in html, secret
        assert secret not in caplog.text, secret
    summary = json.dumps(
        AuditEvent.objects.filter(action_code="IDV_NIN_DIAGNOSTIC_RUN").last().after_summary
    )
    for value in ("SYNTHETIQUE", "AMINA", "1990"):
        assert value not in summary
    assert "SYNTHETIQUE" not in caplog.text


def test_a_presumed_and_incomplete_date_is_explained_never_completed(operator, fake_transport):  # noqa: F811 - shared fixtures
    body = {
        "identite": {"nin": TEST_NIN, "nom_f": "SYNTHETIQUE", "pren_f": "KARIM",
                     "d_nais": "00/00/1985", "presume": True}
    }  # fmt: skip
    _response, html = _lookup(operator, fake_transport, body)
    assert 'data-birth-date-kind="partial"' in html
    assert "00/00/1985" in html
    assert "data-identity-presumed" in html
    assert "01/01/1985" not in html


def test_missing_fields_are_shown_as_not_supplied(operator, fake_transport):  # noqa: F811 - shared fixtures
    body = {"identite": {"nin": TEST_NIN, "nom_f": "SYNTHETIQUE", "pren_f": "LINA",
                         "presume": "False", "d_nais": "20/11/1988"}}  # fmt: skip
    _response, html = _lookup(operator, fake_transport, body)
    assert 'data-identity-field="family_name_arabic"' in html
    assert html.count("Not supplied") >= 2


def test_identite_null_is_not_shown_as_an_identity(operator, fake_transport):  # noqa: F811 - shared fixtures
    response, html = _lookup(operator, fake_transport, {"identite": None})
    assert response.context["presented"]["result"].outcome == diagnostics.IDENTITY_NOT_FOUND
    assert "data-diagnostic-identity" not in html


def test_an_answer_for_another_number_is_flagged(operator, fake_transport):  # noqa: F811 - shared fixtures
    body = {"identite": {"nin": "990000000000000066", "nom_f": "AUTRE", "pren_f": "PERSONNE",
                         "presume": "False", "d_nais": "01/01/1980"}}  # fmt: skip
    _response, html = _lookup(operator, fake_transport, body)
    assert "data-identity-nin-mismatch" in html
    assert "990000000000000066" not in html


def test_an_incomplete_identity_is_reported_as_unreadable_with_its_fields(operator, fake_transport):  # noqa: F811 - shared fixtures
    body = {"identite": {"nin": TEST_NIN, "nom_f": "SYNTHETIQUE", "presume": "False"}}
    response, html = _lookup(operator, fake_transport, body)
    assert response.context["presented"]["result"].outcome == diagnostics.MALFORMED
    assert 'data-identity-field="given_names_latin"' in html
    assert 'data-birth-date-kind="absent"' in html


def test_a_service_error_shows_no_identity(operator, fake_transport):  # noqa: F811 - shared fixtures
    from apps.people.nin_provider import HttpResponse

    fake_transport.lookup = [HttpResponse(status=503, body=b"down")]
    response = _client(operator).post(LOOKUP_URL, {"nin": TEST_NIN})
    assert response.context["presented"]["result"].outcome == diagnostics.UNAVAILABLE
    assert "data-diagnostic-identity" not in response.content.decode()


def test_the_lookup_never_changes_a_registration(operator, fake_transport):  # noqa: F811 - shared fixtures
    from apps.people.models import IdentityVerification, IdentityVerificationAttempt
    from apps.registrations.models import Registration

    counts = (
        Registration.objects.count(),
        IdentityVerification.objects.count(),
        IdentityVerificationAttempt.objects.count(),
    )
    _lookup(
        operator,
        fake_transport,
        {"identite": {"nin": TEST_NIN, "nom_f": "A", "pren_f": "B", "presume": "True"}},
    )
    assert counts == (
        Registration.objects.count(),
        IdentityVerification.objects.count(),
        IdentityVerificationAttempt.objects.count(),
    )
