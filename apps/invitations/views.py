"""Public invitation entry, on-behalf claim, and Organization Workspace views
(Phase 2 Prompt 2; TRD §9.2, §10.1)."""

from __future__ import annotations

import hashlib
import secrets

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts import participant_auth
from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.events.selectors import NoOpenEventEdition, current_event_edition
from apps.organizations.selectors import organizations_visible_to, search_organizations
from apps.people.models import ContactPointStatus, ContactPointType, Person

from .forms import CampaignForm, DelegationUploadForm, OnBehalfDraftForm
from .selectors import (
    campaigns_visible_to,
    delegation_batches_visible_to,
    organization_workspace_registrations,
)
from .services import (
    ClaimUnavailable,
    IllegalCampaignTransitionError,
    InvitationLinkUnavailable,
    NoActiveLinkError,
    apply_delegation_batch,
    change_campaign_status,
    claim_on_behalf_registration_by_hash,
    create_campaign,
    create_on_behalf_draft,
    hash_claim_token,
    issue_initial_link,
    resolve_invitation_link,
    rotate_link,
    upload_delegation_batch,
)

# Session key for a claim token's HASH, carried across the OTP redirect for
# an unauthenticated visitor who opened a claim link (Phase 2 Prompt 2 V2
# correction pass "make the participant claim journey survive OTP
# authentication"). Never the raw token -- a one-way, non-secret reference
# only, resolved back to a claim row after authentication succeeds.
PENDING_CLAIM_SESSION_KEY = "pending_claim_token_hash"

_GENERIC_UNAVAILABLE = _(
    "This invitation link is not available. It may be invalid, expired, or no longer active."
)


def _get_scoped_organization_or_404(
    request, *, organization_id, app_label: str, codename: str, event_edition_id=None
):
    """Scope-filtered organization lookup that also audits a scope denial
    BEFORE raising `Http404` (Phase 2 Prompt 2 correction pass
    "relevant rejected security-sensitive operations"). The 404 itself
    still reveals nothing -- the audit trail is internal-only.

    `event_edition_id` is threaded through to `organizations_visible_to`
    (Phase 2 Prompt 2 V2 correction pass "preserve event scope in
    organization actions") so a membership scoped to a past event alone
    never authorizes acting on an organization for the CURRENT event.
    """
    from django.http import Http404

    organization = (
        organizations_visible_to(
            request.user,
            app_label=app_label,
            codename=codename,
            event_edition_id=event_edition_id,
        )
        .filter(pk=organization_id)
        .first()
    )
    if organization is None:
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(request.user, "pk", None),
                action_code=action_codes.ORGANIZATION_SCOPE_DENIED,
                target_type="Organization",
                target_uuid=organization_id,
                result="DENIED",
                reason_code=f"{app_label}.{codename}",
            )
        )
        raise Http404("No Organization matches the given query.")
    return organization


def _record_workspace_detail_view(user, *, target_type: str, target_uuid) -> None:
    """Audit access to a sensitive Organization Workspace detail screen
    (Phase 2 Prompt 2 correction pass "organization workspace
    sensitive-detail access"). Bounded: no CSV content, no email, no
    token -- only the fact that this actor viewed this exact record."""
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(user, "pk", None),
            action_code=action_codes.ORGANIZATION_WORKSPACE_DETAIL_VIEWED,
            target_type=target_type,
            target_uuid=target_uuid,
            result="SUCCESS",
        )
    )


# ---------------------------------------------------------------------------
# Public invitation entry (AF-ORG-02)
# ---------------------------------------------------------------------------


def render_invitation_unavailable(request):
    """The one generic "this invitation is not available" response, shared by
    every rejection point (initial resolution AND later re-validation at
    consumption time, Phase 2 Prompt 2 correction pass) -- never reveals
    which specific condition failed."""
    # UX-4 (D-12): open registration is offered only while the public channel
    # is really open; in INVITATION_ONLY and CLOSED the fallback is suppressed.
    from apps.events.policies.registration_channels import public_registration_is_open

    try:
        open_registration_available = public_registration_is_open(current_event_edition())
    except NoOpenEventEdition:
        open_registration_available = False
    return render(
        request,
        "invitations/invitation_unavailable.html",
        {"open_registration_available": open_registration_available},
    )


