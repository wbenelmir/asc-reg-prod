"""UI/UX Completion Gate, Checkpoint 1: presentation helpers.

* The operations section bar lists exactly the areas whose view gate the
  user passes: every section's permission is proven against the real view
  (a user holding only that permission gets the page; a user without it gets
  the 403 page), so the navigation can never drift from the decorators.
* Status chips have a tone for every stored status value.
* The wizard stepper mirrors the service's step order.
* `asc_control` / `ltr_isolate` only add presentation attributes.
"""

from __future__ import annotations

import pytest
from django import forms
from django.contrib.auth.models import Group, Permission
from django.template import Context, Template
from django.test import Client
from django.urls import reverse

from apps.core.navigation import OPERATIONS_SECTIONS, operations_navigation
from apps.core.templatetags.asc_ui import _STATUS_TONES, asc_control, ltr_isolate, status_tone

PASSWORD = "__ui-foundation-synthetic__"  # noqa: S105


def _user(email: str, *, permissions: tuple[str, ...] = (), superuser: bool = False):
    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

    if superuser:
        user = OperationalUser.objects.create_superuser(email=email, password=PASSWORD)
        user.status = OperationalUserStatus.ACTIVE
        user.save(update_fields=["status"])
        return user
    user = OperationalUser.objects.create_user(
        email=email, password=PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    if permissions:
        group = Group.objects.create(name=f"ui-foundation {email}")
        for dotted in permissions:
            app_label, codename = dotted.split(".")
            group.permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        ScopedGroupMembership.objects.create(user=user, group=group, granted_by=user)
    return user


# ---------------------------------------------------------------------------
# Operations navigation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_anonymous_user_gets_no_sections() -> None:
    from django.contrib.auth.models import AnonymousUser

    assert operations_navigation(AnonymousUser()) == []


@pytest.mark.django_db
def test_superuser_sees_every_section_in_display_order() -> None:
    user = _user("nav-super@example.test", superuser=True)
    keys = [item.key for item in operations_navigation(user)]
    assert keys == [section.key for section in OPERATIONS_SECTIONS]


@pytest.mark.django_db
def test_user_sees_only_the_sections_their_permissions_open() -> None:
    user = _user("nav-reviewer@example.test", permissions=("reviews.view_reviewcase",))
    items = operations_navigation(user, view_name="reviews:case-detail", namespace="reviews")
    assert [item.key for item in items] == ["reviews"]
    assert items[0].current is True
    assert items[0].url == reverse("reviews:queue-list")


@pytest.mark.django_db
def test_user_without_any_membership_sees_nothing() -> None:
    user = _user("nav-none@example.test")
    assert operations_navigation(user) == []


@pytest.mark.django_db
def test_suspended_membership_hides_the_section() -> None:
    from apps.accounts.models import ScopedGroupMembership, ScopedGroupMembershipStatus

    user = _user("nav-revoked@example.test", permissions=("reviews.view_reviewcase",))
    ScopedGroupMembership.objects.filter(user=user).update(
        status=ScopedGroupMembershipStatus.SUSPENDED
    )
    assert operations_navigation(user) == []


@pytest.mark.django_db
@pytest.mark.parametrize("section", OPERATIONS_SECTIONS, ids=lambda section: section.key)
def test_each_section_permission_is_exactly_its_view_gate(section) -> None:
    """Holding only the section's permission opens its page; holding every
    other section's permission but not this one does not."""
    allowed = _user(f"nav-allowed-{section.key}@example.test", permissions=(section.permission,))
    client = _signed_in(allowed)
    response = client.get(reverse(section.url_name))
    assert response.status_code == 200, (section.key, response.status_code)

    others = tuple(s.permission for s in OPERATIONS_SECTIONS if s.permission != section.permission)
    denied = _user(f"nav-denied-{section.key}@example.test", permissions=others)
    client = _signed_in(denied)
    response = client.get(reverse(section.url_name))
    # A signed-in user without the permission gets the 403 page, never a
    # redirect back to sign-in (UI/UX Completion Gate F3).
    assert response.status_code == 403
    assert "Location" not in response


def _signed_in(user) -> Client:
    """A client signed in through the real operational sign-in view, so the
    session clocks the expiry middleware checks are established as usual."""
    client = Client()
    response = client.post(
        reverse("accounts:operational-sign-in"),
        {"email": user.email_normalized, "password": PASSWORD},
    )
    assert response.status_code == 302
    return client


@pytest.mark.django_db
def test_operations_shell_renders_only_permitted_links() -> None:
    user = _user("nav-shell@example.test", permissions=("reviews.view_reviewcase",))
    client = _signed_in(user)
    content = client.get(reverse("reviews:queue-list")).content.decode()
    assert 'data-section="reviews"' in content
    assert 'aria-current="page" data-section="reviews"' in content
    for section in OPERATIONS_SECTIONS:
        if section.key != "reviews":
            assert f'data-section="{section.key}"' not in content
    # The brand link points at the first permitted area, never at a section
    # this user would be bounced from.
    assert f'class="asc-brand" href="{reverse("reviews:queue-list")}"' in content


# ---------------------------------------------------------------------------
# Status tones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "choices_path"),
    [
        ("registration", "apps.registrations.models.RegistrationPublicStatus"),
        ("processing", "apps.registrations.models.RegistrationInternalStatus"),
        ("review_case", "apps.reviews.models.ReviewCaseStatus"),
        # IDV-3: identity verification statuses (separate from participation).
        ("identity", "apps.people.models.IdentityStatus"),
    ],
)
def test_every_stored_status_has_a_tone_and_icon(family: str, choices_path: str) -> None:
    from django.utils.module_loading import import_string

    choices = import_string(choices_path)
    assert set(_STATUS_TONES[family]) == set(choices.values)
    for value in choices.values:
        tone, icon = status_tone(family, value)
        assert tone in {"success", "info", "warning", "danger", "neutral"}
        assert icon


