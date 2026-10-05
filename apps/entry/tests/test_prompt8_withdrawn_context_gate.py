"""Phase 3 Prompt 8 (P8-06): online verification still denies a withdrawn or
operationally cancelled context.

No usable token is issued to the participant any more, but a QR obtained
earlier must keep producing the existing non-approved-registration denial,
through the real withdrawal and cancellation services. Synthetic data only.
"""

from __future__ import annotations

import pytest

from apps.badges.models import DigitalEntryPassStatus
from apps.badges.services import PassStateError, issue_pass_token
from apps.entry.models import EntryReasonCode, EntryResult
from apps.entry.services.verification import lookup_reference, verify_qr
from apps.entry.tests import factories
from apps.reviews.services import cancel_registration_operationally, withdraw_registration

pytestmark = pytest.mark.django_db


def _end(kind, registration, actor):
    registration.refresh_from_db()
    if kind == "withdrawal":
        withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration.version,
        )
    else:
        cancel_registration_operationally(
            registration=registration,
            expected_version=registration.version,
            reason="SYNTHETIC_OPERATIONAL_CANCELLATION",
            actor=actor,
        )


@pytest.mark.parametrize("kind", ["withdrawal", "cancellation"])
def test_a_qr_held_from_before_is_still_denied_as_not_approved(
    kind, checkpoint, active_pass, registration, staff
):
    token = factories.token_for(active_pass)
    _end(kind, registration, staff)

    outcome = verify_qr(checkpoint=checkpoint, raw_token=token)

    assert outcome.result == EntryResult.DENIED
    assert outcome.reason_code == EntryReasonCode.REGISTRATION_NOT_APPROVED
    assert not outcome.assessment.may_be_overridden
    # The credential itself is untouched: history is preserved.
    active_pass.refresh_from_db()
    assert active_pass.status == DigitalEntryPassStatus.ACTIVE
    # And no fresh usable token can be obtained any more.
    with pytest.raises(PassStateError):
        issue_pass_token(active_pass)


@pytest.mark.parametrize("kind", ["withdrawal", "cancellation"])
def test_a_reference_lookup_no_longer_resolves_the_context(
    kind, checkpoint, active_pass, registration, staff
):
    _end(kind, registration, staff)
    outcome = lookup_reference(checkpoint=checkpoint, raw_reference=registration.public_reference)
    assert outcome.reason_code == EntryReasonCode.NO_MATCH
