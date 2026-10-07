"""Digital Entry Pass views (Phase 3 Prompt 2).

Views coordinate HTTP only: every state change lives in
`apps.badges.services`. Every mutation is POST-only, CSRF-protected,
permission-checked through the shared scoped primitive, and carries both an
idempotency key and an expected lock version.

These templates are deliberately semantic and minimal. They extend the
existing base layout and do NOT introduce a workspace shell or finalize any
design system -- that is the later Prompt 5 visual pass, and these
interfaces are written to stay compatible with it.

No gate admission decision and no EntryEvent is produced anywhere here.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts import participant_auth
from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.accounts.selectors import scope_filtered_queryset
from apps.accreditation.attendance import participant_attendance
from apps.accreditation.models import AssignmentStatus, BadgeType, BadgeTypeAssignment
from apps.badges import references
from apps.badges.credentials.qr import QrRenderError, render_png
from apps.badges.forms import ISSUANCE_REPLACE_REASON_CODES as _ISSUANCE_REPLACE_REASON_CODES
from apps.badges.forms import ISSUANCE_RETURN_REASON_CODES as _ISSUANCE_RETURN_REASON_CODES
from apps.badges.forms import (
    KEY_RETIREMENT_REASON_CODES,
    KEY_REVOCATION_REASON_CODES,
    ActivatePassForm,
    AllocateStockForm,
    ChangeBatchStatusForm,
    CreatePrintBatchForm,
    CreateStockLocationForm,
    FallbackReferenceLookupForm,
    GeneratePassForm,
    IssueBadgeForm,
    MarkIssuanceLostForm,
    PublishVerificationKeyForm,
    ReasonedLifecycleForm,
    ReceivePrintBatchForm,
    RecordAdjustmentForm,
    RecordAttendanceMarkingForm,
    RecordReconciliationForm,
    ReleaseAllocationForm,
    ReplaceIssuanceForm,
    ReplacePassForm,
    ReturnIssuanceForm,
    RevokeVerificationKeyForm,
    TransferStockForm,
    VerificationKeyLifecycleForm,
    VoidIssuanceForm,
    adjustment_reason_choices,
    allocation_purpose_choices,
    credential_reason_choices,
    issuance_reason_choices,
    key_reason_choices,
)
from apps.badges.models import (
    NON_TERMINAL_STATUSES,
    BadgeIssuance,
    BadgeIssuanceStatus,
    BadgeStockAllocation,
    DigitalEntryPass,
    DigitalEntryPassStatus,
    PrintBatch,
    PrintBatchStatus,
    StockLocation,
    StockLocationType,
    VerificationKey,
)
from apps.badges.presentation import service_error_message
from apps.badges.selectors import (
    active_allocations_for,
    active_verification_key,
    allocations_available_for_issuance,
    credential_history_for_registration,
    current_pass_for_registration,
    event_balances,
    issuances_visible_to,
    ledger_entries_for,
    participant_displayable_passes,
    passes_visible_to,
    print_batches_visible_to,
    reconciliations_for,
    series_for_registration,
    stock_locations_visible_to,
    transfers_for,
)
from apps.badges.services import (
    CrossEventScopeError,
    DuplicateStockLocationError,
    FallbackLookupThrottled,
    InsufficientStockError,
    OperationConflictError,
    PassConcurrencyError,
    PassConfigurationError,
    PassNotEligibleError,
    PassServiceError,
    PassStateError,
    StockConcurrencyError,
    StockServiceError,
    VerificationKeyError,
    WrongBadgeTypeError,
    activate_pass,
    allocate_stock,
    change_batch_status,
    create_print_batch,
    create_stock_location,
    generate_pass,
    issue_badge,
    issue_pass_token,
    lookup_series_by_fallback_reference,
    mark_issuance_lost,
    new_operation_id,
    pass_exposes_usable_qr,
    promote_verification_key,
    publish_verification_key,
    receive_print_batch,
    record_adjustment,
    record_attendance_marking,
    record_reconciliation,
    release_allocation,
    replace_issuance,
    replace_pass,
    resume_pass,
    retire_verification_key,
    return_issuance,
    revoke_pass,
    revoke_verification_key,
    suspend_pass,
    transfer_stock,
    void_issuance,
)
from apps.core.concurrency import resolve_request_client_network_identity
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.registrations.models import Registration
from apps.registrations.selectors import is_active_approved_context

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

#: Localized, safe participant-facing status text. Suspended and revoked
#: deliberately carry the same informational content so the participant
#: surface does not disclose which one applies.
#:
#: `gettext_lazy` is required here, not `gettext`: this dict is built once
#: at import time, so an eager translation would freeze whichever language
#: happened to be active during startup and render English inside an Arabic
#: page. Lazy strings resolve per request instead.
PARTICIPANT_STATUS_MESSAGES = {
    DigitalEntryPassStatus.INACTIVE: gettext_lazy(
        "Your entry pass has been prepared and is not yet active."
    ),
    DigitalEntryPassStatus.ACTIVE: gettext_lazy("Your entry pass is active."),
    DigitalEntryPassStatus.SUSPENDED: gettext_lazy(
        "Your entry pass is temporarily on hold. Please contact the accreditation desk."
    ),
    DigitalEntryPassStatus.REVOKED: gettext_lazy(
        "Your entry pass is no longer valid. Please contact the accreditation desk."
    ),
    DigitalEntryPassStatus.EXPIRED: gettext_lazy("Your entry pass validity period has ended."),
}

# An ACTIVE row may legitimately predate its `valid_from` instant. The QR is
# correctly withheld in that state, but it must not be described as expired:
# the validity window has not started yet and no database mutation is needed.
PARTICIPANT_NOT_YET_VALID_MESSAGE = gettext_lazy(
    "Your entry pass is active, but its validity period has not started yet."
)


def _conflict_response(request, message: str) -> HttpResponse:
    messages.error(request, message)
    return render(request, "badges/conflict.html", {"message": message}, status=409)


def _scoped_registration_or_404(request, pk, *, codename: str) -> Registration:
    queryset = scope_filtered_queryset(
        request.user,
        Registration.objects,
        app_label="badges",
        codename=codename,
        organization_field="source_organization_id",
    )
    return get_object_or_404(queryset.select_related("event_edition"), pk=pk)


def _scoped_pass_or_404(request, pk, *, codename: str) -> DigitalEntryPass:
    return get_object_or_404(passes_visible_to(request.user, codename=codename), pk=pk)


def _redirect_to_credential(registration_id) -> HttpResponse:
    return redirect("badges:registration-credential", pk=registration_id)


# ---------------------------------------------------------------------------
# Operational: credential detail and lifecycle commands
# ---------------------------------------------------------------------------


@operational_permission_required(
    "badges.view_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["GET"])
def registration_credential(request, pk):
    """Operational credential view for one registration context."""
    registration = _scoped_registration_or_404(request, pk, codename="view_digitalentrypass")
    scope = {
        "event_edition_id": registration.event_edition_id,
        "organization_id": registration.source_organization_id,
    }
    current = current_pass_for_registration(registration)
    series = series_for_registration(registration)
    context = {
        "registration": registration,
        "current_pass": current,
        "series": series,
        "fallback_reference_display": (
            references.format_fallback_reference(series.fallback_reference) if series else ""
        ),
        "history": credential_history_for_registration(registration),
        # A DISTINCT identifier per action form. One shared value would let a
        # replayed suspend submission be matched against a revoke command.
        "generate_operation_id": new_operation_id(),
        "activate_operation_id": new_operation_id(),
        "suspend_operation_id": new_operation_id(),
        "resume_operation_id": new_operation_id(),
        "revoke_operation_id": new_operation_id(),
        "replace_operation_id": new_operation_id(),
        "can_generate": has_scoped_permission(request.user, "badges.add_digitalentrypass", **scope),
        "can_activate": has_scoped_permission(
            request.user, "badges.activate_digitalentrypass", **scope
        ),
        "can_suspend": has_scoped_permission(
            request.user, "badges.suspend_digitalentrypass", **scope
        ),
        "can_resume": has_scoped_permission(
            request.user, "badges.resume_digitalentrypass", **scope
        ),
        "can_revoke": has_scoped_permission(
            request.user, "badges.revoke_digitalentrypass", **scope
        ),
        "status_choices": DigitalEntryPassStatus,
        # Server-side allowlists; the template renders exactly these and
        # the form rejects anything else.
        "suspend_reason_choices": credential_reason_choices("suspend"),
        "resume_reason_choices": credential_reason_choices("resume"),
        "revoke_reason_choices": credential_reason_choices("revoke"),
        "replace_reason_choices": credential_reason_choices("replace"),
    }
    return render(request, "badges/registration_credential.html", context)


@operational_permission_required(
    "badges.add_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def credential_generate(request, pk):
    registration = _scoped_registration_or_404(request, pk, codename="add_digitalentrypass")
    form = GeneratePassForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        outcome = generate_pass(
            registration=registration,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
        )
    except PassNotEligibleError as exc:
        # The reason code is mapped to a translated label, never shown raw (P8-01).
        messages.error(request, service_error_message(exc))
        return _redirect_to_credential(registration.pk)
    except (PassStateError, PassConfigurationError) as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_credential(registration.pk)

    if outcome.replayed:
        messages.info(request, _("This request was already processed."))
    else:
        messages.success(request, _("Entry pass generated."))
    return _redirect_to_credential(registration.pk)


def _lifecycle_command(
    request, pk, *, codename, form_class, runner, success_message, form_action=None
):
    """Shared POST handling for the simple lifecycle transitions."""
    credential = _scoped_pass_or_404(request, pk, codename=codename)
    form = form_class(request.POST, action=form_action) if form_action else form_class(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        outcome = runner(credential, form.cleaned_data)
    except PassConcurrencyError:
        return _conflict_response(
            request, _("This entry pass changed since you loaded it. Reload and try again.")
        )
    except (PassStateError, PassNotEligibleError, PassConfigurationError) as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_credential(credential.registration_id)
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except PassServiceError as exc:  # pragma: no cover - defensive
        messages.error(request, service_error_message(exc))
        return _redirect_to_credential(credential.registration_id)

    if outcome.replayed:
        messages.info(request, _("This request was already processed."))
    else:
        messages.success(request, success_message)
    return _redirect_to_credential(credential.registration_id)


@operational_permission_required(
    "badges.activate_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def credential_activate(request, pk):
    return _lifecycle_command(
        request,
        pk,
        codename="activate_digitalentrypass",
        form_class=ActivatePassForm,
        runner=lambda credential, data: activate_pass(
            credential=credential,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
        ),
        success_message=_("Entry pass activated."),
    )


@operational_permission_required(
    "badges.suspend_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def credential_suspend(request, pk):
    return _lifecycle_command(
        request,
        pk,
        codename="suspend_digitalentrypass",
        form_class=ReasonedLifecycleForm,
        form_action="suspend",
        runner=lambda credential, data: suspend_pass(
            credential=credential,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_code=data["reason_code"],
            reason_text=data["reason_text"],
        ),
        success_message=_("Entry pass suspended."),
    )


@operational_permission_required(
    "badges.resume_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def credential_resume(request, pk):
    return _lifecycle_command(
        request,
        pk,
        codename="resume_digitalentrypass",
        form_class=ReasonedLifecycleForm,
        form_action="resume",
        runner=lambda credential, data: resume_pass(
            credential=credential,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_code=data["reason_code"],
            reason_text=data["reason_text"],
        ),
        success_message=_("Entry pass resumed."),
    )


@operational_permission_required(
    "badges.revoke_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def credential_revoke(request, pk):
    return _lifecycle_command(
        request,
        pk,
        codename="revoke_digitalentrypass",
        form_class=ReasonedLifecycleForm,
        form_action="revoke",
        runner=lambda credential, data: revoke_pass(
            credential=credential,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_code=data["reason_code"],
            reason_text=data["reason_text"],
        ),
        success_message=_("Entry pass revoked."),
    )


@operational_permission_required(
    "badges.revoke_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def credential_replace(request, pk):
    credential = _scoped_pass_or_404(request, pk, codename="revoke_digitalentrypass")
    form = ReplacePassForm(request.POST, action="replace")
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        outcome = replace_pass(
            credential=credential,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            expected_jti=form.cleaned_data["expected_jti"],
            expected_lock_version=form.cleaned_data["expected_lock_version"],
            reason_code=form.cleaned_data["reason_code"],
            reason_text=form.cleaned_data["reason_text"],
        )
    except PassConcurrencyError:
        return _conflict_response(
            request,
            _("This entry pass is no longer the current one. Reload and try again."),
        )
    except (PassStateError, PassNotEligibleError, PassConfigurationError) as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_credential(credential.registration_id)

    if outcome.replayed:
        messages.info(request, _("This request was already processed."))
    else:
        messages.success(request, _("Entry pass replaced."))
    return _redirect_to_credential(credential.registration_id)


# ---------------------------------------------------------------------------
# Operational: controlled fallback-reference lookup
# ---------------------------------------------------------------------------


@operational_permission_required(
    "badges.view_digitalentrypass", login_url="accounts:operational-sign-in"
)
@never_cache
@require_http_methods(["GET", "POST"])
def fallback_reference_lookup(request):
    """Exact-match-only lookup of a participant fallback reference.

    There is deliberately no listing, no pagination, and no partial match:
    this is a locator for a reference the operator already holds, not a
    participant browse surface. A successful lookup grants nothing.
    """
    form = FallbackReferenceLookupForm(request.POST or None)
    series = None
    searched = False
    if request.method == "POST" and form.is_valid():
        searched = True
        try:
            series = lookup_series_by_fallback_reference(
                raw_reference=form.cleaned_data["reference"],
                actor=request.user,
                # Resolved through the project's trusted-proxy helper, never
                # read straight from a forwarding header a client controls.
                # The service turns it into a keyed fingerprint; no raw
                # address is stored anywhere.
                network_identity=resolve_request_client_network_identity(request),
            )
        except FallbackLookupThrottled as exc:
            form.add_error(None, service_error_message(exc))
            searched = False
    return render(
        request,
        "badges/fallback_lookup.html",
        {
            "form": form,
            "series": series,
            "searched": searched,
            "not_found": searched and series is None,
        },
    )


# ---------------------------------------------------------------------------
# Operational: verification keys
# ---------------------------------------------------------------------------


@operational_permission_required(
    "badges.manage_verificationkey", login_url="accounts:operational-sign-in"
)
@require_http_methods(["GET", "POST"])
def verification_keys(request):
    """Publish and inspect PUBLIC verification keys.

    Private key material is never accepted, never displayed, and never
    stored: the service refuses any PEM carrying a private-key marker.
    """
    form = PublishVerificationKeyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            publish_verification_key(
                key_id=form.cleaned_data["key_id"],
                public_key_pem=form.cleaned_data["public_key_pem"],
                actor=request.user,
            )
            messages.success(request, _("Verification key published."))
            return redirect("badges:verification-keys")
        except VerificationKeyError as exc:
            form.add_error(None, service_error_message(exc))
    # Each control on the page gets its own operation identifier, so no two
    # forms can ever submit the same one.
    keys = list(VerificationKey.objects.order_by("key_id"))
    for key in keys:
        key.promote_operation_id = new_operation_id()
        key.retire_operation_id = new_operation_id()
        key.revoke_operation_id = new_operation_id()
    return render(
        request,
        "badges/verification_keys.html",
        {
            "form": form,
            "keys": keys,
            "active_key": active_verification_key(),
            "retire_reason_choices": key_reason_choices(KEY_RETIREMENT_REASON_CODES),
            "revoke_reason_choices": key_reason_choices(KEY_REVOCATION_REASON_CODES),
        },
    )


@operational_permission_required(
    "badges.manage_verificationkey", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def verification_key_promote(request, pk):
    key = get_object_or_404(VerificationKey, pk=pk)
    form = VerificationKeyLifecycleForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        promote_verification_key(
            key=key, actor=request.user, operation_id=form.cleaned_data["operation_id"]
        )
        messages.success(request, _("Verification key promoted."))
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except VerificationKeyError as exc:
        messages.error(request, service_error_message(exc))
    return redirect("badges:verification-keys")


@operational_permission_required(
    "badges.manage_verificationkey", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def verification_key_retire(request, pk):
    """Retire an ACTIVE key without promoting a successor.

    A retired key keeps verifying credentials already issued under it while
    they remain within their own validity window -- that overlap is the point.
    """
    key = get_object_or_404(VerificationKey, pk=pk)
    form = VerificationKeyLifecycleForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        retire_verification_key(
            key=key,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            reason_code=form.cleaned_data.get("reason_code") or "",
            reason_text=form.cleaned_data.get("reason_text", ""),
        )
        messages.success(request, _("Verification key retired."))
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except VerificationKeyError as exc:
        messages.error(request, service_error_message(exc))
    return redirect("badges:verification-keys")


@operational_permission_required(
    "badges.manage_verificationkey", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def verification_key_revoke(request, pk):
    """Emergency revocation.

    Every credential bearing this key's `kid` fails online verification
    immediately, regardless of its own expiry. Phase 3 makes no claim about
    offline devices receiving this update.
    """
    key = get_object_or_404(VerificationKey, pk=pk)
    form = RevokeVerificationKeyForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("A reason is required to revoke a verification key."))
    try:
        revoke_verification_key(
            key=key,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            reason_code=form.cleaned_data["reason_code"],
            reason_text=form.cleaned_data.get("reason_text", ""),
        )
        messages.success(request, _("Verification key revoked."))
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except VerificationKeyError as exc:
        messages.error(request, service_error_message(exc))
    return redirect("badges:verification-keys")


@operational_permission_required(
    "badges.view_digitalentrypass", login_url="accounts:operational-sign-in"
)
@require_http_methods(["GET"])
def verification_key_set(request):
    """The published public verification key set.

    Public key material only. Consumed online in Phase 3; the signed
    offline key set that devices would consume is Phase 4 and is not
    produced here.
    """
    from apps.badges.services import published_verification_key_set

    return JsonResponse({"keys": published_verification_key_set()})


# ---------------------------------------------------------------------------
# Participant-facing
# ---------------------------------------------------------------------------


def _participant_person(request) -> Person:
    return get_object_or_404(Person, pk=participant_auth.get_participant_person_id(request))


def _participant_pass_context(request, credential) -> dict:
    """Build the safe participant view of one credential.

    A usable QR exists only for an ACTIVE credential. The template receives a
    URL for the QR image endpoint, never the token itself -- the token is
    reconstructed server-side inside that endpoint and never reaches a URL,
    a query string, or a log line.

    No internal reason, reviewer identity, assignment history, signing-key
    detail, or previous credential version is ever exposed.
    """
    series = credential.series
    # Fail closed on time, exactly as verification does. A row can legitimately
    # still read ACTIVE after its window closed, because the expiry sweep is
    # deliberately not load-bearing -- showing a QR then would hand the
    # participant a code every gate rejects. Nothing is written here: this is
    # a GET, and it must not mutate state.
    now = timezone.now()
    exposes_qr = pass_exposes_usable_qr(credential, now=now)
    status_message = PARTICIPANT_STATUS_MESSAGES.get(credential.status, "")
    may_become_active = credential.status in (
        DigitalEntryPassStatus.INACTIVE,
        DigitalEntryPassStatus.SUSPENDED,
    )
    if credential.status in NON_TERMINAL_STATUSES and not is_active_approved_context(
        credential.registration
    ):
        # Withdrawn or operationally cancelled (P8-06). The credential is not
        # revoked -- its history stays -- but the card must neither claim a
        # usable QR nor promise a later activation that the lifecycle now
        # refuses. The safe "no longer valid" guidance applies instead.
        status_message = PARTICIPANT_STATUS_MESSAGES[DigitalEntryPassStatus.REVOKED]
        may_become_active = False
    elif credential.status == DigitalEntryPassStatus.ACTIVE and not exposes_qr:
        if now < credential.valid_from:
            status_message = PARTICIPANT_NOT_YET_VALID_MESSAGE
        else:
            status_message = PARTICIPANT_STATUS_MESSAGES[DigitalEntryPassStatus.EXPIRED]
        may_become_active = False
    return {
        "credential": credential,
        "registration": credential.registration,
        "status_message": status_message,
        "has_usable_qr": exposes_qr,
        # A terminal credential will never become active, so the "once your
        # pass is active" hint would be a false promise on a revoked or
        # expired card.
        "may_become_active": may_become_active,
        "qr_image_url": (
            reverse("badges:participant-pass-qr", kwargs={"public_id": series.public_id})
            if exposes_qr
            else ""
        ),
        "public_id": series.public_id,
        "fallback_reference_display": references.format_fallback_reference(
            series.fallback_reference
        ),
        "valid_from": credential.valid_from,
        "valid_until": credential.valid_until,
        # The authorized conference days, read live (an attendance change is
        # reflected at once; the signed QR carries no attendance claim and the
        # server decides admission). None when not approved.
        "attendance": participant_attendance(credential.registration),
    }


@participant_auth.participant_required
@never_cache
@require_http_methods(["GET"])
def participant_passes(request):
    """The authenticated participant's own current passes."""
    person = _participant_person(request)
    credentials = participant_displayable_passes(person)
    response = render(
        request,
        "badges/participant_passes.html",
        {
            "passes": [_participant_pass_context(request, c) for c in credentials],
            "workspace_nav": "passes",
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    return response


@participant_auth.participant_required
@never_cache
@require_http_methods(["GET"])
def participant_pass_print(request, public_id):
    """Accessible HTML print view for one of the participant's own passes.

    Addressed by the series' random public identifier and additionally
    scoped to the authenticated participant, so guessing an identifier
    still cannot reach someone else's credential.
    """
    credential = _participant_credential_or_404(request, public_id)
    response = render(
        request,
        "badges/participant_pass_print.html",
        _participant_pass_context(request, credential),
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    return response


def _participant_credential_or_404(request, public_id):
    """Resolve one displayable credential owned by the signed-in participant.

    Ownership is enforced by filtering on the authenticated person, not by
    trusting the supplied identifier, so guessing a `public_id` reaches
    nothing.
    """
    person = _participant_person(request)
    for credential in participant_displayable_passes(person):
        if credential.series.public_id == public_id:
            return credential
    raise Http404


@participant_auth.participant_required
@never_cache
@require_http_methods(["GET"])
def participant_pass_qr(request, public_id):
    """Render the participant's own ACTIVE credential as a real QR image.

    The token is reconstructed from stored immutable material inside this
    request and encoded straight to PNG bytes in memory. It is never placed
    in the URL, never logged, and never written to disk. A non-ACTIVE
    credential yields 404 rather than an image -- there is no state in which
    a usable QR exists for a pass that is not active.
    """
    credential = _participant_credential_or_404(request, public_id)
    # Both conditions, and neither is redundant: ACTIVE status AND an open
    # validity window. An ACTIVE row past its window yields 404, not an image.
    if not pass_exposes_usable_qr(credential):
        raise Http404

    try:
        image = render_png(issue_pass_token(credential))
    except (PassStateError, PassConfigurationError, QrRenderError) as exc:
        # Never surface the underlying detail: it would describe credential
        # material or internal key state to the browser.
        raise Http404 from exc

    response = HttpResponse(image, content_type="image/png")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
#
# Every mutation below follows the exact same discipline as the credential
# lifecycle above: POST-only, CSRF-protected (Django's default), permission-
# checked through the shared scoped primitive, and carrying its own
# `operation_id`. No entry device, scanner, or gate-admission concept
# appears anywhere here.
# ---------------------------------------------------------------------------


def _scoped_event_or_404(request, pk, *, codename: str) -> EventEdition:
    """An `EventEdition` is not itself a scope-filterable row (it has no
    `event_edition_id` of its own); the check is the actor's SCOPED
    permission for exactly this event, returning 404 -- never 403 -- so an
    out-of-scope event is indistinguishable from a nonexistent one."""
    event = get_object_or_404(EventEdition, pk=pk)
    if not has_scoped_permission(request.user, f"badges.{codename}", event_edition_id=event.pk):
        raise Http404
    return event


def _scoped_location_or_404(request, pk, *, codename: str) -> StockLocation:
    return get_object_or_404(stock_locations_visible_to(request.user, codename=codename), pk=pk)


def _scoped_batch_or_404(request, pk, *, codename: str) -> PrintBatch:
    return get_object_or_404(print_batches_visible_to(request.user, codename=codename), pk=pk)


def _scoped_issuance_or_404(request, pk, *, codename: str) -> BadgeIssuance:
    return get_object_or_404(issuances_visible_to(request.user, codename=codename), pk=pk)


def _redirect_to_stock_dashboard(event_id) -> HttpResponse:
    return redirect("badges:stock-dashboard", event_pk=event_id)


def _redirect_to_batch(batch_id) -> HttpResponse:
    return redirect("badges:print-batch-detail", pk=batch_id)


def _redirect_to_badge_issuance(registration_id) -> HttpResponse:
    return redirect("badges:registration-badge-issuance", pk=registration_id)


@operational_permission_required(
    "badges.view_stocklocation", login_url="accounts:operational-sign-in"
)
@require_http_methods(["GET"])
def stock_dashboard(request, event_pk):
    """Production and reconciliation summary for one event edition.

    Carries no participant data: locations, batches, balances, ledger
    entries, transfers, and reconciliations are all badge-type/location
    aggregates, never a Registration Context or an identity value.
    """
    # Gated on the stock-ACCOUNTING permission, not on `view_stocklocation`
    # (Prompt 3 correction §8): handover staff hold the latter only so they
    # can choose an issuing location, and must not reach the ledger,
    # batches, transfers, adjustments, or reconciliation through it.
    event = _scoped_event_or_404(request, event_pk, codename="view_stockaccounting")
    scope = {"event_edition_id": event.pk}
    locations = list(StockLocation.objects.filter(event_edition=event).order_by("code"))
    badge_types = list(BadgeType.objects.filter(event_edition=event).order_by("code"))
    context = {
        "event": event,
        "locations": locations,
        "badge_types": badge_types,
        "batches": print_batches_visible_to(request.user)
        .filter(event_edition=event)
        .order_by("-created_at"),
        "balance_rows": _balance_matrix(event=event, locations=locations, badge_types=badge_types),
        "ledger_entries": ledger_entries_for(event_edition=event, limit=50),
        "transfers": transfers_for(event_edition=event, limit=50),
        "reconciliations": reconciliations_for(event_edition=event, limit=50),
        "allocations": active_allocations_for(event_edition=event),
        "can_manage_locations": has_scoped_permission(
            request.user, "badges.manage_stocklocation", **scope
        ),
        "can_manage_batches": has_scoped_permission(
            request.user, "badges.manage_printbatch", **scope
        ),
        "can_transfer": has_scoped_permission(request.user, "badges.transfer_badgestock", **scope),
        "can_allocate": has_scoped_permission(request.user, "badges.allocate_badgestock", **scope),
        "can_reconcile": has_scoped_permission(
            request.user, "badges.reconcile_badgestock", **scope
        ),
        "location_type_choices": StockLocationType.choices,
        "adjustment_reason_choices": adjustment_reason_choices(),
        "allocation_purpose_choices": allocation_purpose_choices(),
        "create_location_operation_id": new_operation_id(),
        "create_batch_operation_id": new_operation_id(),
        "transfer_operation_id": new_operation_id(),
        "adjust_operation_id": new_operation_id(),
        "reconcile_operation_id": new_operation_id(),
        "allocate_operation_id": new_operation_id(),
        "release_operation_id": new_operation_id(),
    }
    # Headline quantities for the summary tiles (Phase 3 Prompt 5): sums of
    # the same aggregates the tables show -- never participant data.
    rows = context["balance_rows"]
    reconciliations = list(context["reconciliations"])
    context["reconciliations"] = reconciliations
    context["totals"] = {
        "on_hand": sum(row["quantity"] for row in rows),
        "allocated": sum(row["reserved"] for row in rows),
        "available": sum(row["available"] for row in rows),
        "discrepancies": sum(1 for r in reconciliations if r.discrepancy),
    }
    return render(request, "badges/stock_dashboard.html", context)


def _balance_matrix(*, event, locations, badge_types) -> list[dict]:
    """One row per (location, badge type), with on-hand, reserved, and available.

    Built server-side as a flat, already-aligned list so the template needs
    no dict-by-variable-key lookup, in the same (location, badge type)
    order the template iterates. `available` is the figure an operator acts
    on (UI/UX §9.3); `reserved` explains the difference.
    """
    rows = {(row.location_id, row.badge_type_id): row for row in event_balances(event)}
    matrix = []
    for location in locations:
        for badge_type in badge_types:
            row = rows.get((location.pk, badge_type.pk))
            quantity = row.quantity if row is not None else 0
            reserved = row.reserved_quantity if row is not None else 0
            matrix.append(
                {
                    "location": location,
                    "badge_type": badge_type,
                    "quantity": quantity,
                    "reserved": reserved,
                    "available": quantity - reserved,
                }
            )
    return matrix


@operational_permission_required(
    "badges.manage_stocklocation", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def stock_location_create(request, event_pk):
    event = _scoped_event_or_404(request, event_pk, codename="manage_stocklocation")
    form = CreateStockLocationForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        create_stock_location(
            event_edition=event,
            code=form.cleaned_data["code"],
            name=form.cleaned_data["name"],
            name_fr=form.cleaned_data["name_fr"],
            name_ar=form.cleaned_data["name_ar"],
            location_type=form.cleaned_data["location_type"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
        )
    except DuplicateStockLocationError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    messages.success(request, _("Stock location created."))
    return _redirect_to_stock_dashboard(event.pk)


@operational_permission_required(
    "badges.manage_printbatch", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def print_batch_create(request, event_pk):
    event = _scoped_event_or_404(request, event_pk, codename="manage_printbatch")
    form = CreatePrintBatchForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    badge_type_id = request.POST.get("badge_type_id")
    badge_type = get_object_or_404(BadgeType, pk=badge_type_id, event_edition=event)
    destination = None
    if form.cleaned_data.get("destination_location_id"):
        destination = get_object_or_404(
            StockLocation, pk=form.cleaned_data["destination_location_id"], event_edition=event
        )
    try:
        batch = create_print_batch(
            event_edition=event,
            badge_type=badge_type,
            planned_quantity=form.cleaned_data["planned_quantity"],
            artwork_version=form.cleaned_data["artwork_version"],
            supplier_reference=form.cleaned_data["supplier_reference"],
            destination_location=destination,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    messages.success(request, _("Print batch created."))
    return _redirect_to_batch(batch.pk)


@operational_permission_required("badges.view_printbatch", login_url="accounts:operational-sign-in")
@require_http_methods(["GET"])
def print_batch_detail(request, pk):
    batch = _scoped_batch_or_404(request, pk, codename="view_printbatch")
    scope = {"event_edition_id": batch.event_edition_id}
    context = {
        "can_view_stock": has_scoped_permission(
            request.user, "badges.view_stockaccounting", **scope
        ),
        "batch": batch,
        "locations": StockLocation.objects.filter(event_edition=batch.event_edition).order_by(
            "code"
        ),
        "status_choices": PrintBatchStatus,
        "can_manage": has_scoped_permission(request.user, "badges.manage_printbatch", **scope),
        "can_receive": has_scoped_permission(request.user, "badges.receive_printbatch", **scope),
        "status_operation_id": new_operation_id(),
        "receive_operation_id": new_operation_id(),
    }
    return render(request, "badges/print_batch_detail.html", context)


@operational_permission_required(
    "badges.manage_printbatch", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def print_batch_status_change(request, pk):
    batch = _scoped_batch_or_404(request, pk, codename="manage_printbatch")
    form = ChangeBatchStatusForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        change_batch_status(
            batch=batch,
            target_status=form.cleaned_data["target_status"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            expected_lock_version=form.cleaned_data["expected_lock_version"],
        )
    except StockConcurrencyError:
        return _conflict_response(
            request, _("This print batch changed since you loaded it. Reload and try again.")
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_batch(batch.pk)
    messages.success(request, _("Print batch status updated."))
    return _redirect_to_batch(batch.pk)


@operational_permission_required(
    "badges.receive_printbatch", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def print_batch_receive(request, pk):
    batch = _scoped_batch_or_404(request, pk, codename="receive_printbatch")
    form = ReceivePrintBatchForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    destination = None
    if form.cleaned_data.get("destination_location_id"):
        destination = get_object_or_404(
            StockLocation,
            pk=form.cleaned_data["destination_location_id"],
            event_edition=batch.event_edition,
        )
    try:
        receive_print_batch(
            batch=batch,
            produced_quantity=form.cleaned_data["produced_quantity"],
            accepted_quantity=form.cleaned_data["accepted_quantity"],
            damaged_quantity=form.cleaned_data["damaged_quantity"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            expected_lock_version=form.cleaned_data["expected_lock_version"],
            destination_location=destination,
        )
    except StockConcurrencyError:
        return _conflict_response(
            request, _("This print batch changed since you loaded it. Reload and try again.")
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_batch(batch.pk)
    messages.success(request, _("Print batch received into stock."))
    return _redirect_to_batch(batch.pk)


@operational_permission_required(
    "badges.transfer_badgestock", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def stock_transfer_create(request, event_pk, source_pk):
    event = _scoped_event_or_404(request, event_pk, codename="transfer_badgestock")
    source = get_object_or_404(StockLocation, pk=source_pk, event_edition=event)
    form = TransferStockForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    destination = get_object_or_404(
        StockLocation, pk=form.cleaned_data["destination_location_id"], event_edition=event
    )
    badge_type = get_object_or_404(
        BadgeType, pk=form.cleaned_data["badge_type_id"], event_edition=event
    )
    try:
        transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=source,
            destination_location=destination,
            quantity=form.cleaned_data["quantity"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            note=form.cleaned_data["note"],
        )
    except InsufficientStockError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    messages.success(request, _("Stock transferred."))
    return _redirect_to_stock_dashboard(event.pk)


@operational_permission_required(
    "badges.manage_stocklocation", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def stock_adjustment_create(request, event_pk, location_pk):
    event = _scoped_event_or_404(request, event_pk, codename="manage_stocklocation")
    location = get_object_or_404(StockLocation, pk=location_pk, event_edition=event)
    form = RecordAdjustmentForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    badge_type = get_object_or_404(
        BadgeType, pk=form.cleaned_data["badge_type_id"], event_edition=event
    )
    try:
        record_adjustment(
            event_edition=event,
            badge_type=badge_type,
            location=location,
            quantity_delta=form.cleaned_data["quantity_delta"],
            reason_code=form.cleaned_data["reason_code"],
            reason_text=form.cleaned_data["reason_text"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
        )
    except InsufficientStockError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    messages.success(request, _("Stock adjustment recorded."))
    return _redirect_to_stock_dashboard(event.pk)


@operational_permission_required(
    "badges.allocate_badgestock", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def stock_allocation_create(request, event_pk, location_pk):
    """Reserve stock at a location: the "allocation" step of the
    authoritative Prompt 3 list. Moves nothing; reduces availability."""
    event = _scoped_event_or_404(request, event_pk, codename="allocate_badgestock")
    location = get_object_or_404(StockLocation, pk=location_pk, event_edition=event)
    form = AllocateStockForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    badge_type = get_object_or_404(
        BadgeType, pk=form.cleaned_data["badge_type_id"], event_edition=event
    )
    try:
        allocate_stock(
            event_edition=event,
            badge_type=badge_type,
            location=location,
            quantity=form.cleaned_data["quantity"],
            purpose_code=form.cleaned_data["purpose_code"],
            purpose_text=form.cleaned_data["purpose_text"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except (CrossEventScopeError, InsufficientStockError, StockServiceError) as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    messages.success(request, _("Stock allocated."))
    return _redirect_to_stock_dashboard(event.pk)


@operational_permission_required(
    "badges.allocate_badgestock", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def stock_allocation_release(request, event_pk, pk):
    """Return an allocation's unconsumed units to general availability."""
    event = _scoped_event_or_404(request, event_pk, codename="allocate_badgestock")
    allocation = get_object_or_404(BadgeStockAllocation, pk=pk, event_edition=event)
    form = ReleaseAllocationForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        release_allocation(
            allocation=allocation,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            expected_lock_version=form.cleaned_data["expected_lock_version"],
            reason_text=form.cleaned_data["reason_text"],
        )
    except StockConcurrencyError:
        return _conflict_response(
            request, _("This allocation changed since you loaded it. Reload and try again.")
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except (CrossEventScopeError, StockServiceError) as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    messages.success(request, _("Allocation released."))
    return _redirect_to_stock_dashboard(event.pk)


@operational_permission_required(
    "badges.reconcile_badgestock", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def stock_reconciliation_create(request, event_pk, location_pk):
    event = _scoped_event_or_404(request, event_pk, codename="reconcile_badgestock")
    location = get_object_or_404(StockLocation, pk=location_pk, event_edition=event)
    form = RecordReconciliationForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    badge_type = get_object_or_404(
        BadgeType, pk=form.cleaned_data["badge_type_id"], event_edition=event
    )
    try:
        record_reconciliation(
            event_edition=event,
            badge_type=badge_type,
            location=location,
            counted_quantity=form.cleaned_data["counted_quantity"],
            notes=form.cleaned_data["notes"],
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_stock_dashboard(event.pk)
    messages.success(request, _("Reconciliation recorded."))
    return _redirect_to_stock_dashboard(event.pk)


# ---------------------------------------------------------------------------
# Badge issuance -- generic physical badge handover to the exact
# Registration Context
# ---------------------------------------------------------------------------


@operational_permission_required(
    "badges.view_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["GET"])
def registration_badge_issuance(request, pk):
    registration = _scoped_registration_or_404(request, pk, codename="view_badgeissuance")
    scope = {
        "event_edition_id": registration.event_edition_id,
        "organization_id": registration.source_organization_id,
    }
    assignment = BadgeTypeAssignment.objects.filter(
        registration=registration, status=AssignmentStatus.CURRENT
    ).first()
    # Keyed on the REGISTRATION, so a Badge Type reassignment cannot make an
    # already-held physical badge look absent (Prompt 3 correction §4).
    current_issuance = (
        BadgeIssuance.objects.select_related("badge_type", "location")
        .filter(registration=registration, status=BadgeIssuanceStatus.ISSUED)
        .first()
    )
    context = {
        "registration": registration,
        "assignment": assignment,
        "current_issuance": current_issuance,
        "history": BadgeIssuance.objects.filter(registration=registration)
        .select_related("badge_type", "location")
        .order_by("-issued_at"),
        # Only ACTIVE locations in this registration's OWN event may be
        # offered as an issuing point (Prompt 3 correction §2/§8).
        "locations": StockLocation.objects.filter(
            event_edition=registration.event_edition, is_active=True
        ).order_by("code"),
        "issuance_allocations": (
            allocations_available_for_issuance(
                badge_type=assignment.badge_type,
                event_edition=registration.event_edition,
            )
            if assignment is not None
            else BadgeStockAllocation.objects.none()
        ),
        "can_issue": has_scoped_permission(request.user, "badges.issue_badgeissuance", **scope),
        "can_return": has_scoped_permission(request.user, "badges.return_badgeissuance", **scope),
        "issuance_status_choices": BadgeIssuanceStatus,
        "issue_operation_id": new_operation_id(),
        "replace_operation_id": new_operation_id(),
        "return_operation_id": new_operation_id(),
        "lost_operation_id": new_operation_id(),
        "void_operation_id": new_operation_id(),
        "replace_reason_choices": issuance_reason_choices(_ISSUANCE_REPLACE_REASON_CODES),
        "return_reason_choices": issuance_reason_choices(_ISSUANCE_RETURN_REASON_CODES),
    }
    context.update(_attendance_marking_context(registration, current_issuance))
    return render(request, "badges/registration_badge_issuance.html", context)


def _attendance_marking_context(registration, current_issuance) -> dict:
    """The marking the operator must apply to the physical badge: the words
    of the registration's CURRENT attendance days (the generic stock is per
    Badge Type and never implies the days)."""
    from apps.accreditation import attendance

    entitlement = attendance.current_entitlement(registration.pk)
    policy = attendance.policy_for(registration.event_edition_id)
    return {
        "attendance_entitlement": entitlement,
        "attendance_view": attendance.participant_attendance(registration),
        "attendance_enforced": bool(policy and policy.enforcement_active),
        "attendance_marking_pending": current_issuance is not None
        and (entitlement is None or current_issuance.attendance_marking != entitlement.category),
    }


@operational_permission_required(
    "badges.issue_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def badge_issue(request, pk):
    registration = _scoped_registration_or_404(request, pk, codename="issue_badgeissuance")
    assignment = get_object_or_404(
        BadgeTypeAssignment, registration=registration, status=AssignmentStatus.CURRENT
    )
    form = IssueBadgeForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    # Never by bare UUID (Prompt 3 correction §2): both objects are looked
    # up WITHIN this registration's event edition, and the issuing location
    # must additionally be active, so a forged POST naming another event's
    # Badge Type or a decommissioned location is a 404 before the service
    # is reached -- and the service re-checks the same boundary anyway.
    badge_type = get_object_or_404(
        BadgeType,
        pk=form.cleaned_data["badge_type_id"],
        event_edition_id=registration.event_edition_id,
    )
    location = get_object_or_404(
        StockLocation,
        pk=form.cleaned_data["location_id"],
        event_edition_id=registration.event_edition_id,
        is_active=True,
    )
    allocation = None
    if form.cleaned_data["allocation_id"] is not None:
        allocation = get_object_or_404(
            allocations_available_for_issuance(
                badge_type=badge_type,
                event_edition=registration.event_edition,
                location=location,
            ),
            pk=form.cleaned_data["allocation_id"],
        )
    try:
        issuance = issue_badge(
            badge_assignment=assignment,
            badge_type=badge_type,
            location=location,
            actor=request.user,
            operation_id=form.cleaned_data["operation_id"],
            optional_serial_number=form.cleaned_data["optional_serial_number"],
            allocation=allocation,
        )
    except CrossEventScopeError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_badge_issuance(registration.pk)
    except WrongBadgeTypeError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_badge_issuance(registration.pk)
    except InsufficientStockError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_badge_issuance(registration.pk)
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_badge_issuance(registration.pk)
    messages.success(request, _("Physical badge issued."))
    marking = form.cleaned_data.get("attendance_marking") or ""
    if marking:
        # The marking confirmed with the handover is recorded as its own
        # step: a mismatch (the days changed meanwhile) never undoes the
        # handover, it leaves the marking to record from the badge page.
        try:
            record_attendance_marking(
                issuance=issuance,
                attendance_marking=marking,
                actor=request.user,
                expected_lock_version=issuance.version,
            )
        except StockServiceError as exc:
            messages.warning(request, service_error_message(exc))
        else:
            messages.success(request, _("The attendance marking of the badge was recorded."))
    return _redirect_to_badge_issuance(registration.pk)


@operational_permission_required(
    "badges.issue_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def badge_issuance_attendance_marking(request, registration_pk, pk):
    """Record the attendance marking (sticker, overlay or print variant)
    applied to a handed-over badge. Scoped like every issuance command."""
    issuance = _scoped_issuance_or_404(request, pk, codename="issue_badgeissuance")
    if issuance.registration_id != registration_pk:
        raise Http404
    form = RecordAttendanceMarkingForm(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        record_attendance_marking(
            issuance=issuance,
            attendance_marking=form.cleaned_data["attendance_marking"],
            actor=request.user,
            expected_lock_version=form.cleaned_data["expected_lock_version"],
        )
    except StockServiceError as exc:
        messages.error(request, service_error_message(exc))
    else:
        messages.success(request, _("The attendance marking of the badge was recorded."))
    return _redirect_to_badge_issuance(issuance.registration_id)


def _issuance_command(
    request, pk, *, codename, form_class, runner, success_message, registration_id
):
    issuance = _scoped_issuance_or_404(request, pk, codename=codename)
    form = form_class(request.POST)
    if not form.is_valid():
        return _conflict_response(request, _("Invalid request. Reload the page and try again."))
    try:
        runner(issuance, form.cleaned_data)
    except StockConcurrencyError:
        return _conflict_response(
            request, _("This badge issuance changed since you loaded it. Reload and try again.")
        )
    except OperationConflictError:
        return _conflict_response(
            request, _("That operation identifier was already used for a different command.")
        )
    except (
        CrossEventScopeError,
        WrongBadgeTypeError,
        InsufficientStockError,
        StockServiceError,
    ) as exc:
        messages.error(request, service_error_message(exc))
        return _redirect_to_badge_issuance(registration_id)
    messages.success(request, success_message)
    return _redirect_to_badge_issuance(registration_id)


@operational_permission_required(
    "badges.issue_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def badge_issuance_replace(request, pk, registration_pk):
    return _issuance_command(
        request,
        pk,
        codename="issue_badgeissuance",
        form_class=ReplaceIssuanceForm,
        runner=lambda issuance, data: replace_issuance(
            issuance=issuance,
            # Scoped to the issuance's OWN event edition, never a bare UUID
            # (Prompt 3 correction §2/§3). The service independently
            # re-checks both the event boundary and that this Badge Type is
            # the Registration Context's current assignment.
            badge_type=get_object_or_404(
                BadgeType,
                pk=data["badge_type_id"],
                event_edition_id=issuance.location.event_edition_id,
            ),
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_code=data["reason_code"],
            reason_text=data["reason_text"],
            return_original=data["return_original"],
        ),
        success_message=_("Physical badge replaced."),
        registration_id=registration_pk,
    )


@operational_permission_required(
    "badges.return_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def badge_issuance_return(request, pk, registration_pk):
    return _issuance_command(
        request,
        pk,
        codename="return_badgeissuance",
        form_class=ReturnIssuanceForm,
        runner=lambda issuance, data: return_issuance(
            issuance=issuance,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_code=data["reason_code"],
            reason_text=data["reason_text"],
        ),
        success_message=_("Physical badge returned."),
        registration_id=registration_pk,
    )


@operational_permission_required(
    "badges.return_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def badge_issuance_mark_lost(request, pk, registration_pk):
    return _issuance_command(
        request,
        pk,
        codename="return_badgeissuance",
        form_class=MarkIssuanceLostForm,
        runner=lambda issuance, data: mark_issuance_lost(
            issuance=issuance,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_text=data["reason_text"],
        ),
        success_message=_("Physical badge reported lost."),
        registration_id=registration_pk,
    )


@operational_permission_required(
    "badges.return_badgeissuance", login_url="accounts:operational-sign-in"
)
@require_http_methods(["POST"])
def badge_issuance_void(request, pk, registration_pk):
    return _issuance_command(
        request,
        pk,
        codename="return_badgeissuance",
        form_class=VoidIssuanceForm,
        runner=lambda issuance, data: void_issuance(
            issuance=issuance,
            actor=request.user,
            operation_id=data["operation_id"],
            expected_lock_version=data["expected_lock_version"],
            reason_text=data["reason_text"],
        ),
        success_message=_("Badge issuance voided."),
        registration_id=registration_pk,
    )
