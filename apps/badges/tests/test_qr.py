"""Real QR rendering, the authenticated image endpoint, and a decode round trip.

The decode test is the one that actually matters: encoding "something" proves
nothing if a scanner at a gate cannot read it back. `pyzbar` here plays the
role of the scanner, and the assertion is byte equality with the issued
compact JWS -- not a substring, not a prefix.
"""

from __future__ import annotations

import io

import pytest
from django.urls import reverse

from apps.badges.credentials.qr import (
    ERROR_CORRECTION,
    QUIET_ZONE_MODULES,
    QrRenderError,
    render_png,
    render_svg,
    symbol_version,
)
from apps.badges.models import DigitalEntryPassStatus
from apps.badges.services import (
    activate_pass,
    generate_pass,
    issue_pass_token,
    new_operation_id,
)

pytestmark = pytest.mark.django_db


def _activated(registration, actor):
    credential = generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return credential


def _sign_in_participant(client, person):
    from django.utils import timezone

    from apps.accounts import participant_auth, session_expiry

    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()


def _decode(png_bytes: bytes) -> list[str]:
    """Decode a PNG QR image the way a scanner would."""
    from PIL import Image
    from pyzbar.pyzbar import decode

    results = decode(Image.open(io.BytesIO(png_bytes)))
    return [result.data.decode("ascii") for result in results if result.type == "QRCODE"]


# ---------------------------------------------------------------------------
# Encoder behaviour
# ---------------------------------------------------------------------------


def test_the_renderer_produces_a_png(eligible_registration, pass_admin, active_key):
    credential = _activated(eligible_registration, pass_admin)
    image = render_png(issue_pass_token(credential))
    assert image.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_renderer_produces_an_svg(eligible_registration, pass_admin, active_key):
    credential = _activated(eligible_registration, pass_admin)
    image = render_svg(issue_pass_token(credential))
    assert b"<svg" in image


def test_a_standards_compliant_quiet_zone_is_used():
    """Four modules. Scanners fail intermittently without it."""
    assert QUIET_ZONE_MODULES == 4
    assert ERROR_CORRECTION == "m"


def test_an_empty_token_is_refused():
    with pytest.raises(QrRenderError):
        render_png("")


def test_the_symbol_stays_a_reasonable_size(eligible_registration, pass_admin, active_key):
    """Guard the symbol size, because it drives real scan reliability.

    A ~380-character token at error-correction M encodes to QR version 16
    (81x81 modules). Rendered at 320 CSS pixels with a 4-module quiet zone
    that is roughly 3.6 device pixels per module, which scans reliably from a
    phone screen and in print.

    The bound is 20 rather than "whatever it is today": a future claim added
    to the payload would push the version up, and this test is what forces
    that trade-off to be looked at rather than discovered at a gate.
    """
    credential = _activated(eligible_registration, pass_admin)
    version = symbol_version(issue_pass_token(credential))
    assert version <= 20, f"QR version {version} is too large to scan reliably"


# ---------------------------------------------------------------------------
# The decode round trip -- the assertion that proves it is really scannable
# ---------------------------------------------------------------------------


def test_the_rendered_qr_decodes_back_to_the_exact_issued_token(
    eligible_registration, pass_admin, active_key
):
    credential = _activated(eligible_registration, pass_admin)
    token = issue_pass_token(credential)

    decoded = _decode(render_png(token))

    assert len(decoded) == 1, "exactly one QR symbol must be present"
    assert decoded[0] == token


def test_the_endpoint_image_decodes_back_to_the_issued_token(
    client, eligible_registration, pass_admin, active_key, person
):
    """End-to-end: what the browser receives is what a scanner will read."""
    credential = _activated(eligible_registration, pass_admin)
    expected = issue_pass_token(credential)
    _sign_in_participant(client, person)

    response = client.get(
        reverse(
            "badges:participant-pass-qr",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"

    decoded = _decode(response.content)
    assert decoded == [expected]


# ---------------------------------------------------------------------------
# Endpoint authorization and status gating
# ---------------------------------------------------------------------------


def test_the_qr_endpoint_requires_a_participant_session(
    client, eligible_registration, pass_admin, active_key
):
    credential = _activated(eligible_registration, pass_admin)
    response = client.get(
        reverse(
            "badges:participant-pass-qr",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code in (302, 403, 404)


def test_another_participant_cannot_fetch_the_qr(
    client, eligible_registration, pass_admin, active_key
):
    from apps.people.models import Person, PersonStatus

    credential = _activated(eligible_registration, pass_admin)
    stranger = Person.objects.create(status=PersonStatus.ACTIVE)
    _sign_in_participant(client, stranger)

    response = client.get(
        reverse(
            "badges:participant-pass-qr",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "status",
    [
        DigitalEntryPassStatus.INACTIVE,
        DigitalEntryPassStatus.SUSPENDED,
        DigitalEntryPassStatus.REVOKED,
        DigitalEntryPassStatus.EXPIRED,
    ],
)
def test_the_qr_endpoint_refuses_every_non_active_status(
    client, eligible_registration, pass_admin, active_key, person, status
):
    credential = _activated(eligible_registration, pass_admin)
    credential.status = status
    credential.save(update_fields=["status"])
    _sign_in_participant(client, person)

    response = client.get(
        reverse(
            "badges:participant-pass-qr",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code == 404


def test_the_qr_response_is_never_cached(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    response = client.get(
        reverse(
            "badges:participant-pass-qr",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert "no-store" in response["Cache-Control"]
    assert response["X-Content-Type-Options"] == "nosniff"


def test_the_token_never_appears_in_the_qr_url(eligible_registration, pass_admin, active_key):
    credential = _activated(eligible_registration, pass_admin)
    token = issue_pass_token(credential)
    url = reverse("badges:participant-pass-qr", kwargs={"public_id": credential.series.public_id})
    assert token not in url
    assert credential.jti not in url
    assert credential.signature_hex not in url


# ---------------------------------------------------------------------------
# Template integration
# ---------------------------------------------------------------------------


def test_the_participant_page_renders_a_real_image_element(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    qr_url = reverse(
        "badges:participant-pass-qr", kwargs={"public_id": credential.series.public_id}
    )
    assert f'src="{qr_url}"' in body
    assert "<img" in body
    assert "alt=" in body
    # The raw credential must not be in the page at all any more.
    assert issue_pass_token(credential) not in body


def test_the_print_page_renders_a_real_image_element(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)
    body = client.get(
        reverse(
            "badges:participant-pass-print",
            kwargs={"public_id": credential.series.public_id},
        )
    ).content.decode()

    assert "<img" in body
    assert "pass-print-qr" in body
    assert issue_pass_token(credential) not in body


def test_no_template_comment_leaks_into_the_rendered_page(
    client, eligible_registration, pass_admin, active_key, person
):
    """Django's `{# #}` is single-line only; a multi-line one renders literally.

    That exact mistake shipped a paragraph of developer commentary onto the
    participant's pass page, so it is now asserted rather than eyeballed.
    """
    _activated(eligible_registration, pass_admin)
    _sign_in_participant(client, person)

    for url in (reverse("badges:participant-passes"),):
        body = client.get(url).content.decode()
        assert "{#" not in body
        assert "#}" not in body
        assert "{% comment" not in body
