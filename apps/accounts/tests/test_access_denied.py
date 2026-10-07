"""Operational access denial (UI/UX Completion Gate finding F3, decision D1).

* A signed-in operational user without the permission a page needs gets the
  localized 403 page -- never a redirect to sign-in, which used to loop
  forever because sign-in sends signed-in users back to an operations page.
* An anonymous user is still redirected to operational sign-in with the
  requested path as `next`; commands (POST) are never replayed, so they
  carry no `next`.
* After sign-in, `next` is honoured only for operations and checkpoint paths
  on this host; anything else falls back to the user's first permitted area.
* The 403 is decided before any object lookup, so it is identical for an
  existing and a non-existent object: it discloses nothing about scope.
"""

from __future__ import annotations

import re
import uuid

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accounts.tests.sign_in import staff_sign_in

pytestmark = pytest.mark.django_db

PASSWORD = "__access-denied-synthetic__"  # noqa: S105
SIGN_IN = reverse("accounts:operational-sign-in")


def _user(email: str, *permissions: str) -> OperationalUser:
    user = OperationalUser.objects.create_user(
        email=email, password=PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    if permissions:
        group = Group.objects.create(name=f"access-denied {email}")
        for dotted in permissions:
            app_label, codename = dotted.split(".")
            group.permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        ScopedGroupMembership.objects.create(user=user, group=group, granted_by=user)
    return user


def _sign_in(user: OperationalUser, *, next_url: str | None = None) -> tuple[Client, object]:
    client = Client()
    response = staff_sign_in(client, user.email_normalized, PASSWORD, next_url=next_url)
    assert response.status_code == 302
    return client, response


def _registration():
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.reviews.tests.conftest import make_registration

    event = EventEdition.objects.create(
        code=f"F3{uuid.uuid4().hex[:6].upper()}",
        name="Synthetic F3 Event",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    return make_registration(event=event)


def _language(client: Client, code: str) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = code


# ---------------------------------------------------------------------------
# Authenticated without permission -> 403, no loop
# ---------------------------------------------------------------------------


def test_signed_in_user_without_permission_gets_403_not_a_redirect() -> None:
    client, _ = _sign_in(_user("f3-badge-only@example.test", "badges.view_digitalentrypass"))
    response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 403
    assert "Location" not in response
    assert "403.html" in [t.name for t in response.templates]
    content = response.content.decode()
    assert "You do not have access to this page" in content
    assert 'class="asc-ui asc-public asc-error-page"' in content
    assert "img/brand/asc-logo.svg" in content


def test_no_redirect_loop_from_sign_in_to_an_area_without_permission() -> None:
    """The exact F3 reproduction: a user whose default destination used to
    be refused now ends on a 403 page after at most one redirect."""
    client, _ = _sign_in(_user("f3-loop@example.test"))
    response = client.get(SIGN_IN, follow=True)
    assert len(response.redirect_chain) <= 1
    assert response.status_code == 403
    response = client.get(reverse("registrations:ops-intake-list"), follow=True)
    assert response.redirect_chain == []
    assert response.status_code == 403


@pytest.mark.parametrize(
    "url_name",
    [
        "registrations:ops-intake-list",
        "reviews:queue-list",
        "invitations:workspace-dashboard",
        "accreditation:bulk-preview",
        "communications:operations-messages",
        "exports:workspace",
        "badges:verification-keys",
    ],
)
def test_every_gated_area_answers_403_to_a_signed_in_user_without_permission(url_name) -> None:
    client, _ = _sign_in(_user(f"f3-{url_name.replace(':', '-')}@example.test"))
    response = client.get(reverse(url_name))
    assert response.status_code == 403
    assert "Location" not in response


def test_403_is_identical_for_existing_and_non_existent_objects() -> None:
    registration = _registration()
    client, _ = _sign_in(_user("f3-scope@example.test"))
    existing = client.get(reverse("registrations:ops-intake-detail", args=[registration.pk]))
    missing = client.get(reverse("registrations:ops-intake-detail", args=[uuid.uuid4()]))
    assert existing.status_code == missing.status_code == 403

    def normalised(response, requested_pk) -> str:
        # Only per-render CSRF tokens, the session-expiry deadlines and the
        # requested path itself (the language switcher's return address)
        # may differ.
        content = response.content.decode().replace(str(requested_pk), "<pk>")
        content = re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", content)
        return re.sub(r'data-(inactivity|absolute)-deadline="\d+"', "", content)

    missing_pk = missing.wsgi_request.path.rstrip("/").rsplit("/", 1)[-1]
    assert normalised(existing, registration.pk) == normalised(missing, missing_pk)
    assert registration.public_reference not in existing.content.decode()


def test_signed_in_command_without_permission_is_refused_with_403() -> None:
    registration = _registration()
    client, _ = _sign_in(_user("f3-command@example.test"))
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403
    response = client.post(reverse("reviews:case-assign", args=[uuid.uuid4()]), {})
    assert response.status_code == 403


def test_permissions_stay_fail_closed_for_an_inactive_membership() -> None:
    from apps.accounts.models import ScopedGroupMembershipStatus

    user = _user("f3-suspended@example.test", "reviews.view_reviewcase")
    client, _ = _sign_in(user)
    assert client.get(reverse("reviews:queue-list")).status_code == 200
    ScopedGroupMembership.objects.filter(user=user).update(
        status=ScopedGroupMembershipStatus.SUSPENDED
    )
    assert client.get(reverse("reviews:queue-list")).status_code == 403


@pytest.mark.parametrize(
    ("language", "direction", "title"),
    [
        ("en", "ltr", "You do not have access to this page"),
        ("fr", "ltr", "Vous n’avez pas accès à cette page"),
        ("ar", "rtl", "ليس لديك صلاحية الوصول إلى هذه الصفحة"),
    ],
)
def test_403_page_is_translated_and_directional(language, direction, title) -> None:
    client, _ = _sign_in(_user(f"f3-lang-{language}@example.test"))
    _language(client, language)
    response = client.get(reverse("reviews:queue-list"))
    assert response.status_code == 403
    content = response.content.decode()
    assert f'<html lang="{language}" dir="{direction}">' in content
    assert title in content


def test_403_page_offers_the_operations_home_and_sign_out() -> None:
    client, _ = _sign_in(_user("f3-home@example.test", "reviews.view_reviewcase"))
    response = client.get(reverse("exports:workspace"))
    assert response.status_code == 403
    content = response.content.decode()
    assert f'href="{reverse("reviews:queue-list")}"' in content
    assert f'action="{reverse("accounts:operational-sign-out")}"' in content


# ---------------------------------------------------------------------------
# Anonymous -> sign-in with a safe next
# ---------------------------------------------------------------------------


def test_anonymous_get_is_redirected_to_sign_in_with_next() -> None:
    target = reverse("reviews:queue-list")
    response = Client().get(target)
    assert response.status_code == 302
    assert response["Location"] == f"{SIGN_IN}?next={target}"


def test_anonymous_command_is_redirected_without_next() -> None:
    response = Client().post(reverse("reviews:case-assign", args=[uuid.uuid4()]), {})
    assert response.status_code == 302
    assert response["Location"] == SIGN_IN


def test_anonymous_accreditation_command_is_redirected_to_sign_in() -> None:
    response = Client().post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": uuid.uuid4(), "kind": "PARTICIPANT_ROLE"},
        )
    )
    assert response.status_code == 302
    assert response["Location"] == SIGN_IN