def invitation_start(request, token: str):
    """Resolve an invitation link and route to participant OTP sign-in.

    On failure: ALWAYS the same generic message (Phase 2 Prompt 2 "do not
    reveal the reason"); offers open registration only when the public
    registration window is currently open, and never preserves any
    invalid campaign/organization provenance. This is only a FIRST check --
    the link and campaign are re-validated again, atomically and under
    lock, at actual draft-creation time (`_get_or_start_draft`), since a
    link valid here could be revoked/rotated/expired before it is consumed.
    """
    try:
        link = resolve_invitation_link(token)
    except InvitationLinkUnavailable:
        return render_invitation_unavailable(request)

    request.session["invitation_link_id"] = str(link.pk)
    return redirect("accounts:otp-request")


# ---------------------------------------------------------------------------
# On-behalf claim (AF-ORG-04)
# ---------------------------------------------------------------------------


def _resume_on_behalf_claim(request, *, token_hash: str):
    """Shared claim-resolution body for both the direct (already
    authenticated) path and the post-OTP resume path (Phase 2 Prompt 2 V2
    correction pass "make the participant claim journey survive OTP
    authentication"). Operates on the claim token's HASH only -- never the
    raw token, which is never persisted in the session.
    """
    person_id = participant_auth.get_valid_participant_person_id(request)
    if person_id is None:
        return redirect("accounts:otp-request")
    person = get_object_or_404(Person, pk=person_id)
    contact = person.contact_points.filter(
        type=ContactPointType.EMAIL,
        is_verified=True,
        login_enabled=True,
        status=ContactPointStatus.ACTIVE,
    ).first()
    if contact is None:
        return render(request, "invitations/claim_unavailable.html", status=400)

    from apps.registrations.services import DeduplicationConflictError

    try:
        registration = claim_on_behalf_registration_by_hash(
            token_hash=token_hash,
            verified_email=contact.value_encrypted,
            person=person,
        )
    except ClaimUnavailable:
        return render(request, "invitations/claim_unavailable.html")
    except DeduplicationConflictError:
        # A controlled conflict, never an HTTP 500 (Phase 2 Prompt 2): the
        # participant already has another current Registration Context that
        # collides once claimed. Support must resolve this manually.
        return render(request, "invitations/claim_conflict.html")

    participant_auth.set_active_registration(request, registration.pk)
    return redirect("registrations:step-identity")


def on_behalf_claim(request, token: str):
    """Entry point for a `/claim/<token>/` link.

    An unauthenticated visitor is redirected to OTP WITHOUT losing the
    claim (Phase 2 Prompt 2 V2 correction pass "make the participant claim
    journey survive OTP authentication"): only the token's non-secret HASH
    is stashed in the session, never the raw token itself, and
    `otp_verify` resumes the claim automatically after a successful
    authentication via `on_behalf_claim_resume`. An already-authenticated
    participant resolves the claim immediately, exactly as before.
    """
    token_hash = hash_claim_token(token)
    if not participant_auth.is_participant_session_valid(request):
        request.session[PENDING_CLAIM_SESSION_KEY] = token_hash
        return redirect("accounts:otp-request")
    return _resume_on_behalf_claim(request, token_hash=token_hash)


@participant_auth.participant_required
def on_behalf_claim_resume(request):
    """Resume a claim stashed in the session by `on_behalf_claim`, reached
    only via `otp_verify`'s post-authentication redirect (Phase 2 Prompt 2
    V2 correction pass). A direct hit with nothing pending falls back to
    the ordinary post-sign-in landing page, never an error."""
    token_hash = request.session.pop(PENDING_CLAIM_SESSION_KEY, None)
    if not token_hash:
        return redirect("registrations:workspace")
    return _resume_on_behalf_claim(request, token_hash=token_hash)


# ---------------------------------------------------------------------------
# Organization Workspace
# ---------------------------------------------------------------------------


@operational_permission_required(
    "invitations.view_invitationcampaign", login_url="accounts:operational-sign-in"
)
def workspace_dashboard(request):
    campaigns = campaigns_visible_to(request.user).select_related("event_edition", "organization")[
        :50
    ]
    batches = delegation_batches_visible_to(request.user).select_related("organization")[:50]
    return render(
        request,
        "invitations/workspace_dashboard.html",
        {"campaigns": campaigns, "batches": batches},
    )


@operational_permission_required(
    "registrations.view_registration", login_url="accounts:operational-sign-in"
)
def workspace_registrations(request):
    registrations = organization_workspace_registrations(request.user).select_related(
        "event_edition"
    )[:200]
    return render(
        request, "invitations/workspace_registrations.html", {"registrations": registrations}
    )


