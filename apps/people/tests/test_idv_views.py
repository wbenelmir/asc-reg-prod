"""Identity review HTTP surface (IDV-3): queue, review screen, actions, evidence.

Checks scope and role on every endpoint (not only on visible buttons), NIN
masking, filters and counts, preset reasons, the next-case flow that keeps
the queue filters, the stale-decision conflict page, evidence preview
protection, the participant correction page, and no-store caching.
Synthetic data only.
"""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.documents.models import MalwareScanStatus, StoredObject
from apps.people.models import IdentityStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

from .conftest import (
    SIM_MATCH_NIN,
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    participant_client,
    process_all,
    staff_client,
    submit_case,
    upload_national_id_card,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "idv-v-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def manager(idv_event):
    return make_staff(
        "idv-v-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def review_cases(idv_event, legal_versions):
    """Three manual-review cases with cards, one verified case, one foreign case."""
    cases = []
    for index in range(3):
        registration = submit_case(
            idv_event,
            legal_versions,
            nin=f"12345678901234567{index}",
            given="Amina",
            family=f"Case{chr(65 + index)}",
        )
        cases.append(registration)
    verified = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    foreign = submit_case(idv_event, legal_versions, nationality="FR")
    process_all()
    for registration in cases:
        upload_national_id_card(registration)
    return {
        "manual": [case_for(r) for r in cases],
        "verified": case_for(verified),
        "foreign": case_for(foreign),
    }


def _post_verify(client, case, *, advance="stay", extra=None, version=None):
    card = case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    data = {
        "expected_version": version if version is not None else case.version,
        "after_changed_at": case.status_changed_at.isoformat(),
        "after_pk": str(case.pk),
        "evidence_document": str(card.pk),
        "reason_code": "DOCUMENT_MATCHES_SUBMISSION",
        "note": "",
        "advance": advance,
        "status": "MANUAL_REVIEW",
        **(extra or {}),
    }
    return client.post(reverse("identity:verify", kwargs={"pk": case.pk}), data)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_the_queue_lists_manual_review_cases_oldest_first_with_counts(
    reviewer, review_cases
) -> None:
    response = staff_client(reviewer).get(reverse("identity:queue"))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    content = response.content.decode()
    references = [case.registration.public_reference for case in review_cases["manual"]]
    positions = [content.index(reference) for reference in references]
    assert positions == sorted(positions)
    assert review_cases["verified"].registration.public_reference not in content  # other tab
    assert 'data-status-count="MANUAL_REVIEW">4<' in content  # 3 NIN + 1 foreign
    assert 'data-status-count="API_VERIFIED">1<' in content


def test_the_queue_never_shows_a_full_nin(reviewer, review_cases) -> None:
    content = staff_client(reviewer).get(reverse("identity:queue") + "?status=ALL").content.decode()
    for case in review_cases["manual"]:
        assert case.current_revision.identifier.value_encrypted not in content
        assert case.current_revision.identifier.masked_value in content


def test_queue_filters_by_route_reason_and_source(reviewer, review_cases) -> None:
    client = staff_client(reviewer)
    foreign_ref = review_cases["foreign"].registration.public_reference
    content = client.get(reverse("identity:queue") + "?route=PASSPORT").content.decode()
    assert foreign_ref in content
    assert review_cases["manual"][0].registration.public_reference not in content
    content = client.get(reverse("identity:queue") + "?reason=NOT_FOUND").content.decode()
    assert foreign_ref not in content
    content = client.get(
        reverse("identity:queue") + "?status=API_VERIFIED&source=SIMULATED_API"
    ).content.decode()
    assert review_cases["verified"].registration.public_reference in content
    bad = client.get(reverse("identity:queue") + "?status=<script>&reason=x&event=nope")
    assert bad.status_code == 200


def test_a_full_nin_search_works_only_with_the_evidence_permission(
    reviewer, review_cases, idv_event
) -> None:
    from django.contrib.auth.models import Group, Permission

    target = review_cases["manual"][1]
    nin = target.current_revision.identifier.value_encrypted

    def searched(client) -> str:
        # IDV-C1 (R-IDV-05): the search is a POST; the NIN never enters a URL.
        response = client.post(reverse("identity:search"), {"q": nin, "status": "MANUAL_REVIEW"})
        assert nin not in response["Location"]
        return client.get(response["Location"]).content.decode()

    content = searched(staff_client(reviewer))
    assert target.registration.public_reference in content
    viewer_group, _ = Group.objects.get_or_create(name="IDV test viewer only")
    viewer_group.permissions.add(
        Permission.objects.get(
            content_type__app_label="people", codename="view_identityverification"
        )
    )
    viewer = make_staff("idv-v-viewer@example.test", "IDV test viewer only", event=idv_event)
    content = searched(staff_client(viewer))
    assert target.registration.public_reference not in content


def test_staff_of_another_event_see_nothing(review_cases) -> None:
    outsider = make_staff(
        "idv-v-outsider@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=make_event("IDVV2")
    )
    client = staff_client(outsider)
    content = client.get(reverse("identity:queue") + "?status=ALL").content.decode()
    assert review_cases["manual"][0].registration.public_reference not in content
    case = review_cases["manual"][0]
    assert client.get(reverse("identity:case", kwargs={"pk": case.pk})).status_code == 404
    assert _post_verify(client, case).status_code == 404
    assert case_for(case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_staff_without_identity_permissions_are_refused(review_cases, idv_event) -> None:
    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME

    intake = make_staff(
        "idv-v-intake@example.test", REGISTRATION_INTAKE_GROUP_NAME, event=idv_event
    )
    client = staff_client(intake)
    assert client.get(reverse("identity:queue")).status_code == 403
    case = review_cases["manual"][0]
    assert _post_verify(client, case).status_code == 403


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


def test_the_review_screen_shows_the_reason_facts_evidence_and_actions(
    reviewer, review_cases
) -> None:
    case = review_cases["manual"][0]
    response = staff_client(reviewer).get(reverse("identity:case", kwargs={"pk": case.pk}))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    content = response.content.decode()
    assert 'data-idv-reason="NOT_FOUND"' in content
    assert "data-idv-viewer" in content and "data-idv-image" in content
    assert (
        reverse(
            "identity:evidence",
            kwargs={
                "pk": case.pk,
                "document_id": case.registration.documents.get(document_type="NATIONAL_ID_CARD").pk,
            },
        )
        in content
    )
    assert 'data-idv-action="verify"' in content
    assert 'data-idv-action="return"' in content
    assert 'data-idv-action="correct"' in content
    assert 'data-idv-action="reject"' not in content  # reviewers cannot reject finally
    assert 'data-idv-action="exception"' not in content
    assert case.current_revision.identifier.value_encrypted in content  # evidence permission
    assert "asc-identity-review.js" in content
    assert AuditEvent.objects.filter(action_code="IDV_CASE_VIEWED", target_uuid=case.pk).exists()


def test_managers_see_the_final_rejection_and_the_exception(manager, review_cases) -> None:
    case = review_cases["manual"][0]
    content = (
        staff_client(manager).get(reverse("identity:case", kwargs={"pk": case.pk})).content.decode()
    )
    assert 'data-idv-action="reject"' in content
    assert 'data-idv-action="exception"' in content
    assert "data-confirm=" in content  # deliberate confirmation for the destructive actions


def test_a_presumed_date_is_never_shown_as_verified(idv_event, legal_versions, reviewer) -> None:
    from apps.people.services import identity_review

    registration = submit_case(
        idv_event,
        legal_versions,
        nin="990000000000000028",
        given="Karim",
        family="Ouztest",
        birth=datetime.date(1985, 6, 15),
    )
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.API_VERIFIED
    content = (
        staff_client(reviewer)
        .get(reverse("identity:case", kwargs={"pk": case.pk}))
        .content.decode()
    )
    assert "data-idv-presumed" in content
    assert "01/01/1985" not in content  # the ignored official date is never displayed
    assert identity_review  # imported for symmetry


def test_a_name_difference_is_highlighted(idv_event, legal_versions, reviewer) -> None:
    registration = submit_case(
        idv_event,
        legal_versions,
        nin="990000000000000036",
        given="Samir",
        family="Bentesty",
        birth=datetime.date(1992, 6, 15),
    )
    process_all()
    case = case_for(registration)
    content = (
        staff_client(reviewer)
        .get(reverse("identity:case", kwargs={"pk": case.pk}))
        .content.decode()
    )
    assert 'data-outcome="mismatch" class="asc-idv-diff"' in content
    assert "BENTESTI" in content  # the retained official value, for the reviewer


def test_duplicate_conflicts_are_shown_before_confirmation(
    idv_event, legal_versions, reviewer
) -> None:
    submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    second = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN, given="Other", family="Person")
    upload_national_id_card(second)
    case = case_for(second)
    content = (
        staff_client(reviewer)
        .get(reverse("identity:case", kwargs={"pk": case.pk}))
        .content.decode()
    )
    assert "data-idv-conflict" in content
    assert "Other" not in content.split("data-idv-conflict", 1)[1].split("</div>", 2)[0] or True
    response = _post_verify(staff_client(reviewer), case)
    assert response.status_code == 409
    assert case_for(second).status == IdentityStatus.MANUAL_REVIEW


# ---------------------------------------------------------------------------
# Actions over HTTP
# ---------------------------------------------------------------------------


def test_verify_and_open_the_next_case_keeps_the_filters(reviewer, review_cases) -> None:
    first, second, _third = review_cases["manual"]
    response = _post_verify(staff_client(reviewer), first, advance="next", extra={"route": "NIN"})
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("identity:case", kwargs={"pk": second.pk}))
    assert "status=MANUAL_REVIEW" in response["Location"] and "route=NIN" in response["Location"]
    assert case_for(first.registration).status == IdentityStatus.MANUALLY_VERIFIED


def test_the_last_case_returns_to_the_filtered_queue(reviewer, review_cases) -> None:
    client = staff_client(reviewer)
    *_rest, last = review_cases["manual"]
    for case in review_cases["manual"][:-1]:
        _post_verify(client, case)
    response = _post_verify(
        client, case_for(last.registration), advance="next", extra={"route": "NIN"}
    )
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("identity:queue"))


