"""The owner-approved country catalog (C-01, 2026-10-04): data, install, reconcile.

Covers the frozen data, a fresh install, the reconciliation of pre-existing
rows and labels, repeat runs, translated labels, the active choices offered
by the forms, preserved historical references, the operator command and the
provisioning guard. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import io
import json
import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import translation

from apps.core.models import Country, selectable_countries
from apps.core.reference_data import countries_v1
from apps.core.services.country_catalog import reconcile_countries, referencing_row_counts

pytestmark = pytest.mark.django_db

APPROVED = {code: (name, fr, ar) for code, name, fr, ar in countries_v1.ENTRIES}


def _install() -> None:
    reconcile_countries(Country, countries_v1, apply=True)


def _empty_country_table() -> None:
    """A fresh database: no country row yet (nothing references one here)."""
    Country.objects.all().delete()


# ---------------------------------------------------------------------------
# The frozen data
# ---------------------------------------------------------------------------


def test_the_catalog_has_the_248_approved_entries_unchanged() -> None:
    entries = countries_v1.ENTRIES
    assert len(entries) == 248
    codes = [code for code, *_labels in entries]
    assert len(set(codes)) == 248
    assert codes == sorted(codes)
    assert all(re.fullmatch(r"[A-Z]{2}", code) for code in codes)
    assert all(all(label and label == label.strip() for label in labels) for _c, *labels in entries)
    digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert digest == countries_v1.ENTRIES_SHA256
    assert countries_v1.SOURCE_SHA256 == (
        "3c82fc8c4e5061fa8441f465738ceb2b4b1c8e9b1274f00f5fbb9cc1ef2728ca"
    )


def test_palestine_uses_ps_and_the_owner_labels_and_il_xk_are_excluded() -> None:
    assert APPROVED["PS"] == ("Palestine", "Palestine", "فلسطين")
    assert "IL" not in APPROVED and "XK" not in APPROVED
    assert set(countries_v1.EXCLUDED_CODES) == {"IL", "XK"}


def test_territories_are_included_as_supplied() -> None:
    for code in ("RE", "GP", "HK", "MO", "PR", "GI", "NC", "EH", "TW", "AQ"):
        assert code in APPROVED
    assert APPROVED["DZ"] == ("Algeria", "Algérie", "الجزائر")
    assert APPROVED["CI"] == ("Côte d’Ivoire", "Côte d’Ivoire", "ساحل العاج")


# ---------------------------------------------------------------------------
# Fresh install, repeat, reconciliation
# ---------------------------------------------------------------------------


def test_a_fresh_install_creates_every_approved_entry_active_with_its_labels() -> None:
    _empty_country_table()
    report = reconcile_countries(Country, countries_v1, apply=True)
    assert len(report.created) == 248 and report.changes == 248
    assert Country.objects.count() == 248
    assert selectable_countries().count() == 248
    for country in Country.objects.all():
        assert (country.name, country.name_fr, country.name_ar) == APPROVED[country.code]
        assert country.is_active


def test_a_repeat_run_changes_nothing() -> None:
    _install()
    before = sorted(Country.objects.values_list("code", "name", "name_fr", "name_ar", "is_active"))
    report = reconcile_countries(Country, countries_v1, apply=True)
    assert report.in_sync and report.changes == 0
    after = sorted(Country.objects.values_list("code", "name", "name_fr", "name_ar", "is_active"))
    assert before == after


def test_existing_rows_are_reconciled_explicitly_and_nothing_is_deleted() -> None:
    from apps.organizations.services import create_organization

    _install()
    # A synthetic label, an inactive approved entry, both exclusions active,
    # and an unlisted code that an organization references.
    Country.objects.filter(code="TN").update(name="Tunisia (old)", name_fr="", name_ar="")
    Country.objects.filter(code="MA").update(is_active=False)
    Country.objects.update_or_create(code="IL", defaults={"name": "Israel", "is_active": True})
    Country.objects.update_or_create(code="XK", defaults={"name": "Kosovo", "is_active": True})
    Country.objects.update_or_create(code="ZZ", defaults={"name": "Unlisted", "is_active": True})
    organization = create_organization(
        official_name="Catalog History Org", organization_type="STARTUP", country_code_id="ZZ"
    )
    preview = reconcile_countries(Country, countries_v1, apply=False)
    assert preview.relabeled == ["TN"] and preview.reactivated == ["MA"]
    assert preview.excluded_deactivated == ["IL", "XK"]
    assert preview.unlisted_deactivated == ["ZZ"]
    assert Country.objects.get(code="IL").is_active  # a preview writes nothing

    report = reconcile_countries(Country, countries_v1, apply=True)
    assert report.changes == 5
    tunisia = Country.objects.get(code="TN")
    assert (tunisia.name, tunisia.name_fr, tunisia.name_ar) == APPROVED["TN"]
    assert Country.objects.get(code="MA").is_active
    for code in ("IL", "XK", "ZZ"):
        row = Country.objects.get(code=code)  # kept, never deleted
        assert not row.is_active
        assert not selectable_countries().filter(code=code).exists()
    organization.refresh_from_db()
    assert organization.country_code_id == "ZZ"  # the history is untouched
    assert referencing_row_counts(Country, ["ZZ", "IL"]) == {"ZZ": 1, "IL": 0}
    assert sorted(report.outside_catalog) == ["IL", "XK", "ZZ"]
    assert reconcile_countries(Country, countries_v1, apply=True).in_sync


def test_registrations_keep_a_country_that_leaves_the_selectable_list() -> None:
    """A submitted registration that references a country that later becomes
    inactive is not rewritten; it still shows its localized name."""
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import make_event
    from apps.registrations.models import RegistrationProfile
    from apps.registrations.services import get_or_create_active_draft

    _install()
    Country.objects.update_or_create(code="XK", defaults={"name": "Kosovo", "is_active": True})
    person = resolve_or_create_participant_for_email("catalog-history@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=make_event("CATALOG"))
    RegistrationProfile.objects.update_or_create(
        registration=draft, defaults={"nationality_code_id": "XK", "country_of_residence_id": "DZ"}
    )
    reconcile_countries(Country, countries_v1, apply=True)
    profile = RegistrationProfile.objects.get(registration=draft)
    assert profile.nationality_code_id == "XK"
    assert profile.nationality_code.localized_name == "Kosovo"


# ---------------------------------------------------------------------------
# Translated labels and the choices the forms offer
# ---------------------------------------------------------------------------


def test_the_catalog_labels_are_shown_in_each_language() -> None:
    _install()
    palestine = Country.objects.get(code="PS")
    algeria = Country.objects.get(code="DZ")
    for language, expected_ps, expected_dz in (
        ("en", "Palestine", "Algeria"),
        ("fr", "Palestine", "Algérie"),
        ("ar", "فلسطين", "الجزائر"),
    ):
        with translation.override(language):
            assert palestine.localized_name == expected_ps
            assert algeria.localized_name == expected_dz


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_every_country_list_offers_exactly_the_active_catalog(language) -> None:
    from apps.organizations.models import OperatingScope  # noqa: F401 - form import side effects
    from apps.registrations.forms import ContactStepForm, IdentityStepForm, ProfessionalStepForm

    _install()
    Country.objects.update_or_create(code="IL", defaults={"name": "Israel", "is_active": True})
    with translation.override(language):
        identity = IdentityStepForm()
        professional = ProfessionalStepForm()
        contact = ContactStepForm()
        for field in (
            identity.fields["nationality_code"],
            identity.fields["country_of_residence"],
            identity.fields["passport_country_code"],
            professional.fields["country_code"],
        ):
            codes = set(field.queryset.values_list("code", flat=True))
            assert codes == set(APPROVED)  # 248, IL and XK never offered
        # The calling-code list: the same catalog, minus the territories that
        # have no telephone calling code (no number can be validated for them).
        import phonenumbers

        calling = set(contact.fields["mobile_country_code"].queryset.values_list("code", flat=True))
        without_code = {code for code in APPROVED if not phonenumbers.country_code_for_region(code)}
        assert without_code == {"AQ", "BV", "GS", "HM", "PN", "TF", "UM"}
        assert calling == set(APPROVED) - without_code
        html = str(identity["nationality_code"])
        assert (
            {"en": "Palestine", "fr": "Palestine", "ar": "فلسطين"}[language] in html
            and 'value="IL"' not in html
            and 'value="XK"' not in html
        )


# ---------------------------------------------------------------------------
# The operator command
# ---------------------------------------------------------------------------


def _command(*args: str) -> str:
    out = io.StringIO()
    call_command("reconcile_country_catalog", *args, stdout=out)
    return out.getvalue()


def test_the_command_previews_applies_and_then_reports_in_sync() -> None:
    _install()
    Country.objects.filter(code="FR").update(name_ar="")
    Country.objects.update_or_create(code="XK", defaults={"name": "Kosovo", "is_active": True})
    with pytest.raises(CommandError) as refused:
        _command()
    assert refused.value.returncode == 1
    assert Country.objects.get(code="FR").name_ar == ""  # the preview wrote nothing
    applied = _command("--apply")
    assert "Applied 2 change(s)" in applied and "FR" in applied and "XK: 0" in applied
    assert Country.objects.get(code="FR").name_ar == "فرنسا"
    assert "In sync" in _command()
    assert "In sync" in _command("--apply")  # repeatable


# ---------------------------------------------------------------------------
# Provisioning never replaces the catalog
# ---------------------------------------------------------------------------


def test_staging_provisioning_checks_the_catalog_and_never_creates_a_country(settings) -> None:
    from apps.core.services import staging_provisioning as provisioning

    settings.STAGING_PROVISIONING_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://localhost"
    _install()
    count = Country.objects.count()
    report = provisioning.build_plan(
        email_pattern="uat-{key}@uat-mail.example.invalid", confirm_host="localhost"
    )
    country_lines = [line for line in report.lines if line.kind == "country"]
    assert [(line.key, line.action) for line in country_lines] == [
        ("DZ", "exists"),
        ("FR", "exists"),
    ]
    Country.objects.filter(code="FR").update(is_active=False)
    report = provisioning.build_plan(
        email_pattern="uat-{key}@uat-mail.example.invalid", confirm_host="localhost"
    )
    assert ("FR", "conflict") in [(line.key, line.action) for line in report.conflicts]
    assert Country.objects.count() == count
    assert not hasattr(provisioning, "SYNTHETIC_COUNTRIES")
