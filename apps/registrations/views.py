"""Universal registration wizard, participant workspace, and minimal operations intake."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.views.decorators.http import require_http_methods

from apps.accounts import participant_auth
from apps.accounts.policies import operational_permission_required
from apps.accounts.selectors import registrations_visible_to
from apps.documents.services import active_passport_identity_page, active_profile_photo
from apps.documents.views import scan_unavailable_message
from apps.events.policies.registration_channels import (
    RegistrationChannelClosed,
    decide_for_registration,
    public_registration_is_open,
)
from apps.events.selectors import NoOpenEventEdition, current_event_edition
from apps.invitations.services import CampaignCapacityExceededError
from apps.people.models import Person
from apps.privacy.models import ConsentPurpose
from apps.privacy.selectors import effective_published_version_with_fallback

from .forms import (
    ContactStepForm,
    IdentityStepForm,
    InterestsStepForm,
    NoticesForm,
    ProfessionalStepForm,
)
from .models import InterestTopic, Registration, RegistrationPublicStatus
from .selectors import has_sensitive_support_consent
from .services import (
    DATA_PROCESSING_PURPOSE_CODE,
    AccommodationValidationError,
    IncompleteRegistrationError,
    ProcessingConsentRequired,
    RegistrationNotDraft,
    RegistrationNotRestartable,
    SensitiveConsentRequired,
    accommodation_request_for,
    advance_current_step,
    get_or_create_active_draft,
    initial_submission_operation_key,
    is_participant_withdrawn,
    restart_option_for,
    save_contact_step,
    save_identity_step,
    save_interests_and_accommodation_step,
    save_professional_step,
    start_registration_after_identity_rejection,
    start_registration_after_withdrawal,
    start_registration_from_invitation,
    submit_full_registration,
)

ALGERIA_COUNTRY_CODE = "DZ"

# Maps a persisted Registration.current_step value to the URL name that
# resumes there (Prompt 4 final closure pass §5). An unrecognized/blank
# value always falls back to the first step, never a guess further ahead.
_STEP_URL_NAMES = {
    "identity": "registrations:step-identity",
    "contact": "registrations:step-contact",
    "professional": "registrations:step-professional",
    "interests": "registrations:step-interests",
    "review": "registrations:step-review",
    "notices": "registrations:step-notices",
}

# The generic, localized message shown for every incomplete-submission step
# -- never names the specific missing field (Prompt 4 final closure pass §1).
_INCOMPLETE_STEP_MESSAGES = {
    "account": _("Your account is not ready to submit a registration yet. Please contact support."),
    "identity": _("Please complete all required identity information before continuing."),
    "contact": _("Please provide the required contact information before continuing."),
    "professional": _("Please complete all required professional information before continuing."),
    "interests": _("Please select at least one interest and describe your objectives."),
    "notices": _("Legal notices are not currently available. Please try again shortly."),
}


def _step_url_name(current_step: str) -> str:
    return _STEP_URL_NAMES.get(current_step, "registrations:step-identity")


def _current_person(request) -> Person:
    return get_object_or_404(Person, pk=participant_auth.get_participant_person_id(request))


def _get_or_start_draft(request) -> tuple[Registration | None, object | None]:
    """Returns `(registration, early_response)` -- exactly one is `None`."""
    person = _current_person(request)
    active_id = participant_auth.get_active_registration_id(request)
    if active_id:
        registration = Registration.objects.filter(pk=active_id, person_id=person.id).first()
        if registration is not None:
            return registration, None

    # An invitation link resolved to a valid campaign earlier in this
    # session (`invitations.views.invitation_start`) -- consume it exactly
    # once, creating an INVITATION-source draft instead of an OPEN one
    # (Phase 2 Prompt 2, AF-ORG-02). A pending link is treated like every
    # other invalid/revoked/expired invitation and rendered as the SAME
    # generic unavailable response -- INCLUDING when the row has been
    # deleted outright between initial resolution and this consumption
    # (Phase 2 Prompt 2 V2 correction pass "fail generically when a
    # pending invitation link disappears": a session value that no longer
    # resolves to any row must never silently fall through to ordinary
    # open registration, which would let a participant who followed a
    # since-deleted invitation link end up registering as if they had
    # never used one at all). A link that DOES still exist is atomically
    # RE-VALIDATED (locked, status/expiry/campaign rechecked) inside
    # `create_invited_draft_registration` itself -- it may have been
    # revoked, rotated, expired, or had its campaign suspended/closed/
    # expired since it was first resolved; that failure is surfaced here
    # as the SAME generic unavailable response the initial resolution uses
    # (Phase 2 Prompt 2 correction pass "revalidate invitation links
    # atomically when consumed").
    pending_link_id = request.session.pop("invitation_link_id", None)
    if pending_link_id:
        from apps.invitations.models import InvitationLink
        from apps.invitations.services import InvitationLinkUnavailable
        from apps.invitations.views import render_invitation_unavailable

        link = InvitationLink.objects.filter(pk=pending_link_id).first()
        if link is None:
            return None, render_invitation_unavailable(request)
        try:
            # The same service as the workspace button: a current registration
            # of that campaign is resumed (never a second context, which the
            # deduplication constraint would refuse), and one the participant
            # withdrew gives way to a new draft (owner correction, 2026-10-04).
            registration = start_registration_from_invitation(link=link, person=person)
        except InvitationLinkUnavailable:
            return None, render_invitation_unavailable(request)
        participant_auth.set_active_registration(request, registration.pk)
        return registration, None

    try:
        event = current_event_edition()
    except NoOpenEventEdition:
        return None, None
    try:
        registration = get_or_create_active_draft(person=person, event_edition=event)
    except RegistrationChannelClosed as exc:
        return None, render_registration_closed(request, exc.decision.reason)
    participant_auth.set_active_registration(request, registration.pk)
    return registration, None


def render_registration_closed(request, reason: str):
    """The one "registration is closed" page (UX-4, D-12), HTTP 403.

    Shown only to a signed-in participant, after OTP verification, so it
    reveals nothing about whether an email address has an account. Saved
    drafts are kept and the page says so.
    """
    return render(
        request,
        "registrations/registration_closed.html",
        {"all_channels_closed": reason == "REGISTRATION_CLOSED"},
        status=403,
    )


def _require_open_draft(request):
    registration, early_response = _get_or_start_draft(request)
    if early_response is not None:
        return None, early_response
    if registration is None:
        return None, render(request, "registrations/no_open_event.html", status=503)
    if registration.public_status == RegistrationPublicStatus.SUBMITTED:
        # A retried/double-clicked step request against an already-submitted
        # Registration returns to that SAME successful result (AF-REG-06:
        # "Repeated clicks or retries MUST return the same successful
        # result"), not a generic bounce to the workspace.
        return None, redirect("registrations:confirmation", reference=registration.public_reference)
    if registration.public_status != RegistrationPublicStatus.DRAFT:
        return None, redirect("registrations:workspace")
    # UX-4 (D-12): every wizard GET and POST is refused while the draft's
    # channel is closed -- direct URLs, forged posts and stale tabs alike.
    # The source is server-held, so no request value can change the rule.
    decision = decide_for_registration(registration)
    if not decision.allowed:
        return None, render_registration_closed(request, decision.reason)
    return registration, None


@participant_auth.participant_required
@require_http_methods(["POST"])
def continue_registration(request, pk):
    """Resume ONE specific Draft, verified to belong to the signed-in participant
    (Prompt 4 final closure pass §5). POST + CSRF (Django's default middleware) so a
    third-party page cannot silently switch which registration context is active."""
    person = _current_person(request)
    registration = get_object_or_404(Registration, pk=pk, person=person)
    participant_auth.set_active_registration(request, registration.pk)
    if registration.public_status == RegistrationPublicStatus.SUBMITTED:
        return redirect("registrations:confirmation", reference=registration.public_reference)
    return redirect(_step_url_name(registration.current_step))


@participant_auth.participant_required
def identity_step(request):
    registration, early_response = _require_open_draft(request)
    if early_response is not None:
        return early_response

    current_page = active_passport_identity_page(registration)
    profile = getattr(registration, "profile", None)
    initial = {}
    if profile is not None:
        initial = {
            "given_names": profile.submitted_given_names,
            "family_name": profile.submitted_family_name,
            "date_of_birth": profile.date_of_birth,
            "nationality_code": profile.nationality_code_id,
            "country_of_residence": profile.country_of_residence_id,
        }
    elif request.method == "GET":
        # UX-2 (M08, S-09): Algeria is preselected only on a genuinely blank
        # new form: no saved profile and no posted data. It is editable, and a
        # language-switch restore still overrides it on the client.
        initial = {
            "nationality_code": ALGERIA_COUNTRY_CODE,
            "country_of_residence": ALGERIA_COUNTRY_CODE,
        }
    # IDV-Q3: the documentary route is offered only while the registration
    # team holds an ACTIVE NIN exemption for this draft.
    from apps.documents.services import active_identity_evidence, is_reviewable_evidence
    from apps.people.services.nin_exemption import active_exemption

    exemption = active_exemption(registration.pk)
    exemption_evidence_on_file = (
        {
            document.document_type
            for document in active_identity_evidence(registration)
            if is_reviewable_evidence(document)
        }
        if exemption is not None
        else set()
    )
    form = IdentityStepForm(
        request.POST or None,
        request.FILES or None,
        initial=initial,
        event_edition=registration.event_edition,
        has_passport_identity_page=current_page is not None,
        nin_exemption_active=exemption is not None,
        exemption_evidence_on_file=exemption_evidence_on_file,
    )
    if request.method == "POST" and form.is_valid():
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.documents.services import (
            NationalIdCardValidationError,
            PassportIdentityPageValidationError,
        )

        data = form.cleaned_data
        try:
            save_identity_step(
                registration=registration,
                given_names=data["given_names"],
                family_name=data["family_name"],
                date_of_birth=data["date_of_birth"],
                nationality_code_id=data["nationality_code"].pk,
                country_of_residence_id=data["country_of_residence"].pk,
                identity_path=data["identity_path"],
                nin_value=data.get("nin_value", ""),
                passport_number=data.get("passport_number", ""),
                passport_country_code_id=(
                    data["passport_country_code"].pk if data.get("passport_country_code") else ""
                ),
                passport_expires_at=data.get("passport_expires_at"),
                passport_identity_page_file=data.get("passport_identity_page"),
                exemption_document_kind=data.get("exemption_document_kind", ""),
                exemption_document_number=data.get("exemption_document_number", ""),
                exemption_document_expires_at=data.get("exemption_document_expires_at"),
                exemption_document_file=data.get("exemption_document_image"),
            )
        except RegistrationNotDraft:
            # UX-C2 (C): submitted in another tab; the committed record wins.
            return redirect("registrations:workspace")
        except (
            PassportIdentityPageValidationError,
            NationalIdCardValidationError,
        ) as exc:
            from apps.documents.services import SCAN_UNAVAILABLE

            field = (
                "exemption_document_image"
                if data.get("identity_path") == "NIN_EXEMPTION"
                else "passport_identity_page"
            )
            if exc.reason_code == SCAN_UNAVAILABLE:
                form.add_error(field, scan_unavailable_message())
            else:
                form.add_error(
                    field, _("This file could not be accepted. Please try a different image.")
                )
        except DjangoValidationError:
            # Defence in depth (Prompt 5 correction pass §6): the form's own
            # `clean()` already rejects a tampered NIN-path-plus-file POST
            # before this is ever reached, but a tampered/unexpected
            # request must still surface as a normal form-validation
            # response here too, never an unhandled 500 -- no technical
            # detail from the underlying exception is exposed.
            form.add_error(None, _("Please check the information provided and try again."))
        else:
            advance_current_step(registration, "contact")
            return redirect("registrations:step-contact")

    from django.conf import settings

    return render(
        request,
        "registrations/step_identity.html",
        {
            "form": form,
            "registration": registration,
            "current_passport_identity_page": current_page,
            "passport_identity_page_max_size_mb": (
                settings.PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES // (1024 * 1024)
            ),
            "nin_exemption_active": exemption is not None,
            "exemption_evidence_on_file": bool(exemption_evidence_on_file),
        },
    )


@participant_auth.participant_required
def contact_step(request):
    registration, early_response = _require_open_draft(request)
    if early_response is not None:
        return early_response

    profile = getattr(registration, "profile", None)
    # UX-2 (M13, D-01): a mobile number is required for everyone.
    mobile_required = True
    initial = {}
    existing_contact = None
    if profile is not None:
        existing_contact = profile.declared_mobile_contact or profile.verified_mobile_contact
    if existing_contact is not None:
        initial = {
            "mobile_number": existing_contact.value_encrypted,
            "mobile_country_code": existing_contact.country_code_id,
        }
    elif profile is not None and profile.country_of_residence_id:
        # The calling code starts from the country of residence only while the
        # number is empty (S-10); it never overrides a saved number.
        initial = {"mobile_country_code": profile.country_of_residence_id}
    form = ContactStepForm(request.POST or None, initial=initial, mobile_required=mobile_required)
    if request.method == "POST" and form.is_valid():
        from django.core.exceptions import ValidationError as DjangoValidationError

        data = form.cleaned_data
        try:
            save_contact_step(
                registration=registration,
                mobile_number=data.get("mobile_number", ""),
                mobile_country_code_id=(
                    data["mobile_country_code"].pk if data.get("mobile_country_code") else ""
                ),
            )
        except RegistrationNotDraft:
            # UX-C2 (C): submitted in another tab; the committed record wins.
            return redirect("registrations:workspace")
        except DjangoValidationError:
            # The service re-applies the form's rules (UX-C1); a value that
            # changed status in between is a form error, never a 500.
            form.add_error(None, _("Please check the information provided and try again."))
        else:
            advance_current_step(registration, "professional")
            return redirect("registrations:step-professional")

    return render(
        request,
        "registrations/step_contact.html",
        {"form": form, "registration": registration, "mobile_required": mobile_required},
    )


@participant_auth.participant_required
def professional_step(request):
    registration, early_response = _require_open_draft(request)
    if early_response is not None:
        return early_response

    affiliation = getattr(registration, "professional_affiliation", None)
    initial = {}
    if affiliation is not None:
        initial = {
            "organization_name": affiliation.submitted_organization_name,
            "organization_type": affiliation.organization_type,
            "job_title": affiliation.job_title,
            "department": affiliation.department,
            "sector": affiliation.sector_id,
            "country_code": affiliation.country_code_id,
            "operating_scope": affiliation.operating_scope,
            "organization_website": affiliation.organization_website,
            "professional_profile_url": affiliation.professional_profile_url,
            "biography": affiliation.biography,
        }
    # The photograph is required (owner correction, 2026-10-04); an eligible
    # one already on file satisfies it, so a re-save does not ask again.
    current_photo = active_profile_photo(registration)
    form = ProfessionalStepForm(
        request.POST or None,
        request.FILES or None,
        initial=initial,
        has_profile_photo=current_photo is not None,
    )
    if request.method == "POST" and form.is_valid():
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.documents.services import ProfilePhotoValidationError

        data = form.cleaned_data
        try:
            save_professional_step(
                registration=registration,
                organization_name=data["organization_name"],
                organization_type=data["organization_type"],
                job_title=data["job_title"],
                department=data["department"],
                sector_code_id=data["sector"].pk,
                country_code_id=data["country_code"].pk,
                organization_website=data["organization_website"],
                professional_profile_url=data["professional_profile_url"],
                biography=data["biography"],
                profile_photo_file=data.get("profile_photo"),
                operating_scope=data["operating_scope"],
            )
        except RegistrationNotDraft:
            # UX-C2 (C): submitted in another tab; the committed record wins.
            return redirect("registrations:workspace")
        except ProfilePhotoValidationError as exc:
            from apps.documents.services import SCAN_UNAVAILABLE

            if exc.reason_code == SCAN_UNAVAILABLE:
                form.add_error("profile_photo", scan_unavailable_message())
            else:
                form.add_error(
                    "profile_photo",
                    _("This photograph could not be accepted. Please try a different image."),
                )
        except DjangoValidationError:
            # The service re-applies the form's rules (UX-C1); see contact_step.
            form.add_error(None, _("Please check the information provided and try again."))
        else:
            advance_current_step(registration, "interests")
            return redirect("registrations:step-interests")

    return render(
        request,
        "registrations/step_professional.html",
        {
            "form": form,
            "registration": registration,
            "current_photo": current_photo,
        },
    )


#: Display order and labels of the interest groups (UX-2, C-06).
INTEREST_GROUP_LABELS = (
    ("FUNDING_INVESTMENT", _lazy("Funding and investment")),
    ("GROWTH", _lazy("Growth")),
    ("INNOVATION", _lazy("Innovation")),
    ("ECOSYSTEM_POLICY", _lazy("Ecosystem and policy")),
    ("TALENT", _lazy("Talent")),
    ("NETWORKING", _lazy("Networking")),
    ("", _lazy("Other topics")),
)


def _interest_groups(form, topic_queryset) -> list[dict]:
    """The topic checkboxes of `form`, grouped for display. Presentation only."""
    group_by_pk = {str(pk): group for pk, group in topic_queryset.values_list("pk", "group_code")}
    known = {code for code, _label in INTEREST_GROUP_LABELS}
    buckets: dict[str, list] = {code: [] for code, _label in INTEREST_GROUP_LABELS}
    for checkbox in form["interest_topics"]:
        group = group_by_pk.get(str(checkbox.data["value"]), "")
        buckets[group if group in known else ""].append(checkbox)
    return [
        {"label": label, "checkboxes": buckets[code]}
        for code, label in INTEREST_GROUP_LABELS
        if buckets[code]
    ]


@participant_auth.participant_required
def interests_step(request):
    registration, early_response = _require_open_draft(request)
    if early_response is not None:
        return early_response

    topic_queryset = InterestTopic.objects.filter(
        event_edition=registration.event_edition, is_active=True
    )
    profile = getattr(registration, "profile", None)
    accommodation = accommodation_request_for(registration)
    initial = {
        "interest_topics": list(registration.interests.values_list("interest_topic_id", flat=True)),
        "objectives_text": profile.objectives_text if profile else "",
        "accommodation_answer": accommodation.answer if accommodation else "",
        "accommodation_categories": list(accommodation.categories) if accommodation else [],
        "accommodation_note": accommodation.note_encrypted if accommodation else "",
    }
    form = InterestsStepForm(
        request.POST or None, initial=initial, interest_topic_queryset=topic_queryset
    )
    interest_groups = _interest_groups(form, topic_queryset)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            # UX-C2 (C): both parts in one transaction behind the Registration lock.
            save_interests_and_accommodation_step(
                registration=registration,
                interest_topic_ids=[topic.pk for topic in data["interest_topics"]],
                objectives_text=data.get("objectives_text", ""),
                accommodation_answer=data.get("accommodation_answer", ""),
                accommodation_categories=data.get("accommodation_categories", []),
                accommodation_note=data.get("accommodation_note", ""),
            )
        except RegistrationNotDraft:
            # Submitted in another tab after this request was checked (UX-F05,
            # UX-C2): the committed submission wins and nothing is changed.
            return redirect("registrations:workspace")
        except DjangoValidationError, AccommodationValidationError:
            # UXR-C1 (UXR-F06): the service re-applies the rules; a topic that
            # changed status in between is a form error, never a 500, and the
            # whole POST was rolled back.
            form.add_error(None, _("Please check the information provided and try again."))
        else:
            advance_current_step(registration, "review")
            return redirect("registrations:step-review")

    return render(
        request,
        "registrations/step_interests.html",
        {"form": form, "registration": registration, "interest_groups": interest_groups},
    )


@participant_auth.participant_required
def review_step(request):
    registration, early_response = _require_open_draft(request)
    if early_response is not None:
        return early_response
    if request.method == "POST":
        advance_current_step(registration, "notices")
        return redirect("registrations:step-notices")
    from apps.registrations.models import AccommodationCategory

    accommodation = accommodation_request_for(registration)
    labels = dict(AccommodationCategory.choices)
    return render(
        request,
        "registrations/step_review.html",
        {
            "registration": registration,
            "profile_photo": active_profile_photo(registration),
            "accommodation": accommodation,
            "accommodation_labels": [labels[code] for code in accommodation.categories]
            if accommodation
            else [],
        },
    )


@participant_auth.participant_required
def notices_step(request):
    registration, early_response = _require_open_draft(request)
    if early_response is not None:
        return early_response

    language = request.LANGUAGE_CODE or "en"
    privacy_version = effective_published_version_with_fallback("PRIVACY_NOTICE", language)
    terms_version = effective_published_version_with_fallback("TERMS", language)
    marketing_purpose = ConsentPurpose.objects.filter(code="MARKETING", is_active=True).first()
    processing_purpose = ConsentPurpose.objects.filter(
        code=DATA_PROCESSING_PURPOSE_CODE, is_active=True
    ).first()
    accommodation = accommodation_request_for(registration)
    requires_sensitive_consent = accommodation is not None and accommodation.has_sensitive_data
    from apps.registrations.models import AccommodationAnswer

    offers_optional_sensitive_consent = (
        accommodation is not None
        and accommodation.answer == AccommodationAnswer.YES
        and not requires_sensitive_consent
    )

    form = NoticesForm(
        request.POST or None,
        requires_sensitive_consent=requires_sensitive_consent,
        offers_optional_sensitive_consent=offers_optional_sensitive_consent,
        initial={
            "privacy_version_id": str(privacy_version.pk) if privacy_version else "",
            "terms_version_id": str(terms_version.pk) if terms_version else "",
        },
    )
    if request.method == "POST" and form.is_valid():
        if privacy_version is None or terms_version is None or processing_purpose is None:
            # UX-C1 (UX-F02): without the active processing purpose the
            # required consent cannot be recorded, so nothing is submitted.
            messages.error(request, _INCOMPLETE_STEP_MESSAGES["notices"])
        elif (form.cleaned_data["privacy_version_id"], form.cleaned_data["terms_version_id"]) != (
            str(privacy_version.pk),
            str(terms_version.pk),
        ):
            # Owner correction (2026-10-04): a notice was republished (or the
            # page was opened in another language) after this page was shown.
            # The participant reads and confirms the current version; nothing
            # is recorded for a version they did not see.
            messages.info(
                request,
                _(
                    "The Privacy Notice or the Registration Terms shown to you have changed. "
                    "Please read the current version and confirm again."
                ),
            )
            return redirect("registrations:step-notices")
        else:
            # Derived ONLY from the Registration's own id -- never a random
            # value cached in the session (Prompt 6 P6-H-02 correction): two
            # near-simultaneous requests (a double-click, or a client retry)
            # sharing the same session could otherwise both read the session
            # before either write committed, and each mint a DIFFERENT
            # random key, defeating idempotency entirely. This key is
            # identical for every request addressing this Registration's
            # initial submission, so the service-layer lock and dedup below
            # converge regardless of how many requests raced to get here.
            idempotency_key = initial_submission_operation_key(registration.pk)
            try:
                submit_full_registration(
                    registration=registration,
                    privacy_notice_version=privacy_version,
                    terms_version=terms_version,
                    marketing_consent_purpose=marketing_purpose,
                    marketing_consent_granted=form.cleaned_data.get("marketing_consent", False),
                    data_processing_consent_granted=form.cleaned_data["accept_data_processing"],
                    data_processing_consent_purpose=processing_purpose,
                    sensitive_data_consent_granted=bool(
                        form.cleaned_data.get("accept_sensitive_data", False)
                    ),
                    session_reference=request.session.session_key or "",
                    idempotency_key=idempotency_key,
                )
            except IncompleteRegistrationError as exc:
                messages.error(
                    request,
                    _INCOMPLETE_STEP_MESSAGES.get(exc.step, _INCOMPLETE_STEP_MESSAGES["identity"]),
                )
                return redirect(_step_url_name(exc.step))
            except RegistrationChannelClosed as exc:
                # Closed after this page was opened: nothing was recorded and
                # the draft is kept.
                return render_registration_closed(request, exc.decision.reason)
            except RegistrationNotDraft:
                # UX-C2 (C): no longer a draft (for example withdrawn meanwhile).
                return redirect("registrations:workspace")
            except ProcessingConsentRequired:
                messages.error(
                    request,
                    _("Please confirm the consent to the processing of your personal data."),
                )
                return redirect("registrations:step-notices")
            except SensitiveConsentRequired:
                # The accommodation data changed in another tab after this page
                # was rendered; show the page again with the consent box.
                messages.error(
                    request,
                    _("Please confirm the consent for your accommodation information."),
                )
                return redirect("registrations:step-notices")
            except CampaignCapacityExceededError:
                # Generic, participant-facing result -- never names "capacity"
                # or any other campaign internal (Phase 2 Prompt 2).
                messages.error(
                    request,
                    _("This registration could not be completed. Please contact support."),
                )
                return redirect("registrations:workspace")
            else:
                from .confirmation import send_registration_confirmation

                send_registration_confirmation(registration)
                return redirect(
                    "registrations:confirmation", reference=registration.public_reference
                )

    return render(
        request,
        "registrations/step_notices.html",
        {
            "form": form,
            "registration": registration,
            "privacy_version": privacy_version,
            "terms_version": terms_version,
        },
    )


@participant_auth.participant_required
def confirmation(request, reference: str):
    from apps.communications.models import CommunicationMessage

    person = _current_person(request)
    registration = get_object_or_404(Registration, public_reference=reference, person=person)
    confirmation_message = (
        CommunicationMessage.objects.filter(
            registration=registration,
            template_version__template__code="REGISTRATION_CONFIRMATION",
        )
        .order_by("-created_at")
        .first()
    )
    from apps.accreditation.attendance import participant_attendance

    registration.attendance = participant_attendance(registration)
    return render(
        request,
        "registrations/confirmation.html",
        {"registration": registration, "confirmation_message": confirmation_message},
    )


def _has_current_registration_of_same_origin(registration, registrations) -> bool:
    """In-memory twin of `services.current_registration_of_same_origin` over the
    workspace's already-loaded rows (all the participant's own)."""
    if registration.source_kind == "OPEN":
        context_key = "open"
    elif registration.source_kind == "INVITATION":
        context_key = f"invitation-campaign:{registration.invitation_campaign_id}"
    else:
        return False
    return any(
        other.pk != registration.pk
        and other.is_current_context
        and other.event_edition_id == registration.event_edition_id
        and other.source_kind == registration.source_kind
        and other.source_context_key == context_key
        for other in registrations
    )


@participant_auth.participant_required
def workspace(request):
    from apps.reviews.models import InformationRequest, InformationRequestStatus

    person = _current_person(request)
    registrations = list(
        Registration.objects.filter(person=person)
        .select_related("event_edition")
        .order_by("-created_at")
    )
    # One extra bounded query for every registration's own action-required
    # indicator (Phase 2 Prompt 3 §14 "context-specific action-required
    # indicator") -- never N+1, never another context's request.
    active_requests = {
        info_request.registration_id: info_request
        for info_request in InformationRequest.objects.filter(
            registration__in=registrations,
            status__in=InformationRequestStatus.participant_action_required_statuses(),
        )
    }
    from apps.accreditation.attendance import participant_attendance
    from apps.accreditation.selectors import participant_safe_badge_projection
    from apps.people.models import IdentityStatus, IdentityVerification

    # IDV-3: one bounded query for the identity correction requests -- only
    # while the registration still awaits the participant (IDV-C1, R-IDV-06).
    identity_corrections = set(
        IdentityVerification.objects.filter(
            registration__in=registrations,
            registration__public_status=RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
            status=IdentityStatus.RETURNED_FOR_CORRECTION,
        ).values_list("registration_id", flat=True)
    )
    # IDV-Q2: one bounded query for the registrations closed by a final
    # identity rejection (coded participation decision only, never a reason),
    # and the events where the person already has a current registration.
    from apps.reviews.models import RegistrationDecision
    from apps.reviews.services import IDENTITY_REJECTION_INTERNAL_REASON

    identity_rejected = set(
        RegistrationDecision.objects.filter(
            registration__in=registrations,
            registration__public_status=RegistrationPublicStatus.NOT_APPROVED,
            registration__is_current_context=False,
            is_current=True,
            internal_reason_code=IDENTITY_REJECTION_INTERNAL_REASON,
        ).values_list("registration_id", flat=True)
    )
    current_events = {
        registration.event_edition_id
        for registration in registrations
        if registration.is_current_context
    }
    for registration in registrations:
        registration.active_information_request = active_requests.get(registration.pk)
        registration.identity_correction_open = registration.pk in identity_corrections
        registration.identity_rejected = registration.pk in identity_rejected
        registration.restart_option = None
        registration.registered_again = False
        if registration.identity_rejected:
            registration.registered_again = registration.event_edition_id in current_events
            if not registration.registered_again:
                # IDV-Q-C1: the accurate next step for this registration's own
                # origin (open, invitation, or created by staff).
                registration.restart_option = restart_option_for(registration)
        # Owner correction (2026-10-04): a registration the participant
        # withdrew offers the same origin-aware next step. "Registered again"
        # means a current registration of the same origin exists (computed
        # from the rows already loaded); one cancelled by the team says whom
        # to contact instead.
        registration.withdrawn_by_participant = is_participant_withdrawn(registration)
        registration.cancelled_by_team = (
            registration.public_status == RegistrationPublicStatus.WITHDRAWN
            and registration.cancelled_at is not None
        )
        if registration.withdrawn_by_participant:
            registration.registered_again = _has_current_registration_of_same_origin(
                registration, registrations
            )
            if not registration.registered_again:
                registration.restart_option = restart_option_for(registration)
        # Conservative, configurable visibility (Phase 2 Prompt 4 §6):
        # `None` unless the assigned badge type's OWN `participant_visible`
        # flag is explicitly `True` -- never the raw assignment/badge row.
        registration.visible_badge = participant_safe_badge_projection(registration)
        # Attendance days shown WITH the approval status (None unless
        # approved; an unclassified approval reads as pending, never guessed).
        registration.attendance = participant_attendance(registration)
        if registration.public_status == RegistrationPublicStatus.DRAFT:
            registration.channel_open = decide_for_registration(registration).allowed
        accommodation = accommodation_request_for(registration)
        # UX-C2 (G): withdrawable when this registration's request holds
        # details, or when its own submission recorded the consent.
        registration.accommodation_withdrawable = (
            registration.public_status != RegistrationPublicStatus.DRAFT
            and accommodation is not None
            and accommodation.withdrawn_at is None
            and (accommodation.has_sensitive_data or has_sensitive_support_consent(registration))
        )
    # A new public registration is offered only while the public channel is
    # open, or when a valid invitation link is waiting in this session.
    try:
        can_start = public_registration_is_open(current_event_edition())
    except NoOpenEventEdition:
        can_start = False
    pending_invitation = bool(request.session.get("invitation_link_id"))
    can_start = can_start or pending_invitation
    return render(
        request,
        "registrations/workspace.html",
        {
            "registrations": registrations,
            "can_start_registration": can_start,
            # IDV-Q-C1: an invitation opened while signed in can be used even
            # when the person already has registrations (a rejected one, say).
            "pending_invitation": pending_invitation,
        },
    )


@participant_auth.participant_required
@require_http_methods(["POST"])
def start_pending_invitation(request):
    """Start (or resume) a registration from the invitation link the signed-in
    participant just opened (IDV-Q-C1). POST + CSRF. The link is the one the
    invitation page stored after its own check; it is re-validated under lock
    here, and an unusable link gets the same generic page as everywhere."""
    from apps.invitations.models import InvitationLink
    from apps.invitations.services import InvitationLinkUnavailable
    from apps.invitations.views import render_invitation_unavailable

    person = _current_person(request)
    link_id = request.session.pop("invitation_link_id", None)
    link = InvitationLink.objects.filter(pk=link_id).first() if link_id else None
    if link is None:
        return render_invitation_unavailable(request)
    try:
        registration = start_registration_from_invitation(link=link, person=person)
    except InvitationLinkUnavailable:
        return render_invitation_unavailable(request)
    participant_auth.set_active_registration(request, registration.pk)
    if registration.public_status != RegistrationPublicStatus.DRAFT:
        return redirect("registrations:workspace")
    return redirect(_step_url_name(registration.current_step))


@participant_auth.participant_required
@require_http_methods(["POST"])
def register_again(request, pk):
    """Register again from the same account after a final identity rejection
    (owner decision IDV-Q2; origin-aware since IDV-Q-C1) or after the
    participant withdrew the registration (owner correction, 2026-10-04).
    POST + CSRF. The closed registration is never reopened; the participant
    continues in their new (or existing) draft: a public one for an open
    registration, one through the same invitation for an invitation
    registration."""
    from apps.invitations.services import InvitationLinkUnavailable

    person = _current_person(request)
    registration = get_object_or_404(Registration, pk=pk, person=person)
    try:
        if is_participant_withdrawn(registration):
            # Owner correction (2026-10-04): a new registration after a
            # withdrawal; the withdrawn one stays WITHDRAWN with its history.
            draft = start_registration_after_withdrawal(registration=registration, person=person)
        else:
            draft = start_registration_after_identity_rejection(
                registration=registration, person=person
            )
    except RegistrationNotRestartable:
        messages.info(request, _("A new registration is not available for this registration."))
        return redirect("registrations:workspace")
    except InvitationLinkUnavailable:
        messages.info(
            request,
            _(
                "Your invitation can no longer be used. To register again, ask the organization "
                "that invited you for a new invitation, then open it while signed in to this "
                "account."
            ),
        )
        return redirect("registrations:workspace")
    except RegistrationChannelClosed as exc:
        return render_registration_closed(request, exc.decision.reason)
    participant_auth.set_active_registration(request, draft.pk)
    if draft.public_status != RegistrationPublicStatus.DRAFT:
        return redirect("registrations:workspace")
    return redirect(_step_url_name(draft.current_step))


# ---------------------------------------------------------------------------
# Minimum authorized operations intake (UI/UX §8, scoped by
# apps.accounts.selectors.registrations_visible_to -- never a raw queryset).
# ---------------------------------------------------------------------------


@operational_permission_required(
    "registrations.view_registration", login_url="accounts:operational-sign-in"
)
def ops_intake_list(request):
    registrations = registrations_visible_to(request.user).select_related("event_edition")
    # IDV-Q3 procedure: staff find the draft a participant names by its exact
    # registration reference (never a name or an identifier), within scope.
    reference = (request.GET.get("reference") or "").strip()[:32]
    if reference:
        registrations = registrations.filter(public_reference__iexact=reference)
    return render(
        request,
        "registrations/ops_intake_list.html",
        {"registrations": registrations.order_by("-created_at")[:200], "reference": reference},
    )


@operational_permission_required(
    "registrations.view_registration", login_url="accounts:operational-sign-in"
)
def ops_intake_detail(request, pk):
    registration = get_object_or_404(registrations_visible_to(request.user), pk=pk)
    context = {"registration": registration}
    # IDV-Q3: the NIN exemption panel, only for holders of
    # `people.grant_nin_exemption` in this registration's exact scope.
    from apps.people.policies import can_grant_nin_exemption

    if can_grant_nin_exemption(request.user, registration):
        from apps.people.forms.identity import GrantNinExemptionForm, RevokeNinExemptionForm
        from apps.people.models import NinExemptionStatus
        from apps.people.services.nin_exemption import latest_exemption

        exemption = latest_exemption(registration.pk)
        profile = getattr(registration, "profile", None)
        live = exemption is not None and exemption.status in (
            NinExemptionStatus.ACTIVE,
            NinExemptionStatus.USED,
        )
        context.update(
            can_grant_nin_exemption=True,
            nin_exemption=exemption,
            nin_exemption_grantable=(
                registration.public_status == RegistrationPublicStatus.DRAFT
                and not live
                and (profile is None or profile.nationality_code_id == ALGERIA_COUNTRY_CODE)
            ),
            nin_exemption_grant_form=GrantNinExemptionForm(auto_id="id_nin_exemption_%s"),
            nin_exemption_revoke_form=(
                RevokeNinExemptionForm(
                    initial={"expected_version": exemption.version},
                    auto_id="id_nin_revoke_%s",
                )
                if exemption is not None
                and exemption.status == NinExemptionStatus.ACTIVE
                and registration.public_status == RegistrationPublicStatus.DRAFT
                else None
            ),
        )
    return render(request, "registrations/ops_intake_detail.html", context)


# ---------------------------------------------------------------------------
# UX-3 (M21, D-10, S-18): accommodation support
# ---------------------------------------------------------------------------


@operational_permission_required(
    "registrations.coordinate_accommodation_support", login_url="accounts:operational-sign-in"
)
def ops_accommodation_list(request):
    """Accommodation requests in the coordinator's scope.

    Sensitive, so the screen is never cached, each view is audited without
    any content, and it is reachable only with the dedicated permission.
    The legacy free text saved before UX-3 is shown separately and marked.
    """
    from apps.audit import action_codes
    from apps.audit.contracts import AuditRecord
    from apps.audit.services import PersistentAuditRecorder
    from apps.registrations.models import AccommodationCategory
    from apps.registrations.selectors import accommodation_requests_visible_to

    requests_ = list(accommodation_requests_visible_to(request.user)[:200])
    labels = dict(AccommodationCategory.choices)
    for item in requests_:
        item.category_labels = [labels.get(code, code) for code in item.categories]
        profile = getattr(item.registration, "profile", None)
        item.legacy_text = profile.accessibility_needs_text if profile else ""
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=request.user.pk,
            action_code=action_codes.ACCOMMODATION_REQUEST_VIEWED,
            target_type="AccommodationRequest",
            result="SUCCESS",
            after_summary={"rows": len(requests_)},
        )
    )
    response = render(request, "registrations/ops_accommodation_list.html", {"requests": requests_})
    response["Cache-Control"] = "private, no-store"
    return response


@participant_auth.participant_required
@require_http_methods(["POST"])
def withdraw_accommodation(request, pk):
    """The participant withdraws the consent for accommodation data (D-11)."""
    from apps.registrations.services import withdraw_accommodation_consent

    person = _current_person(request)
    registration = get_object_or_404(Registration, pk=pk, person=person)
    if withdraw_accommodation_consent(registration=registration, person=person):
        # UX-C1 (UX-F06): one message for every outcome. It never promises a
        # deletion that a legal hold may prevent, and never reveals whether a
        # hold applies; it repeats the draft privacy notice's own wording.
        messages.success(
            request,
            _(
                "Your consent is withdrawn. The support team can no longer see your "
                "accessibility support details. They are deleted, unless a legal hold "
                "requires keeping them."
            ),
        )
    return redirect("registrations:workspace")