def test_unknown_status_is_neutral_and_still_labelled() -> None:
    assert status_tone("registration", "SOMETHING_NEW") == ("neutral", "info")
    html = Template('{% load asc_ui %}{% status_chip "registration" "X" "Label" %}').render(
        Context({})
    )
    assert "asc-chip-neutral" in html
    assert "<span>Label</span>" in html
    assert 'aria-hidden="true"' in html


# ---------------------------------------------------------------------------
# Wizard stepper
# ---------------------------------------------------------------------------


def test_stepper_order_mirrors_the_service_step_order() -> None:
    from apps.registrations.services import _STEP_ORDER
    from apps.registrations.templatetags.registration_ui import WIZARD_STEPS

    assert [code for code, _label, _url in WIZARD_STEPS] == _STEP_ORDER


def test_stepper_states_for_a_participant_editing_an_earlier_step() -> None:
    from apps.registrations.templatetags.registration_ui import wizard_step_states

    states = [step["state"] for step in wizard_step_states("contact", "interests")]
    assert states == ["done", "current", "done", "available", "upcoming", "upcoming"]


def test_stepper_never_links_a_step_beyond_the_furthest_reached() -> None:
    from apps.registrations.templatetags.registration_ui import wizard_step_states

    steps = wizard_step_states("identity", "identity")
    assert [step["state"] for step in steps] == ["current"] + ["upcoming"] * 5
    html = Template(
        '{% load registration_ui %}{% wizard_stepper registration "identity" %}'
    ).render(Context({"registration": type("R", (), {"current_step": "identity"})()}))
    assert html.count("<a ") == 0
    assert 'aria-current="step"' in html


# ---------------------------------------------------------------------------
# asc_control / ltr_isolate
# ---------------------------------------------------------------------------


class _SampleForm(forms.Form):
    email = forms.EmailField(label="Email", help_text="Help")
    code = forms.CharField(label="Code", widget=forms.TextInput(attrs={"class": "form-control"}))
    agree = forms.BooleanField(label="Agree", required=False)
    kind = forms.ChoiceField(choices=[("a", "A")])


def test_asc_control_adds_class_ids_and_direction_without_duplicates() -> None:
    form = _SampleForm(data={"email": "not-an-email", "code": "", "kind": "a"})
    assert not form.is_valid()
    email = str(asc_control(form["email"]))
    assert 'class="form-control"' in email
    assert 'dir="ltr"' in email
    assert 'aria-invalid="true"' in email
    assert 'aria-describedby="id_email_help id_email_error"' in email
    code = str(asc_control(form["code"], "ltr"))
    assert code.count("form-control") == 1
    assert 'dir="ltr"' in code
    agree = str(asc_control(form["agree"]))
    assert 'class="form-check-input"' in agree
    assert "aria-required" not in agree
    kind = str(asc_control(form["kind"]))
    assert 'class="form-select"' in kind
    # Never changes names or values.
    assert 'name="email"' in email and 'value="not-an-email"' in email


def test_ltr_isolate_escapes_and_isolates() -> None:
    assert str(ltr_isolate("a<b>@example.test")) == '<bdi dir="ltr">a&lt;b&gt;@example.test</bdi>'
    assert ltr_isolate("") == ""