@operational_permission_required(
    "invitations.add_invitationcampaign", login_url="accounts:operational-sign-in"
)
def organization_search(request):
    """Search organizations, intersected with the caller's OWN scope.

    Never searches the whole table merely because the caller holds
    `add_invitationcampaign` through SOME membership (Phase 2 Prompt 2
    correction pass): a user scoped to a specific organization sees only
    that organization in results, even for a query that would otherwise
    match many others. A user with a broad (NULL-organization) membership,
    or a superuser, still sees every match -- exactly the same rule
    `organizations_visible_to` already applies everywhere else.

    Event-scoped (Phase 2 Prompt 2 V2 correction pass "preserve event
    scope in organization actions"): a membership scoped to a past event
    alone must never surface an organization as searchable for the
    CURRENT event. Scope is intersected INSIDE `search_organizations`
    itself, before ordering/slicing -- never by filtering its result
    afterwards, which Django rejects once a queryset has been sliced
    (Phase 2 Prompt 2 V2 correction pass "fix organization search
    execution").
    """
    query = request.GET.get("q", "")
    try:
        event_edition = current_event_edition()
    except NoOpenEventEdition:
        return render(
            request, "invitations/organization_search.html", {"query": query, "results": []}
        )
    visible_ids = organizations_visible_to(
        request.user,
        app_label="invitations",
        codename="add_invitationcampaign",
        event_edition_id=event_edition.pk,
    )
    organizations = search_organizations(query, visible_ids=visible_ids) if query else []
    # Authorization-aware UI (Phase 2 Prompt 2 correction pass): campaign
    # creation, delegation upload and on-behalf registration are THREE
    # separate permissions -- holding one for an organization never implies
    # the others, so each control is gated independently per result.
    results = [
        {
            "organization": organization,
            "can_upload_delegation": has_scoped_permission(
                request.user,
                "invitations.add_delegationbatch",
                event_edition_id=event_edition.pk,
                organization_id=organization.pk,
            ),
            "can_register_on_behalf": has_scoped_permission(
                request.user,
                "registrations.register_on_behalf",
                event_edition_id=event_edition.pk,
                organization_id=organization.pk,
            ),
        }
        for organization in organizations
    ]
    return render(
        request, "invitations/organization_search.html", {"query": query, "results": results}
    )


@operational_permission_required(
    "invitations.add_invitationcampaign", login_url="accounts:operational-sign-in"
)
def campaign_create(request, organization_id):
    # The current event is resolved FIRST, then threaded into the scoped
    # organization lookup (Phase 2 Prompt 2 V2 correction pass "preserve
    # event scope in organization actions") -- a membership scoped only to
    # a past event must never authorize creating a campaign for the
    # CURRENT event, even for an organization the caller otherwise has
    # this permission for.
    try:
        event_edition = current_event_edition()
    except NoOpenEventEdition:
        messages.error(request, _("No event edition is currently open."))
        return redirect("invitations:workspace-dashboard")

    # Scope-filtered lookup, never a global `Organization.objects.get`: an
    # organization outside the caller's scope 404s exactly like one that
    # does not exist at all -- never a 403 that would confirm the UUID is
    # real (Phase 2 Prompt 2 correction pass "eliminate organization
    # existence leaks").
    organization = _get_scoped_organization_or_404(
        request,
        organization_id=organization_id,
        app_label="invitations",
        codename="add_invitationcampaign",
        event_edition_id=event_edition.pk,
    )

    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        import uuid

        campaign = create_campaign(
            event_edition=event_edition,
            organization=organization,
            name=form.cleaned_data["name"],
            public_reference=f"CAMP-{uuid.uuid4().hex[:10].upper()}",
            valid_from=form.cleaned_data.get("valid_from"),
            valid_until=form.cleaned_data.get("valid_until"),
            capacity=form.cleaned_data.get("capacity"),
            language=form.cleaned_data["language"],
            created_by=request.user,
        )
        return redirect("invitations:campaign-detail", pk=campaign.pk)
    return render(
        request, "invitations/campaign_create.html", {"form": form, "organization": organization}
    )


def _get_visible_campaign_or_404(request, pk, *, codename: str = "view_invitationcampaign"):
    """Scope-filtered campaign lookup for the EXACT action being performed.

    `codename` MUST be the specific mutation's own permission for a
    mutation view (Phase 2 Prompt 2 V2 correction pass "enforce
    action-specific object scope on every mutation") -- the previous
    default of always using the VIEW permission let a user with
    change-scope for organization A, but only view-scope for organization
    B, load and then mutate a campaign belonging to B, since
    `operational_permission_required` only proves the permission is held
    through SOME membership, never that it applies to THIS object.
    """
    return get_object_or_404(campaigns_visible_to(request.user, codename=codename), pk=pk)


