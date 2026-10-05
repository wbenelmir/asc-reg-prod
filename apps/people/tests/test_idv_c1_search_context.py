"""IDV-C1, R-IDV-05: identity search text never travels in a URL.

The queue search is a CSRF-protected POST. The server resolves it once into a
bounded set of case ids and keeps them in the reviewer's server-side session
under a random reference; links, tabs, pagination, the review screen and the
decision redirects carry only that reference (`ctx`). No search text and no
NIN is stored in the session, a cookie or a URL. The reference is bound to the
reviewer and the session, expires, can be cleared, and scope (and, for a NIN
search, the evidence permission) is applied again on every use. A legacy
`?q=` address is redirected without being used.

Synthetic identities only.
"""

from __future__ import annotations

import datetime
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.people.tests.conftest import (
    SIM_MATCH_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    staff_client,
    submit_case,
    upload_national_id_card,
)
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db

URL_ATTRIBUTE = re.compile(r'(?:href|action|src|data-[a-z-]*url)="([^"]*)"')


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "idv-c1-search@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def cases(idv_event, legal_versions):
    registrations = [
        submit_case(
            idv_event,
            legal_versions,
            nin=f"12345678901234561{index}",
            given="Amina",
            family=f"Search{chr(65 + index)}",
        )
        for index in range(3)
    ]
    submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    process_all()
    for registration in registrations:
        upload_national_id_card(registration)
    return [case_for(registration) for registration in registrations]


def _search(client, text: str, **filters):
    return client.post(
        reverse("identity:search"), {"q": text, "status": "MANUAL_REVIEW", **filters}
    )


def _ctx(location: str) -> str:
    return parse_qs(urlsplit(location).query)["ctx"][0]


def _urls(content: str) -> list[str]:
    return URL_ATTRIBUTE.findall(content)


def _post_verify(client, case, **extra):
    card = case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    return client.post(
        reverse("identity:verify", kwargs={"pk": case.pk}),
        {
            "expected_version": case.version,
            "after_changed_at": case.status_changed_at.isoformat(),
            "after_pk": str(case.pk),
            "evidence_document": str(card.pk),
            "reason_code": "DOCUMENT_MATCHES_SUBMISSION",
            "note": "",
            "advance": "next",
            "status": "MANUAL_REVIEW",
            **extra,
        },
    )


def test_the_queue_never_takes_a_search_from_the_address(reviewer, cases) -> None:
    nin = cases[1].current_revision.identifier.value_encrypted
    response = staff_client(reviewer).get(
        reverse("identity:queue") + f"?status=MANUAL_REVIEW&q={nin}"
    )
    assert response.status_code == 302
    assert nin not in response["Location"] and "q=" not in response["Location"]


def test_a_nin_search_keeps_the_nin_out_of_every_url(reviewer, cases) -> None:
    target = cases[1]
    nin = target.current_revision.identifier.value_encrypted
    client = staff_client(reviewer)

    response = _search(client, nin)

    assert response.status_code == 302
    location = response["Location"]
    assert nin not in location
    ctx = _ctx(location)
    content = client.get(location).content.decode()
    assert target.registration.public_reference in content
    assert cases[0].registration.public_reference not in content  # narrowed
    assert nin not in content  # never echoed back into the page
    assert all(nin not in url for url in _urls(content))
    case_url = reverse("identity:case", kwargs={"pk": target.pk})
    assert f"{case_url}?" in content and f"ctx={ctx}" in content
    review_page = client.get(f"{case_url}?status=MANUAL_REVIEW&ctx={ctx}").content.decode()
    assert all(nin not in url for url in _urls(review_page))
    assert f'name="ctx" value="{ctx}"' in review_page  # decisions keep the queue position
    decided = _post_verify(client, target, ctx=ctx)
    assert decided.status_code == 302
    assert nin not in decided["Location"]


