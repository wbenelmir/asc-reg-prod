"""No clear identity value, QR token, search string, device secret, or
activation code reaches a log line, an audit row, or an entry record."""

from __future__ import annotations

import json
import logging

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.entry.models import EntryEvent
from apps.entry.services.decisions import pending_from_assessment, record_entry_decision
from apps.entry.services.verification import (
    lookup_identity,
    lookup_reference,
    manual_search,
    verify_qr,
)
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db

SYNTHETIC_NIN = "109870123456789012"
SYNTHETIC_PASSPORT = "ZX9087654"
SYNTHETIC_QUERY = "Synthetic Participant"


def _everything_persisted() -> str:
    audit = list(
        AuditEvent.objects.values(
            "action_code", "reason_code", "before_summary", "after_summary", "target_type"
        )
    )
    events = list(EntryEvent.objects.values())
    return json.dumps({"audit": audit, "events": events}, default=str)


def _all_log_text(caplog) -> str:
    return "\n".join(
        f"{record.getMessage()} {record.exc_text or ''} {record.__dict__}"
        for record in caplog.records
    )


def test_identity_values_never_reach_logs_audit_or_events(
    caplog, checkpoint, supervisor_checkpoint, person, active_pass
):
    caplog.set_level(logging.DEBUG)
    factories.add_identifier(
        person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
    )
    factories.add_identifier(
        person=person, identifier_type="PASSPORT", country="FR", value=SYNTHETIC_PASSPORT
    )
    nin = lookup_identity(checkpoint=supervisor_checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
    lookup_identity(
        checkpoint=supervisor_checkpoint,
        method="PASSPORT",
        raw_value=SYNTHETIC_PASSPORT,
        country_code="FR",
    )
    lookup_identity(checkpoint=supervisor_checkpoint, method="NIN", raw_value="000000000000000000")
    manual_search(checkpoint=supervisor_checkpoint, raw_query=SYNTHETIC_QUERY)
    lookup_reference(checkpoint=supervisor_checkpoint, raw_reference="ENT-DOESNOTEXIST")
    pending = pending_from_assessment(
        assessment=nin.assessment, checkpoint=supervisor_checkpoint, method="NIN"
    )
    record_entry_decision(checkpoint=supervisor_checkpoint, pending=pending, decision="ADMIT")

    persisted = _everything_persisted()
    logs = _all_log_text(caplog)
    for secret in (SYNTHETIC_NIN, SYNTHETIC_PASSPORT, SYNTHETIC_QUERY, "000000000000000000"):
        assert secret not in persisted, secret
        assert secret not in logs, secret


def test_qr_token_never_reaches_logs_or_audit_even_on_failure(
    caplog, checkpoint, active_pass, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    token = factories.token_for(active_pass)
    verify_qr(checkpoint=checkpoint, raw_token=token)
    verify_qr(checkpoint=checkpoint, raw_token=token + "tampered")

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr("apps.badges.credentials.verify.verify_pass_token", boom)
    verify_qr(checkpoint=checkpoint, raw_token=token)
    assert token not in _everything_persisted()
    assert token not in _all_log_text(caplog)


def test_device_secret_and_activation_code_never_reach_logs_or_audit(
    caplog, event, layout, device_admin
):
    from datetime import timedelta

    from django.utils import timezone

    from apps.entry.services.devices import enroll_device_with_code, register_device

    caplog.set_level(logging.DEBUG)
    registration = register_device(
        event_edition=event,
        public_name="Redaction tablet",
        expires_at=timezone.now() + timedelta(days=2),
        venue=layout.venue,
        gate=layout.gate_a,
        zones=[layout.main],
        verification_methods=["QR"],
        actor=device_admin,
    )
    enrollment = enroll_device_with_code(raw_code=registration.activation_code, actor=device_admin)
    persisted = _everything_persisted()
    logs = _all_log_text(caplog)
    for secret in (registration.activation_code, enrollment.device_secret):
        assert secret not in persisted
        assert secret not in logs


def test_http_lookup_does_not_log_the_submitted_value(
    caplog, client, operator, device_and_secret, layout, person, active_pass
):
    from django.conf import settings

    caplog.set_level(logging.DEBUG)
    factories.add_identifier(
        person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
    )
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": operator.email_normalized, "password": factories.TEST_PASSWORD},
    )
    client.cookies[settings.ENTRY_DEVICE_COOKIE_NAME] = device_and_secret[1]
    client.post(reverse("entry:home"), {"action": "start", "zone_id": str(layout.main.pk)})
    response = client.post(
        reverse("entry:lookup-identity"), {"method": "NIN", "value": SYNTHETIC_NIN}
    )
    assert response.status_code == 200
    assert SYNTHETIC_NIN not in _all_log_text(caplog)
    assert SYNTHETIC_NIN not in _everything_persisted()
    assert device_and_secret[1] not in _all_log_text(caplog)
