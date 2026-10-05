"""Idempotent local/demo bootstrap for the minimum Phase 1 reference data.

    uv run --env-file .env python manage.py seed_phase1_local_data --settings=config.settings.local

This is a REAL local PostgreSQL database, not a test fixture: without this
command (or the migrations it mirrors), a fresh local database has no
`Country`/`Sector` rows at all, and the Algerian/international registration
paths cannot be exercised through the real running application (review
finding, Prompt 5 correction pass §1).

This is deliberately NOT a production country or sector catalogue -- only
the minimum synthetic reference data the Phase 1 demonstrated journey
needs: Algeria (the Algerian/NIN path), France (the demonstrated
international/passport path), the TECH sector, the current synthetic event
configuration, the draft legal-document versions, and the seeded interest
topics. The countries come from the owner-approved catalog that migration
`core.0005_approved_country_catalog` installs (C-01), so on a migrated
database the two country lines find the existing catalog rows and create
nothing; they never rename or reactivate a country
(`manage.py reconcile_country_catalog` does that).

Safe to run repeatedly: every write is `get_or_create` (or an equivalent
existence check), so a row a reviewer has already edited -- a renamed
Country, a re-worded legal notice -- is never overwritten. The one
exception is the two `--enable-`/`--disable-passport-identity-page-upload`
flags, which exist purely to flip the Prompt 5 correction §2 policy switch
for a local manual walkthrough; they touch only that one boolean field on
the current event and only when explicitly passed.
"""

from __future__ import annotations

import hashlib

from django.core.management.base import BaseCommand
from django.utils import timezone

ALGERIA = ("DZ", "Algeria", "Algérie", "الجزائر")
FRANCE = ("FR", "France", "France", "فرنسا")
TECH_SECTOR = ("TECH", "Technology", "Technologie", "التكنولوجيا")

EVENT_CODE = "ASC2026"

INTEREST_TOPICS = [
    ("FUNDING", "Access to funding", "Accès au financement", "الوصول إلى التمويل"),
    ("MENTORSHIP", "Mentorship and coaching", "Mentorat et accompagnement", "الإرشاد والتوجيه"),
    ("MARKET_ACCESS", "Market access", "Accès au marché", "الوصول إلى السوق"),
    (
        "TECHNOLOGY",
        "Technology and innovation",
        "Technologie et innovation",
        "التكنولوجيا والابتكار",
    ),
    ("POLICY", "Policy and regulation", "Politique et réglementation", "السياسات والتنظيم"),
]

_LEGAL_CONTENT = {
    ("PRIVACY_NOTICE", "en"): "[DRAFT -- pending Legal review] Synthetic local Privacy Notice.",
    (
        "PRIVACY_NOTICE",
        "fr",
    ): "[BROUILLON -- en attente de révision] Notice de confidentialité synthétique locale.",
    ("PRIVACY_NOTICE", "ar"): "[مسودة -- قيد المراجعة القانونية] إشعار خصوصية اصطناعي محلي.",
    ("TERMS", "en"): "[DRAFT -- pending Legal review] Synthetic local Registration Terms.",
    (
        "TERMS",
        "fr",
    ): "[BROUILLON -- en attente de révision] Conditions d'inscription synthétiques locales.",
    ("TERMS", "ar"): "[مسودة -- قيد المراجعة القانونية] شروط تسجيل اصطناعية محلية.",
}


