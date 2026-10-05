"""UX-3 migrations reverse and re-apply cleanly on real PostgreSQL.

`registrations.0008_accommodation_request` adds one table;
`privacy.0004_ux3_consent_purposes_and_v2_notices` adds two consent purposes
and the v2 notices, retiring the v1 versions they replace. Reversing restores
v1 as the published version and removes only the unreferenced v2 rows; since
UX-C2 (R1) it keeps the consent purposes and refuses unsafe states
(`tests/foundation/test_uxc2_migration_reversal.py`).
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_registrations_0008_reverses_and_reapplies() -> None:
    try:
        call_command("migrate", "registrations", "0007", verbosity=0)
        assert "registrations_accommodation_request" not in _tables()
    finally:
        call_command("migrate", "registrations", verbosity=0)
    assert "registrations_accommodation_request" in _tables()


def test_privacy_0004_reverses_and_reapplies() -> None:
    from django.utils import timezone

    from apps.privacy.models import ConsentPurpose, LegalDocument, LegalDocumentVersion

    try:
        call_command("migrate", "privacy", "0003", verbosity=0)
        assert not LegalDocumentVersion.objects.filter(version_label="v2-draft").exists()
        # UX-C2 (UX-F04, R1): the reverse always keeps the consent purposes.
        # A transactional test database may have been flushed, so the v1 row
        # the forward migration retires is created here explicitly.
        document, _ = LegalDocument.objects.get_or_create(
            code="TERMS", defaults={"document_type": "TERMS"}
        )
        LegalDocumentVersion.objects.filter(legal_document=document, language="fr").delete()
        v1 = LegalDocumentVersion.objects.create(
            legal_document=document,
            language="fr",
            version_label="v1-draft",
            content="Synthetic v1",
            content_hash="d" * 64,
            effective_from=timezone.now(),
            status="PUBLISHED",
        )
    finally:
        call_command("migrate", "privacy", verbosity=0)
    assert LegalDocumentVersion.objects.filter(version_label="v2-draft").count() == 6
    v1.refresh_from_db()
    assert v1.status == "RETIRED" and v1.effective_until is not None
    assert v1.content == "Synthetic v1"  # never rewritten
    assert ConsentPurpose.objects.filter(code="SENSITIVE_ACCOMMODATION_DATA").exists()