def test_safe_next_is_honoured_after_sign_in() -> None:
    user = _user("f3-next@example.test", "reviews.view_reviewcase")
    target = reverse("reviews:queue-list") + "?status=QUEUED"
    _client, response = _sign_in(user, next_url=target.replace("?", "%3F").replace("=", "%3D"))
    assert response["Location"] == target


def test_next_to_a_refused_area_ends_on_403_without_a_loop() -> None:
    user = _user("f3-next-refused@example.test", "reviews.view_reviewcase")
    client, response = _sign_in(user, next_url=reverse("exports:workspace"))
    assert response["Location"] == reverse("exports:workspace")
    final = client.get(response["Location"], follow=True)
    assert final.redirect_chain == []
    assert final.status_code == 403


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "https://attacker.example/ops/",
        "//attacker.example/ops/",
        "/\\attacker.example/ops/",
        "/ops/../accounts/ops/sign-out/",
        "/accounts/ops/sign-in/",
        "/workspace/",
        "/register/identity/",
        "javascript:alert(1)",
        "ops/reviews/queue/",
    ],
)
def test_unsafe_next_falls_back_to_the_first_permitted_area(unsafe_next) -> None:
    user = _user(f"f3-unsafe-{uuid.uuid4().hex[:8]}@example.test", "reviews.view_reviewcase")
    client = Client()
    response = staff_sign_in(client, user.email_normalized, PASSWORD, next=unsafe_next)
    assert response.status_code == 302
    assert response["Location"] == reverse("reviews:queue-list")


def test_default_destination_is_the_first_permitted_area() -> None:
    _client, response = _sign_in(_user("f3-default@example.test", "exports.add_exportrequest"))
    assert response["Location"] == reverse("exports:workspace")


def test_signed_in_visit_to_sign_in_goes_to_the_first_permitted_area() -> None:
    client, _ = _sign_in(_user("f3-revisit@example.test", "reviews.view_reviewcase"))
    response = client.get(SIGN_IN)
    assert response.status_code == 302
    assert response["Location"] == reverse("reviews:queue-list")