class Command(BaseCommand):
    help = (
        "Idempotently create the minimum synthetic Phase 1 reference data "
        "(Algeria, France, the TECH sector, the current event, legal "
        "notices, and interest topics) in the real local database. Never "
        "overwrites a row that already exists."
    )

    def add_arguments(self, parser) -> None:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--enable-passport-identity-page-upload",
            action="store_true",
            help=(
                "Also enable the disabled-by-default passport identity-page "
                "upload policy switch on the current event, for a local "
                "manual walkthrough. Never done by default."
            ),
        )
        group.add_argument(
            "--disable-passport-identity-page-upload",
            action="store_true",
            help="Explicitly turn the same policy switch back off.",
        )

    def handle(self, *args, **options) -> None:
        from apps.core.models import Country, Sector
        from apps.events.models import EventEdition, EventEditionStatus
        from apps.privacy.models import LegalDocument, LegalDocumentVersion
        from apps.registrations.models import InterestTopic

        created = {"countries": 0, "sectors": 0, "events": 0, "legal_versions": 0, "topics": 0}

        for code, name, name_fr, name_ar in (ALGERIA, FRANCE):
            _, was_created = Country.objects.get_or_create(
                code=code, defaults={"name": name, "name_fr": name_fr, "name_ar": name_ar}
            )
            created["countries"] += int(was_created)

        _, sector_created = Sector.objects.get_or_create(
            code=TECH_SECTOR[0],
            defaults={"name": TECH_SECTOR[1], "name_fr": TECH_SECTOR[2], "name_ar": TECH_SECTOR[3]},
        )
        created["sectors"] += int(sector_created)

        now = timezone.now()
        event, event_created = EventEdition.objects.get_or_create(
            code=EVENT_CODE,
            defaults={
                "name": "Africa Startup Conference 2026",
                "timezone": "Africa/Algiers",
                "starts_at": now,
                "ends_at": now,
                "registration_opens_at": now,
                "status": EventEditionStatus.REGISTRATION_OPEN,
                "default_language": "en",
                "supported_languages": ["en", "fr", "ar"],
            },
        )
        created["events"] += int(event_created)

        document_types = {"PRIVACY_NOTICE": "PRIVACY_NOTICE", "TERMS": "TERMS"}
        for code, document_type in document_types.items():
            document, _ = LegalDocument.objects.get_or_create(
                code=code, defaults={"document_type": document_type}
            )
            for language in ("en", "fr", "ar"):
                # Checked by (document, language, PUBLISHED) -- not by this
                # command's own `version_label` -- so a version already
                # published under a DIFFERENT label (e.g. the
                # `privacy.0002_seed_legal_notices` migration's "v1-draft")
                # is recognized as already satisfying the requirement and is
                # never shadowed by a second, competing PUBLISHED version
                # for the same document and language.
                already_published = LegalDocumentVersion.objects.filter(
                    legal_document=document, language=language, status="PUBLISHED"
                ).exists()
                if already_published:
                    continue
                LegalDocumentVersion.objects.create(
                    legal_document=document,
                    language=language,
                    version_label="v1-local-seed",
                    content=_LEGAL_CONTENT[(code, language)],
                    content_hash=hashlib.sha256(
                        _LEGAL_CONTENT[(code, language)].encode("utf-8")
                    ).hexdigest(),
                    effective_from=now,
                    status="PUBLISHED",
                )
                created["legal_versions"] += 1

        for topic_code, label_en, label_fr, label_ar in INTEREST_TOPICS:
            _, topic_created = InterestTopic.objects.get_or_create(
                event_edition=event,
                code=topic_code,
                defaults={"label": label_en, "label_fr": label_fr, "label_ar": label_ar},
            )
            created["topics"] += int(topic_created)

        if options.get("enable_passport_identity_page_upload"):
            EventEdition.objects.filter(pk=event.pk).update(
                passport_identity_page_upload_enabled=True
            )
            self.stdout.write("Passport identity-page upload ENABLED for the current event.")
        elif options.get("disable_passport_identity_page_upload"):
            EventEdition.objects.filter(pk=event.pk).update(
                passport_identity_page_upload_enabled=False
            )
            self.stdout.write("Passport identity-page upload DISABLED for the current event.")

        self.stdout.write(
            self.style.SUCCESS(
                "Phase 1 local reference data ready. Newly created this run: "
                f"{created['countries']} countr{'y' if created['countries'] == 1 else 'ies'}, "
                f"{created['sectors']} sector(s), {created['events']} event(s), "
                f"{created['legal_versions']} legal document version(s), "
                f"{created['topics']} interest topic(s). This is minimum synthetic "
                "data, never a production reference-data catalogue."
            )
        )