def test_a_name_search_is_kept_out_of_urls_too(reviewer, cases) -> None:
    client = staff_client(reviewer)
    location = _search(client, "SearchB")["Location"]
    assert "SearchB" not in location
    content = client.get(location).content.decode()
    assert cases[1].registration.public_reference in content
    assert cases[0].registration.public_reference not in content
    assert all("SearchB" not in url for url in _urls(content))


def test_the_search_context_holds_no_search_text(reviewer, cases) -> None:
    from django.contrib.sessions.models import Session

    nin = cases[1].current_revision.identifier.value_encrypted
    client = staff_client(reviewer)
    _search(client, nin)
    stored = Session.objects.get(session_key=client.session.session_key)
    assert nin not in stored.session_data
    assert nin not in repr(dict(stored.get_decoded()))
    assert all(nin not in morsel.value for morsel in client.cookies.values())


def test_a_context_is_bound_to_its_reviewer(reviewer, cases, idv_event) -> None:
    nin = cases[1].current_revision.identifier.value_encrypted
    location = _search(staff_client(reviewer), nin)["Location"]
    other = make_staff(
        "idv-c1-other@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )

    content = staff_client(other).get(location).content.decode()

    assert "data-idv-search-expired" in content  # not applied for anyone else
    assert cases[0].registration.public_reference in content


def test_a_context_applies_scope_and_the_evidence_permission_again(reviewer, cases) -> None:
    from apps.accounts.models import ScopedGroupMembership

    nin = cases[1].current_revision.identifier.value_encrypted
    client = staff_client(reviewer)
    location = _search(client, nin)["Location"]
    assert cases[1].registration.public_reference in client.get(location).content.decode()
    ScopedGroupMembership.objects.filter(user=reviewer).update(
        event_edition=make_event("IDVC1ELSEWHERE")
    )

    content = client.get(location).content.decode()

    assert cases[1].registration.public_reference not in content


def test_a_nin_search_needs_the_evidence_permission(cases, idv_event) -> None:
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name="IDV C1 viewer only")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="people", codename="view_identityverification"
        )
    )
    viewer = make_staff("idv-c1-viewer@example.test", "IDV C1 viewer only", event=idv_event)
    client = staff_client(viewer)
    location = _search(client, cases[1].current_revision.identifier.value_encrypted)["Location"]
    assert cases[1].registration.public_reference not in client.get(location).content.decode()


def test_a_context_expires_and_can_be_cleared(reviewer, cases) -> None:
    from apps.people.search_context import SEARCH_SESSION_KEY

    nin = cases[1].current_revision.identifier.value_encrypted
    client = staff_client(reviewer)
    location = _search(client, nin)["Location"]
    ctx = _ctx(location)
    session = client.session
    contexts = session[SEARCH_SESSION_KEY]
    contexts[ctx]["created"] = (timezone.now() - datetime.timedelta(hours=2)).isoformat()
    session[SEARCH_SESSION_KEY] = contexts
    session.save()
    assert "data-idv-search-expired" in client.get(location).content.decode()
    assert ctx not in client.session.get(SEARCH_SESSION_KEY, {})  # forgotten once expired

    fresh = _ctx(_search(client, nin)["Location"])
    response = client.post(
        reverse("identity:search"), {"action": "clear", "ctx": fresh, "status": "MANUAL_REVIEW"}
    )
    assert response.status_code == 302 and "ctx=" not in response["Location"]
    assert fresh not in client.session.get(SEARCH_SESSION_KEY, {})


def test_the_search_endpoint_refuses_get_and_people_without_the_permission(cases) -> None:
    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME

    intake = make_staff("idv-c1-intake@example.test", REGISTRATION_INTAKE_GROUP_NAME)
    assert staff_client(intake).post(reverse("identity:search"), {"q": "x"}).status_code in (
        302,
        403,
    )
    reviewer = make_staff("idv-c1-get@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)
    assert staff_client(reviewer).get(reverse("identity:search")).status_code == 405