@operational_permission_required(
    "invitations.view_invitationcampaign", login_url="accounts:operational-sign-in"
)
def campaign_detail(request, pk):
    campaign = _get_visible_campaign_or_404(request, pk)
    _record_workspace_detail_view(
        request.user, target_type="InvitationCampaign", target_uuid=campaign.pk
    )
    active_link = campaign.links.filter(status="ACTIVE").first()
    issued_url = request.session.pop(f"issued_link_url:{campaign.pk}", None)
    scope = {
        "event_edition_id": campaign.event_edition_id,
        "organization_id": campaign.organization_id,
    }
    return render(
        request,
        "invitations/campaign_detail.html",
        {
            "campaign": campaign,
            "active_link": active_link,
            "issued_url": issued_url,
            # Authorization-aware UI (Phase 2 Prompt 2 correction pass): the
            # server-side decorator on each POST endpoint remains the real
            # enforcement -- these flags only decide whether to RENDER a
            # control the user could not use anyway.
            "can_issue_link": has_scoped_permission(
                request.user, "invitations.add_invitationlink", **scope
            ),
            "can_rotate_link": has_scoped_permission(
                request.user, "invitations.change_invitationlink", **scope
            ),
            "can_change_status": has_scoped_permission(
                request.user, "invitations.change_invitationcampaign", **scope
            ),
        },
    )


