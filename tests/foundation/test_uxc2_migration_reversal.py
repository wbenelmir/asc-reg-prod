"""UX-C2 (UX-F04, owner decision R1): guarded fail-safe forward and reverse of
the four UX data migrations, on seeded data in the isolated pytest PostgreSQL
database. The development database is never migrated here.

R1 is an intentionally partial reversal:

* `registrations.0010` and `core.0003` keep every reference row on reverse;
* `privacy.0004` and `communications.0006` restore publication state only when
  it is safely established, and otherwise refuse atomically;
* `privacy.0004` always keeps the consent purposes.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)

PRIVACY_BEFORE = "0003_retentioncategory_legalhold_privacyrequest"
PRIVACY_TARGET = "0004_ux3_consent_purposes_and_v2_notices"
COMMS_BEFORE = "0005_communicationmessage_rendered_body_encrypted_and_more"
COMMS_TARGET = "0006_ux2_arabic_event_name_templates"


def _migrate(app: str, target: str) -> None:
    call_command("migrate", app, target, verbosity=0)


@pytest.fixture
def restore_schema():
    yield
    call_command("migrate", verbosity=0)


def _refused(app: str, target: str) -> str:
    with pytest.raises(RuntimeError) as excinfo:
        _migrate(app, target)
    assert "refused" in str(excinfo.value)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Legal documents (privacy.0004)
# ---------------------------------------------------------------------------


def _privacy_seed():
    """The state before privacy.0004: one PUBLISHED v1 per document and
    language, as `privacy.0002` seeds it (recreated after a test flush)."""
    from apps.privacy.models import LegalDocument, LegalDocumentVersion

    _migrate("privacy", PRIVACY_BEFORE)
    now = timezone.now() - timedelta(days=30)
    v1 = {}
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        for language in ("en", "fr", "ar"):
            LegalDocumentVersion.objects.filter(legal_document=document, language=language).delete()
            v1[(code, language)] = LegalDocumentVersion.objects.create(
                legal_document=document,
                language=language,
                version_label="v1-draft",
                content=f"Synthetic v1 {code} {language}",
                content_hash=hashlib.sha256(f"v1 {code} {language}".encode()).hexdigest(),
                effective_from=now,
                status="PUBLISHED",
            )
    return v1


def _state(document_codes=("PRIVACY_NOTICE", "TERMS")):
    from apps.privacy.models import LegalDocumentVersion

    return sorted(
        LegalDocumentVersion.objects.filter(legal_document__code__in=document_codes).values_list(
            "legal_document__code", "language", "version_label", "status", "content"
        )
    )


def test_privacy_reverse_restores_the_paired_v1_and_keeps_everything_else(restore_schema) -> None:
    from apps.privacy.models import ConsentPurpose, LegalDocument, LegalDocumentVersion

    v1 = _privacy_seed()
    older = LegalDocumentVersion.objects.create(  # deliberately retired before the forward
        legal_document=v1[("TERMS", "fr")].legal_document,
        language="fr",
        version_label="v0-draft",
        content="Synthetic v0",
        content_hash="e" * 64,
        effective_from=timezone.now() - timedelta(days=90),
        effective_until=timezone.now() - timedelta(days=60),
        status="RETIRED",
    )
    unrelated_document = LegalDocument.objects.create(code="UNRELATED", document_type="OTHER")
    unrelated = LegalDocumentVersion.objects.create(
        legal_document=unrelated_document,
        language="en",
        version_label="v2-draft",
        content="Unrelated v2",
        content_hash="f" * 64,
        effective_from=timezone.now() - timedelta(days=1),
        status="PUBLISHED",
    )
    purpose, _ = ConsentPurpose.objects.update_or_create(
        code="PERSONAL_DATA_PROCESSING", defaults={"name": "Owner purpose kept as is"}
    )

    _migrate("privacy", PRIVACY_TARGET)
    for version in v1.values():
        version.refresh_from_db()
        assert version.status == "RETIRED"
    assert LegalDocumentVersion.objects.filter(version_label="v2-draft").count() == 7
    purpose.refresh_from_db()
    assert purpose.name == "Owner purpose kept as is"  # get_or_create never rewrote it

    _migrate("privacy", PRIVACY_BEFORE)
    for version in v1.values():
        version.refresh_from_db()
        assert version.status == "PUBLISHED" and version.effective_until is None
    older.refresh_from_db()
    assert older.status == "RETIRED"  # never republished
    unrelated.refresh_from_db()
    assert unrelated.status == "PUBLISHED" and unrelated.content == "Unrelated v2"
    assert (
        LegalDocumentVersion.objects.filter(
            legal_document__code__in=("PRIVACY_NOTICE", "TERMS"), version_label="v2-draft"
        ).count()
        == 0
    )
    # R1 keeps both consent purposes; the pre-existing one is unchanged.
    purpose.refresh_from_db()
    assert purpose.name == "Owner purpose kept as is"
    assert ConsentPurpose.objects.filter(code="SENSITIVE_ACCOMMODATION_DATA").exists()


def test_privacy_forward_refuses_a_pre_existing_identical_target_row(restore_schema) -> None:
    from apps.privacy.models import LegalDocumentVersion

    v1 = _privacy_seed()
    migration = __import__(
        "apps.privacy.migrations.0004_ux3_consent_purposes_and_v2_notices", fromlist=["CONTENT"]
    )
    content = migration.CONTENT[("PRIVACY_NOTICE", "en")]
    collision = LegalDocumentVersion.objects.create(  # identical content, hash and label
        legal_document=v1[("PRIVACY_NOTICE", "en")].legal_document,
        language="en",
        version_label="v2-draft",
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        effective_from=timezone.now(),
        status="DRAFT",
    )
    before = _state()
    _refused("privacy", PRIVACY_TARGET)
    assert _state() == before  # nothing retired, nothing created
    collision.delete()  # let the teardown re-apply the forward


def test_privacy_forward_refuses_competing_published_versions(restore_schema) -> None:
    from apps.privacy.models import LegalDocumentVersion

    v1 = _privacy_seed()
    competing = LegalDocumentVersion.objects.create(
        legal_document=v1[("TERMS", "ar")].legal_document,
        language="ar",
        version_label="owner-final",
        content="Owner text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status="PUBLISHED",
    )
    before = _state()
    _refused("privacy", PRIVACY_TARGET)
    assert _state() == before
    competing.delete()  # let the teardown re-apply the forward


@pytest.mark.parametrize(
    "alteration",
    ["content", "hash_only", "publication_state", "predecessor_republished", "predecessor_end"],
)
def test_privacy_reverse_refuses_manual_changes_atomically(restore_schema, alteration) -> None:
    from apps.privacy.models import LegalDocumentVersion

    v1 = _privacy_seed()
    _migrate("privacy", PRIVACY_TARGET)
    v2 = LegalDocumentVersion.objects.get(
        legal_document__code="PRIVACY_NOTICE", language="en", version_label="v2-draft"
    )
    predecessor = v1[("PRIVACY_NOTICE", "en")]
    if alteration == "content":
        LegalDocumentVersion.objects.filter(pk=v2.pk).update(content=v2.content + " edited")
    elif alteration == "hash_only":
        LegalDocumentVersion.objects.filter(pk=v2.pk).update(content_hash="0" * 64)
    elif alteration == "publication_state":
        LegalDocumentVersion.objects.filter(pk=v2.pk).update(status="RETIRED")
    elif alteration == "predecessor_republished":
        LegalDocumentVersion.objects.filter(pk=predecessor.pk).update(status="PUBLISHED")
    elif alteration == "predecessor_end":
        LegalDocumentVersion.objects.filter(pk=predecessor.pk).update(
            effective_until=timezone.now() + timedelta(seconds=5)
        )
    before = _state()
    _refused("privacy", PRIVACY_BEFORE)
    assert _state() == before  # no partial deletion or republication anywhere
    if alteration == "predecessor_republished":
        # Let the teardown re-apply privacy.0005 (v3, 2026-10-04), whose guarded
        # forward refuses the two PUBLISHED versions this case created.
        LegalDocumentVersion.objects.filter(pk=predecessor.pk).update(status="RETIRED")


def test_privacy_reverse_refuses_a_referenced_version_atomically(restore_schema) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.privacy.models import ConsentPurpose, ConsentRecord, LegalDocumentVersion

    _privacy_seed()
    _migrate("privacy", PRIVACY_TARGET)
    v2 = LegalDocumentVersion.objects.get(
        legal_document__code="TERMS", language="fr", version_label="v2-draft"
    )
    ConsentRecord.objects.create(
        person=resolve_or_create_participant_for_email("uxc2-migration@example.com"),
        purpose=ConsentPurpose.objects.get(code="PERSONAL_DATA_PROCESSING"),
        action="GRANTED",
        source="PUBLIC_WEB",
        occurred_at=timezone.now(),
        legal_document_version=v2,
    )
    before = _state()
    message = _refused("privacy", PRIVACY_BEFORE)
    assert "referenced" in message
    assert _state() == before


# ---------------------------------------------------------------------------
# Message templates (communications.0006)
# ---------------------------------------------------------------------------


def _comms_seed():
    from apps.communications.models import MessageTemplate, MessageTemplateVersion

    _migrate("communications", COMMS_BEFORE)
    migration = __import__(
        "apps.communications.migrations.0006_ux2_arabic_event_name_templates",
        fromlist=["ARABIC_V2"],
    )
    seeded = {}
    for code in migration.ARABIC_V2:
        template, _ = MessageTemplate.objects.get_or_create(
            code=code, defaults={"channel": "EMAIL", "purpose_code": code}
        )
        MessageTemplateVersion.objects.filter(template=template).delete()
        for language in ("en", "fr", "ar"):
            version = MessageTemplateVersion.objects.create(
                template=template,
                language=language,
                version_label="v1-draft",
                subject=f"Synthetic {code} {language}",
                body=f"Synthetic body {language}",
                allowed_variables=[],
                status="PUBLISHED",
                effective_from=timezone.now() - timedelta(days=30),
                content_hash="b" * 64,
            )
            seeded[(code, language)] = version
    return seeded


def _comms_state():
    from apps.communications.models import MessageTemplateVersion

    return sorted(
        MessageTemplateVersion.objects.values_list(
            "template__code", "language", "version_label", "status", "subject"
        )
    )


def test_comms_reverse_restores_each_paired_v1(restore_schema) -> None:
    seeded = _comms_seed()
    _migrate("communications", COMMS_TARGET)
    arabic = {key: value for key, value in seeded.items() if key[1] == "ar"}
    for version in arabic.values():
        version.refresh_from_db()
        assert version.status == "RETIRED" and version.effective_until is not None
    _migrate("communications", COMMS_BEFORE)
    for version in seeded.values():
        version.refresh_from_db()
        assert version.status == "PUBLISHED" and version.effective_until is None
    from apps.communications.models import MessageTemplateVersion

    assert not MessageTemplateVersion.objects.filter(version_label="v2-draft").exists()


def test_comms_reverse_refuses_a_referenced_version_atomically(restore_schema) -> None:
    from apps.communications.models import CommunicationMessage, MessageTemplateVersion

    _comms_seed()
    _migrate("communications", COMMS_TARGET)
    v2 = MessageTemplateVersion.objects.get(
        template__code="REGISTRATION_CONFIRMATION", language="ar", version_label="v2-draft"
    )
    CommunicationMessage.objects.create(
        template_version=v2,
        channel="EMAIL",
        language="ar",
        destination_encrypted="synthetic@example.com",
        destination_hash="c" * 64,
        destination_hash_key_version=1,
        content_hash="d" * 64,
        idempotency_key="uxc2-migration-message",
    )
    before = _comms_state()
    message = _refused("communications", COMMS_BEFORE)
    assert "messages" in message
    after = _comms_state()
    assert after == before  # no partial deletion, and no second published Arabic version
    published_ar = [row for row in after if row[1] == "ar" and row[3] == "PUBLISHED"]
    assert len(published_ar) == len({row[0] for row in after})


def test_comms_forward_refuses_a_pre_existing_target_row(restore_schema) -> None:
    from apps.communications.models import MessageTemplateVersion

    seeded = _comms_seed()
    template = seeded[("DELEGATION_CLAIM", "ar")].template
    collision = MessageTemplateVersion.objects.create(
        template=template,
        language="ar",
        version_label="v2-draft",
        subject="Owner v2",
        body="Owner body",
        allowed_variables=[],
        status="DRAFT",
        effective_from=timezone.now(),
        content_hash="e" * 64,
    )
    before = _comms_state()
    _refused("communications", COMMS_TARGET)
    assert _comms_state() == before
    collision.delete()  # let the teardown re-apply the forward


def test_comms_forward_leaves_an_owner_version_untouched(restore_schema) -> None:
    from apps.communications.models import MessageTemplateVersion

    seeded = _comms_seed()
    owner = seeded[("ACCOUNT_ACCESS", "ar")]
    MessageTemplateVersion.objects.filter(pk=owner.pk).update(version_label="owner-final")
    _migrate("communications", COMMS_TARGET)
    owner.refresh_from_db()
    assert owner.status == "PUBLISHED" and owner.version_label == "owner-final"
    assert not MessageTemplateVersion.objects.filter(
        template=owner.template, version_label="v2-draft"
    ).exists()
    _migrate("communications", COMMS_BEFORE)  # the other two templates reverse safely
    owner.refresh_from_db()
    assert owner.status == "PUBLISHED"


def test_comms_reverse_refuses_an_edited_subject(restore_schema) -> None:
    from apps.communications.models import MessageTemplateVersion

    _comms_seed()
    _migrate("communications", COMMS_TARGET)
    MessageTemplateVersion.objects.filter(
        template__code="DELEGATION_CLAIM", language="ar", version_label="v2-draft"
    ).update(subject="Edited by an operator")
    before = _comms_state()
    _refused("communications", COMMS_BEFORE)
    assert _comms_state() == before


# ---------------------------------------------------------------------------
# Reference rows (registrations.0010, core.0003)
# ---------------------------------------------------------------------------


def test_reference_rows_are_kept_on_reverse(restore_schema) -> None:
    from apps.core.models import Sector
    from apps.events.models import EventEdition
    from apps.registrations.models import InterestTopic

    _migrate("core", "0002_human_challenge_use")
    _migrate("registrations", "0009_interesttopic_group_code")
    Sector.objects.filter(code="AGRITECH").delete()
    owner_sector = Sector.objects.create(code="AGRITECH", name="Owner agritech label")
    other_event = EventEdition.objects.create(
        code="UXC2OTHER",
        name="Unrelated edition",
        timezone="UTC",
        starts_at=timezone.now() + timedelta(days=40),
        ends_at=timezone.now() + timedelta(days=41),
    )
    other_topic = InterestTopic.objects.create(
        event_edition=other_event, code="AI", label="Unrelated AI topic"
    )
    call_command("migrate", verbosity=0)
    sectors_after_forward = set(Sector.objects.values_list("code", flat=True))

    _migrate("registrations", "0009_interesttopic_group_code")
    _migrate("core", "0002_human_challenge_use")
    owner_sector.refresh_from_db()
    assert owner_sector.name == "Owner agritech label"
    other_topic.refresh_from_db()
    assert other_topic.label == "Unrelated AI topic"
    assert set(Sector.objects.values_list("code", flat=True)) == sectors_after_forward
