"""Verifies the Phase 2 Prompt 5 seed migration (`0004_seed_prompt5_templates`)
published EN/FR/AR versions for every new communication purpose.

Plain (non-`transaction=True`) `django_db`: the seeded rows are inserted
once by the migration that built the test database and are never flushed
between ordinary savepoint-rollback tests (unlike `transaction=True` tests
-- see the same caveat documented in
`apps.registrations.tests.test_confirmation`).
"""

from __future__ import annotations

import pytest

from apps.communications.models import CommunicationChannel, MessageTemplateVersionStatus
from apps.communications.purposes import CommunicationPurpose
from apps.communications.services import resolve_template_version

pytestmark = pytest.mark.django_db

_NEW_PURPOSES = (
    CommunicationPurpose.INFORMATION_REQUEST,
    CommunicationPurpose.INFORMATION_RESPONSE_CONFIRMATION,
    CommunicationPurpose.DECISION_STATUS,
    CommunicationPurpose.ACCOUNT_ACCESS,
)


@pytest.mark.parametrize("purpose_code", _NEW_PURPOSES)
@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_seed_migration_publishes_every_purpose_in_every_language(
    purpose_code: str, language: str
) -> None:
    version = resolve_template_version(purpose_code=purpose_code, language=language)
    assert version is not None
    assert version.language == language
    assert version.status == MessageTemplateVersionStatus.PUBLISHED
    assert version.template.channel == CommunicationChannel.EMAIL
    assert version.subject
    assert version.body