@operational_permission_required(
    "invitations.change_invitationcampaign", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def campaign_change_status(request, pk):
    campaign = _get_visible_campaign_or_404(request, pk, codename="change_invitationcampaign")
    new_status = request.POST.get("new_status", "")
    try:
        change_campaign_status(campaign, new_status, actor=request.user)
    except IllegalCampaignTransitionError:
        messages.error(request, _("That status change is not allowed from the current status."))
    return redirect("invitations:campaign-detail", pk=campaign.pk)


@operational_permission_required(
    "invitations.add_invitationlink", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def campaign_issue_link(request, pk):
    campaign = _get_visible_campaign_or_404(request, pk, codename="add_invitationlink")
    link, raw_token = issue_initial_link(campaign, actor=request.user)
    url = request.build_absolute_uri(f"/invite/{raw_token}/")
    request.session[f"issued_link_url:{campaign.pk}"] = url
    return redirect("invitations:campaign-detail", pk=campaign.pk)


@operational_permission_required(
    "invitations.change_invitationlink", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def campaign_rotate_link(request, pk):
    campaign = _get_visible_campaign_or_404(request, pk, codename="change_invitationlink")
    try:
        _link, raw_token = rotate_link(campaign, actor=request.user)
    except NoActiveLinkError:
        messages.error(request, _("This campaign has no active link to rotate."))
        return redirect("invitations:campaign-detail", pk=campaign.pk)
    url = request.build_absolute_uri(f"/invite/{raw_token}/")
    request.session[f"issued_link_url:{campaign.pk}"] = url
    return redirect("invitations:campaign-detail", pk=campaign.pk)


@operational_permission_required(
    "invitations.add_delegationbatch", login_url="accounts:operational-sign-in"
)
def delegation_upload(request, organization_id):
    # Event resolved first; see `campaign_create` (Phase 2 Prompt 2 V2
    # correction pass "preserve event scope in organization actions").
    try:
        event_edition = current_event_edition()
    except NoOpenEventEdition:
        messages.error(request, _("No event edition is currently open."))
        return redirect("invitations:workspace-dashboard")

    organization = _get_scoped_organization_or_404(
        request,
        organization_id=organization_id,
        app_label="invitations",
        codename="add_delegationbatch",
        event_edition_id=event_edition.pk,
    )

    # A server-issued, session-bound, per-(organization, event) form token
    # (Phase 2 Prompt 2 correction pass "make delegation upload genuinely
    # idempotent"): combined with the organization, event and the file's
    # own content digest to derive a deterministic idempotency key -- NOT
    # used as an authorization secret in its own right (the view/service
    # still separately enforces scope and permission). Rotated only after
    # a submission is processed, so a genuine retry of the SAME rendered
    # form (double-click, network retry) always carries the SAME token
    # and converges on the SAME batch, while a fresh page load later gets
    # a new token, allowing a deliberately new re-upload of even identical
    # content.
    session_key = f"delegation_form_marker:{organization.pk}:{event_edition.pk}"
    form_token = request.session.get(session_key)
    if not form_token:
        form_token = secrets.token_hex(16)
        request.session[session_key] = form_token

    form = DelegationUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        submitted_token = request.POST.get("form_token", "")
        uploaded = form.cleaned_data["csv_file"]
        csv_bytes = uploaded.read()
        content_digest = hashlib.sha256(csv_bytes).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{organization.pk}:{event_edition.pk}:{submitted_token}:{content_digest}".encode()
        ).hexdigest()
        batch = upload_delegation_batch(
            organization=organization,
            event_edition=event_edition,
            csv_bytes=csv_bytes,
            uploaded_filename=uploaded.name,
            created_by=request.user,
            idempotency_key=idempotency_key,
        )
        request.session.pop(session_key, None)
        return redirect("invitations:delegation-detail", pk=batch.pk)
    return render(
        request,
        "invitations/delegation_upload.html",
        {"form": form, "organization": organization, "form_token": form_token},
    )


def _masked_email(encrypted_email: str) -> str:
    """Mask a delegation row's decrypted candidate email for display
    (UI/UX §8.7 "mask identity and contact values by default").

    A plain string-in/string-out helper -- deliberately NOT a view, and
    never decorated. It is invoked directly against a row's decrypted
    email value, never against an `HttpRequest` (Phase 2 Prompt 2
    correction: this decorator was previously mis-attached here instead
    of on `delegation_detail`, leaving that view with NO permission
    check at all).
    """
    if not encrypted_email or "@" not in encrypted_email:
        return ""
    local, domain = encrypted_email.split("@", 1)
    return f"{local[:1]}***@{domain}"


@operational_permission_required(
    "invitations.view_delegationbatch", login_url="accounts:operational-sign-in"
)
def delegation_detail(request, pk):
    batch = get_object_or_404(delegation_batches_visible_to(request.user), pk=pk)
    _record_workspace_detail_view(request.user, target_type="DelegationBatch", target_uuid=batch.pk)
    rows = [
        {"row": row, "masked_email": _masked_email(row.candidate_email_encrypted)}
        for row in batch.rows.order_by("row_number")[:500]
    ]
    can_apply = has_scoped_permission(
        request.user,
        "invitations.add_delegationbatch",
        event_edition_id=batch.event_edition_id,
        organization_id=batch.organization_id,
    )
    return render(
        request,
        "invitations/delegation_detail.html",
        {"batch": batch, "rows": rows, "can_apply": can_apply},
    )


@operational_permission_required(
    "invitations.add_delegationbatch", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def delegation_apply(request, pk):
    # Scoped by the MUTATION's own permission, not the view-only default
    # (Phase 2 Prompt 2 V2 correction pass "enforce action-specific object
    # scope on every mutation") -- see `_get_visible_campaign_or_404`.
    batch = get_object_or_404(
        delegation_batches_visible_to(request.user, codename="add_delegationbatch"), pk=pk
    )
    from apps.events.policies.registration_channels import RegistrationChannelClosed

    try:
        apply_delegation_batch(batch, actor=request.user)
    except RegistrationChannelClosed:
        messages.error(
            request,
            _("Registration is closed for this event. No draft was created."),
        )
    return redirect("invitations:delegation-detail", pk=batch.pk)


@operational_permission_required(
    "registrations.register_on_behalf", login_url="accounts:operational-sign-in"
)
def on_behalf_create(request, organization_id):
    # Event resolved first; see `campaign_create` (Phase 2 Prompt 2 V2
    # correction pass "preserve event scope in organization actions").
    try:
        event_edition = current_event_edition()
    except NoOpenEventEdition:
        messages.error(request, _("No event edition is currently open."))
        return redirect("invitations:workspace-dashboard")

    organization = _get_scoped_organization_or_404(
        request,
        organization_id=organization_id,
        app_label="registrations",
        codename="register_on_behalf",
        event_edition_id=event_edition.pk,
    )

    form = OnBehalfDraftForm(request.POST or None)
    claim_url = None
    if request.method == "POST" and form.is_valid():
        from apps.events.policies.registration_channels import RegistrationChannelClosed

        try:
            _registration, raw_token = create_on_behalf_draft(
                event_edition=event_edition,
                source_organization=organization,
                created_by=request.user,
                intended_email=form.cleaned_data["intended_email"],
                preferred_language=get_language() or "en",
            )
        except RegistrationChannelClosed:
            form.add_error(None, _("Registration is closed for this event. No draft was created."))
        else:
            claim_url = request.build_absolute_uri(f"/claim/{raw_token}/")
            form = OnBehalfDraftForm()
    return render(
        request,
        "invitations/on_behalf_create.html",
        {"form": form, "organization": organization, "claim_url": claim_url},
    )
