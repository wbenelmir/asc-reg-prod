"""No raw internal code reaches a user (UI/UX Completion Gate Stage 3).

Every stored code the platform can produce, and that is not a translated
model choice, has a translated display label; every status family used by a
migrated page has a tone and icon for each stored value.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.utils import translation
from django.utils.module_loading import import_string

from apps.core.display_labels import CODE_LABELS, code_label
from apps.core.templatetags.asc_ui import _STATUS_TONES, status_tone


def _codes(family: str) -> set[str]:
    return set(CODE_LABELS[family])


def test_export_purposes_are_labelled() -> None:
    from apps.exports.models import ALLOWED_EXPORT_PURPOSE_CODES

    assert set(ALLOWED_EXPORT_PURPOSE_CODES) <= _codes("export_purpose")


def test_communication_purposes_are_labelled() -> None:
    from apps.communications.purposes import CommunicationPurpose
    from apps.invitations.services import DELEGATION_CLAIM_TEMPLATE_CODE

    purposes = {
        value
        for name, value in vars(CommunicationPurpose).items()
        if name.isupper() and isinstance(value, str)
    }
    assert purposes | {DELEGATION_CLAIM_TEMPLATE_CODE} <= _codes("communication_purpose")


def test_languages_are_labelled() -> None:
    assert {code for code, _name in settings.LANGUAGES} <= _codes("language")


def test_checklist_items_and_request_fields_are_labelled() -> None:
    from apps.reviews.services import ALLOWED_CHECKLIST_ITEM_CODES, ALLOWED_REQUEST_FIELD_CODES

    items = {code for codes in ALLOWED_CHECKLIST_ITEM_CODES.values() for code in codes}
    assert items <= _codes("checklist_item")
    assert set(ALLOWED_REQUEST_FIELD_CODES) <= _codes("request_field")


def test_delegation_row_errors_are_labelled() -> None:
    from apps.invitations.services import REQUIRED_DELEGATION_COLUMNS

    produced = {"INVALID_EMAIL", "FORMULA_SHAPED_VALUE"} | {
        f"MISSING_{column.upper()}" for column in REQUIRED_DELEGATION_COLUMNS
    }
    produced |= {
        "DUPLICATE_COLUMN",
        "FORBIDDEN_COLUMN",
        "UNKNOWN_COLUMN",
        "MISSING_REQUIRED_COLUMN",
    }
    assert produced <= _codes("delegation_error")


@pytest.mark.parametrize(
    ("family", "choices_path"),
    [
        ("export_status", "apps.exports.models.ExportStatus"),
        ("message_status", "apps.communications.models.CommunicationMessageStatus"),
    ],
)
def test_untranslated_model_statuses_have_translated_labels(family, choices_path) -> None:
    assert set(import_string(choices_path).values) == _codes(family)


@pytest.mark.parametrize(
    ("family", "choices_path"),
    [
        ("campaign", "apps.invitations.models.InvitationCampaignStatus"),
        ("invitation_link", "apps.invitations.models.InvitationLinkStatus"),
        ("delegation_batch", "apps.invitations.models.DelegationBatchStatus"),
        ("delegation_row", "apps.invitations.models.DelegationRowStatus"),
        ("information_request", "apps.reviews.models.InformationRequestStatus"),
        ("export", "apps.exports.models.ExportStatus"),
        ("message", "apps.communications.models.CommunicationMessageStatus"),
    ],
)
def test_every_stage3_status_has_a_tone_and_icon(family, choices_path) -> None:
    values = set(import_string(choices_path).values)
    assert set(_STATUS_TONES[family]) == values
    for value in values:
        tone, icon = status_tone(family, value)
        assert tone in {"success", "info", "warning", "danger", "neutral"}
        assert icon


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Operational reporting"),
        ("fr", "Rapports opérationnels"),
        ("ar", "التقارير التشغيلية"),
    ],
)
def test_labels_are_translated(language, expected) -> None:
    with translation.override(language):
        assert code_label("export_purpose", "OPERATIONAL_REPORTING") == expected


def test_unknown_code_is_shown_as_is_never_hidden() -> None:
    assert code_label("export_purpose", "SOMETHING_NEW") == "SOMETHING_NEW"
    assert code_label("export_purpose", "") == ""