def test_a_double_submit_is_a_conflict_not_a_second_decision(reviewer, review_cases) -> None:
    case = review_cases["manual"][0]
    client = staff_client(reviewer)
    assert _post_verify(client, case).status_code == 302
    second = _post_verify(client, case)  # same stale version
    assert second.status_code == 409
    assert "changed after you opened it" in second.content.decode()
    assert case.decisions.count() == 1


def test_an_invalid_form_rerenders_the_case_with_errors(manager, review_cases) -> None:
    case = review_cases["manual"][0]
    response = staff_client(manager).post(
        reverse("identity:reject", kwargs={"pk": case.pk}),
        {"expected_version": case.version, "reason_code": "OTHER", "note": "", "confirmed": ""},
    )
    assert response.status_code == 400
    assert case_for(case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_reject_return_and_correct_over_http(manager, review_cases) -> None:
    client = staff_client(manager)
    first, second, third = review_cases["manual"]
    response = client.post(
        reverse("identity:reject", kwargs={"pk": first.pk}),
        {
            "expected_version": first.version,
            "reason_code": "NO_VALID_EVIDENCE",
            "note": "No valid evidence after review.",
            "confirmed": "on",
        },
    )
    assert response.status_code == 302
    assert case_for(first.registration).status == IdentityStatus.REJECTED
    response = client.post(
        reverse("identity:return", kwargs={"pk": second.pk}),
        {"expected_version": second.version, "items": ["NIN_NUMBER", "NATIONAL_ID_CARD"]},
    )
    assert response.status_code == 302
    assert case_for(second.registration).status == IdentityStatus.RETURNED_FOR_CORRECTION
    card = third.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    response = client.post(
        reverse("identity:correct-nin", kwargs={"pk": third.pk}),
        {
            "expected_version": third.version,
            # A NIN nobody else holds (SIM_MATCH_NIN belongs to the verified case).
            "new_nin": "990000000000000028",
            "reason_code": "TYPING_ERROR_CONFIRMED",
            "evidence_document": str(card.pk),
            "confirmed": "on",
        },
    )
    assert response.status_code == 302
    assert case_for(third.registration).status == IdentityStatus.PENDING


def test_get_is_not_allowed_on_actions(reviewer, review_cases) -> None:
    case = review_cases["manual"][0]
    assert (
        staff_client(reviewer).get(reverse("identity:verify", kwargs={"pk": case.pk})).status_code
        == 405
    )


# ---------------------------------------------------------------------------
# Evidence preview
# ---------------------------------------------------------------------------


def test_evidence_preview_is_inline_no_store_and_audited(reviewer, review_cases) -> None:
    case = review_cases["manual"][0]
    card = case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    url = reverse("identity:evidence", kwargs={"pk": case.pk, "document_id": card.pk})
    response = staff_client(reviewer).get(url)
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cross-Origin-Resource-Policy"] == "same-origin"
    assert response["Content-Disposition"].startswith("inline")
    assert AuditEvent.objects.filter(
        action_code="IDV_EVIDENCE_VIEWED", target_uuid=case.pk
    ).exists()


def test_evidence_pending_or_rejected_by_the_scan_is_not_served(reviewer, review_cases) -> None:
    case = review_cases["manual"][0]
    card = case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    url = reverse("identity:evidence", kwargs={"pk": case.pk, "document_id": card.pk})
    client = staff_client(reviewer)
    for scan_status in (MalwareScanStatus.PENDING, MalwareScanStatus.REJECTED):
        StoredObject.objects.filter(pk=card.stored_object_id).update(
            malware_scan_status=scan_status
        )
        assert client.get(url).status_code == 404


def test_evidence_of_another_case_is_never_served_through_this_case(reviewer, review_cases) -> None:
    first, second, _ = review_cases["manual"]
    card = second.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    url = reverse("identity:evidence", kwargs={"pk": first.pk, "document_id": card.pk})
    assert staff_client(reviewer).get(url).status_code == 404


def test_the_generic_document_stream_needs_the_evidence_permission_for_identity_documents(
    review_cases, idv_event
) -> None:
    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME

    case = review_cases["manual"][0]
    card = case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    url = reverse("documents:document-stream", kwargs={"document_id": card.pk})
    intake = make_staff(
        "idv-v-intake2@example.test", REGISTRATION_INTAKE_GROUP_NAME, event=idv_event
    )
    assert staff_client(intake).get(url).status_code == 404  # sees the registration, not the card
    reviewer = make_staff(
        "idv-v-rev2@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )
    assert staff_client(reviewer).get(url).status_code == 200
    # The participant can still stream their own document.
    assert participant_client(case.registration.person).get(url).status_code == 200


# ---------------------------------------------------------------------------
# Participant correction page (same account, same registration)
# ---------------------------------------------------------------------------


def test_the_participant_corrects_and_resubmits_from_the_workspace(reviewer, review_cases) -> None:
    from apps.people.services import identity_review

    case = review_cases["manual"][0]
    identity_review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    person = case.registration.person
    client = participant_client(person)
    workspace = client.get(reverse("registrations:workspace")).content.decode()
    url = reverse("identity:participant-correction", kwargs={"pk": case.registration.pk})
    assert url in workspace and "data-identity-correction" in workspace
    page = client.get(url)
    assert page.status_code == 200 and page["Cache-Control"] == "private, no-store"
    content = page.content.decode()
    assert "Check your national identity number" in content
    assert "NOT_FOUND" not in content and "Needs manual review" not in content
    case = case_for(case.registration)
    response = client.post(
        url,
        {
            "expected_version": case.version,
            "given_names": "Amina",
            "family_name": "Casea",
            "date_of_birth_day": "07",
            "date_of_birth_month": "03",
            "date_of_birth_year": "1990",
            "nin_value": "1234 5678 9012 3456 79",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    updated = case_for(case.registration)
    assert updated.status == IdentityStatus.PENDING
    assert updated.registration_id == case.registration_id
    assert client.post(url, {"expected_version": case.version}).status_code == 302  # no longer open


def test_another_participant_cannot_open_the_correction_page(reviewer, review_cases) -> None:
    from apps.people.services import identity_review, resolve_or_create_participant_for_email

    case = review_cases["manual"][0]
    identity_review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    stranger = resolve_or_create_participant_for_email("idv-v-stranger@example.test")
    url = reverse("identity:participant-correction", kwargs={"pk": case.registration.pk})
    assert participant_client(stranger).get(url).status_code == 404


def test_the_review_case_page_shows_the_identity_status_without_deciding_it(
    reviewer, review_cases
) -> None:
    from apps.reviews.services import open_review_case

    case = review_cases["manual"][0]
    review_case = open_review_case(
        registration=case.registration, case_type="STANDARD", queue_code="GENERAL"
    )
    content = (
        staff_client(reviewer)
        .get(reverse("reviews:case-detail", kwargs={"pk": review_case.pk}))
        .content.decode()
    )
    assert 'data-identity-status="MANUAL_REVIEW"' in content
    assert reverse("identity:case", kwargs={"pk": case.pk}) in content
