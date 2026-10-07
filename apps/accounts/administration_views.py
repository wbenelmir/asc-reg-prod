"""Staff account administration (`/ops/staff-accounts/`) and the public
credential setup page (`/accounts/setup/`).

Views coordinate HTTP only. Every page and every command requires
`accounts.manage_operational_accounts` through an effective scoped
membership (`operational_permission_required`), and every command is
re-authorized by `apps.accounts.administration` against the actor's own
administration scope -- hidden buttons are never the boundary.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.accounts import administration as admin
from apps.accounts import roles
from apps.accounts.forms.administration import (
    EVERY,
    CredentialLinkForm,
    CredentialSetupForm,
    RoleGrantForm,
    RoleRevokeForm,
    StaffAccountCreateForm,
    StaffAccountFilterForm,
    StaffAccountStatusForm,
    StaffAccountUpdateForm,
)
from apps.accounts.models import (
    OperationalUser,
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.accounts.policies import operational_permission_required
from apps.core.middleware.correlation import get_correlation_id

_LOGIN = "accounts:operational-sign-in"
_PERMISSION = admin.MANAGE_PERMISSION
PAGE_SIZE = 25

_ERROR_MESSAGES = {
    "NOT_PERMITTED": gettext_lazy("You are not an account administrator."),
    "SELF_CHANGE": gettext_lazy(
        "You cannot change your own account, status or roles. Ask another administrator."
    ),
    "SCOPE_NOT_DELEGATABLE": gettext_lazy(
        "This is outside your administration scope. Nothing was changed."
    ),
    "LAST_ADMINISTRATOR": gettext_lazy(
        "This would remove the last account administrator with full scope. Nothing was changed."
    ),
    "REASON_REQUIRED": gettext_lazy("Enter a short reason (at least 3 characters)."),
    "INVALID_STATE": gettext_lazy("This is not possible in the account's current status."),
    "INVALID_VALIDITY": gettext_lazy("The end of validity must follow its start."),
    "EXPIRY_REQUIRED": gettext_lazy(
        "A temporary external-security account and its roles need a future end of validity."
    ),
    "EMAIL_TAKEN": gettext_lazy("An account already uses this email address."),
    "EMAIL_INVALID": gettext_lazy("Enter a valid email address."),
    "NAME_REQUIRED": gettext_lazy("Enter a display name."),
    "INVALID_ACCOUNT_TYPE": gettext_lazy("Choose an account type from the list."),
    "INVALID_ROLE": gettext_lazy("Choose a role from the list."),
    "EVENT_REQUIRED": gettext_lazy("Choose an event edition, or every event edition."),
    "INVALID_SCOPE": gettext_lazy(
        "This role cannot have this scope. Check the organization and the checkpoint."
    ),
    "NOT_FOUND": gettext_lazy("This role is no longer on the account. Reload the page."),
    "INVALID_PURPOSE": gettext_lazy("Invalid request. Reload the page and try again."),
    "DELIVERY_FAILED": gettext_lazy(
        "The email with the link could not be sent, so that link was cancelled. "
        "Try again in a few minutes."
    ),
}
_FALLBACK = gettext_lazy("This change could not be applied. Reload the page and try again.")

_STATUS_TONES = {
    OperationalUserStatus.ACTIVE: "success",
    OperationalUserStatus.INVITED: "info",
    OperationalUserStatus.SUSPENDED: "warning",
    OperationalUserStatus.EXPIRED: "warning",
    OperationalUserStatus.DISABLED: "danger",
}


def error_message(exc: admin.AccountAdministrationError) -> str:
    return str(_ERROR_MESSAGES.get(exc.code, _FALLBACK))


def _status_label(status: str) -> str:
    return {
        OperationalUserStatus.INVITED: _("Invited"),
        OperationalUserStatus.ACTIVE: _("Active"),
        OperationalUserStatus.SUSPENDED: _("Suspended"),
        OperationalUserStatus.EXPIRED: _("Expired"),
        OperationalUserStatus.DISABLED: _("Disabled"),
    }.get(status, status)


def _access_label(code: str) -> str:
    return {
        admin.ACCESS_NONE: _("Cannot sign in: the account is not active or outside its validity."),
        admin.ACCESS_NOT_SET_UP: _("Active, but no password set yet: send a setup link."),
        admin.ACCESS_NO_ROLE: _("Active, but no current role: the account opens no area."),
        admin.ACCESS_OK: _("Can sign in and use its current roles."),
    }[code]


def _membership_state_label(state: str) -> str:
    return {
        admin.MEMBERSHIP_EFFECTIVE: _("In effect"),
        admin.MEMBERSHIP_SCHEDULED: _("Starts later"),
        admin.MEMBERSHIP_ENDED: _("Ended"),
        admin.MEMBERSHIP_SUSPENDED: _("Suspended"),
    }[state]


def _correlation_id() -> str:
    return get_correlation_id() or ""


def _refuse(request, actor, target_uuid, exc) -> None:
    admin.record_refusal(
        actor=actor, target_uuid=target_uuid, code=exc.code, correlation_id=_correlation_id()
    )
    messages.error(request, error_message(exc))


# ---------------------------------------------------------------------------
# List and creation
# ---------------------------------------------------------------------------


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@never_cache
def staff_account_list(request):
    """Only the accounts the administrator may read (R01): the search, the
    filters, the count and the pages never reach outside that set, and the
    roles shown are only the ones inside the administrator's scope."""
    form = StaffAccountFilterForm(request.GET or None)
    scope = admin.administration_scope(request.user)
    accounts = admin.visible_accounts(request.user, scope).order_by("email_normalized")
    now = timezone.now()
    if form.is_valid():
        query = form.cleaned_data["q"].strip()
        if len(query) >= 2:
            accounts = accounts.filter(
                Q(email_normalized__icontains=query) | Q(display_name__icontains=query)
            )
        if form.cleaned_data["status"]:
            accounts = accounts.filter(status=form.cleaned_data["status"])
        role = roles.ROLES_BY_KEY.get(form.cleaned_data["role"] or "")
        if role is not None:
            accounts = accounts.filter(
                Exists(
                    admin.visible_memberships(
                        scope,
                        ScopedGroupMembership.objects.filter(
                            user=OuterRef("pk"), group__name=role.group_name, status="ACTIVE"
                        ).filter(Q(active_until__isnull=True) | Q(active_until__gt=now)),
                    )
                )
            )
    accounts = accounts.prefetch_related(
        Prefetch(
            "scoped_memberships",
            queryset=ScopedGroupMembership.objects.select_related("group"),
        )
    )
    page = Paginator(accounts, PAGE_SIZE).get_page(request.GET.get("page"))
    for account in page.object_list:
        memberships = list(account.scoped_memberships.all())
        shown = [m for m in memberships if scope.covers(m.event_edition_id, m.organization_id)]
        account.status_label = _status_label(account.status)
        account.status_tone = _STATUS_TONES.get(account.status, "outline")
        account.access_code = admin.effective_access(account, memberships, now=now)
        account.access_label = _access_label(account.access_code)
        account.current_roles = sorted(
            {
                str(getattr(roles.role_for_group_name(m.group.name), "label", m.group.name))
                for m in shown
                if admin.membership_state(m, now=now) == admin.MEMBERSHIP_EFFECTIVE
            }
        )
        account.has_hidden_roles = any(
            admin.membership_state(m, now=now) == admin.MEMBERSHIP_EFFECTIVE
            for m in memberships
            if m not in shown
        )
    querystring = request.GET.copy()
    querystring.pop("page", None)
    return render(
        request,
        "accounts/staff_account_list.html",
        {"page": page, "form": form, "querystring": querystring.urlencode()},
    )


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["GET", "POST"])
def staff_account_create(request):
    form = StaffAccountCreateForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            user = admin.create_account(
                actor=request.user,
                email=data["email"],
                display_name=data["display_name"],
                account_type=data["account_type"],
                active_from=data["active_from"],
                active_until=data["active_until"],
                correlation_id=_correlation_id(),
            )
        except admin.AccountAdministrationError as exc:
            form.add_error(None, error_message(exc))
            status = 400
        else:
            messages.success(
                request,
                _(
                    "The account was created as Invited. Next: send a setup link, activate it "
                    "and grant a role -- three separate steps."
                ),
            )
            return redirect("staff_accounts:detail", pk=user.pk)
    elif request.method == "POST":
        status = 400
    return render(request, "accounts/staff_account_create.html", {"form": form}, status=status)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def _delegatable_events(scope):
    from apps.events.models import EventEdition

    events = EventEdition.objects.order_by("-starts_at")
    if scope.unrestricted or any(event is None for event, _org in scope.pairs):
        return list(events)
    return list(events.filter(pk__in={event for event, _org in scope.pairs}))


def _grant_form(scope, data=None):
    from apps.events.models import Gate
    from apps.organizations.models import Organization

    events = _delegatable_events(scope)
    return RoleGrantForm(
        data,
        events=events,
        organizations=Organization.objects.filter(status="ACTIVE").order_by("official_name")[:500],
        gates=Gate.objects.select_related("venue")
        .filter(venue__event_edition__in=events, is_active=True)
        .order_by("venue__code", "code"),
        auto_id="id_grant_%s",
    )


def _account_or_404(request, pk) -> OperationalUser:
    """An account the administrator may read, else 404 -- the same answer
    as for an unknown id, so a guessed URL does not confirm that an account
    outside the administrator's scope exists."""
    return get_object_or_404(admin.visible_accounts(request.user), pk=pk)


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@never_cache
def staff_account_detail(request, pk):
    account = _account_or_404(request, pk)
    scope = admin.administration_scope(request.user)
    now = timezone.now()
    all_memberships = list(ScopedGroupMembership.objects.filter(user=account))
    memberships = list(
        admin.visible_memberships(scope, ScopedGroupMembership.objects.filter(user=account))
        .select_related("group", "event_edition", "organization", "venue", "gate")
        .order_by("-created_at")
    )
    shown = {membership.pk for membership in memberships}
    hidden_roles = any(membership.pk not in shown for membership in all_memberships)
    for membership in memberships:
        role = roles.role_for_group_name(membership.group.name)
        membership.role_label = str(role.label) if role else membership.group.name
        membership.state = admin.membership_state(membership, now=now)
        membership.state_label = _membership_state_label(membership.state)
        membership.revocable = (
            membership.state in (admin.MEMBERSHIP_EFFECTIVE, admin.MEMBERSHIP_SCHEDULED)
            and account.pk != request.user.pk
            and scope.covers(membership.event_edition_id, membership.organization_id)
        )
    can_administer = admin.can_administer_account(request.user, account, scope)
    access_code = admin.effective_access(account, all_memberships, now=now)
    context = {
        "account": account,
        "status_label": _status_label(account.status),
        "status_tone": _STATUS_TONES.get(account.status, "outline"),
        "access_code": access_code,
        "access_label": _access_label(access_code),
        "has_password": account.has_usable_password(),
        "memberships": memberships,
        "hidden_roles": hidden_roles,
        "is_self": account.pk == request.user.pk,
        "can_administer": can_administer,
        "can_grant": account.pk != request.user.pk
        and account.status != OperationalUserStatus.DISABLED
        and not (account.is_superuser and not request.user.is_superuser),
        "is_external_security": account.account_type
        == OperationalUserAccountType.EXTERNAL_SECURITY,
        # Setup-link metadata only for an administrator who may send links.
        "open_link": account.credential_setup_tokens.filter(
            used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now
        )
        .order_by("-created_at")
        .first()
        if can_administer
        else None,
        "update_form": StaffAccountUpdateForm(
            initial={
                "display_name": account.display_name,
                "active_from": account.active_from,
                "active_until": account.active_until,
            },
            auto_id="id_update_%s",
        ),
        "grant_form": _grant_form(scope),
        "status_commands": _status_commands(account),
    }
    return render(request, "accounts/staff_account_detail.html", context)


def _status_commands(account) -> list[dict]:
    commands = []
    for command, (_new_status, allowed_from) in admin._STATUS_COMMANDS.items():
        if account.status in allowed_from:
            commands.append(
                {
                    "command": command,
                    "label": {
                        admin.ACTIVATE: _("Activate"),
                        admin.SUSPEND: _("Suspend"),
                        admin.DISABLE: _("Disable"),
                    }[command],
                    "danger": command != admin.ACTIVATE,
                    "form": StaffAccountStatusForm(
                        initial={"command": command}, auto_id=f"id_{command}_%s"
                    ),
                }
            )
    return commands


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _back(account) -> object:
    return redirect("staff_accounts:detail", pk=account.pk)


def _first_error(form) -> str:
    for errors in form.errors.values():
        for error in errors:
            return str(error)
    return str(_FALLBACK)


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["POST"])
def staff_account_update(request, pk):
    account = _account_or_404(request, pk)
    form = StaffAccountUpdateForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return _back(account)
    data = form.cleaned_data
    try:
        admin.update_account(
            actor=request.user,
            target=account,
            display_name=data["display_name"],
            active_from=data["active_from"],
            active_until=data["active_until"],
            reason=data["reason"],
            correlation_id=_correlation_id(),
        )
    except admin.AccountAdministrationError as exc:
        _refuse(request, request.user, account.pk, exc)
    else:
        messages.success(request, _("The account was updated."))
    return _back(account)


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["POST"])
def staff_account_status(request, pk):
    account = _account_or_404(request, pk)
    form = StaffAccountStatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return _back(account)
    try:
        updated = admin.change_status(
            actor=request.user,
            target=account,
            command=form.cleaned_data["command"],
            reason=form.cleaned_data["reason"],
            correlation_id=_correlation_id(),
        )
    except admin.AccountAdministrationError as exc:
        _refuse(request, request.user, account.pk, exc)
    else:
        messages.success(
            request,
            _("The account status is now: %(status)s.") % {"status": _status_label(updated.status)},
        )
    return _back(account)


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["POST"])
def staff_account_credential_link(request, pk):
    account = _account_or_404(request, pk)
    form = CredentialLinkForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return _back(account)
    try:
        admin.issue_credential_setup(
            actor=request.user,
            target=account,
            purpose=form.cleaned_data["purpose"],
            correlation_id=_correlation_id(),
        )
    except admin.AccountAdministrationError as exc:
        if exc.code == "DELIVERY_FAILED":  # already audited as a delivery failure
            messages.error(request, error_message(exc))
        else:
            _refuse(request, request.user, account.pk, exc)
    else:
        messages.success(
            request,
            _(
                "A single-use link was sent to the account's email address. Any earlier link "
                "no longer works."
            ),
        )
    return _back(account)


def _choice_object(model, raw, **filters):
    if not raw or raw == EVERY:
        return None
    try:
        key = uuid.UUID(str(raw))
    except ValueError:
        raise Http404 from None
    return get_object_or_404(model, pk=key, **filters)


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["POST"])
def staff_account_grant(request, pk):
    from apps.events.models import EventEdition, Gate
    from apps.organizations.models import Organization

    account = _account_or_404(request, pk)
    scope = admin.administration_scope(request.user)
    form = _grant_form(scope, request.POST)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return _back(account)
    data = form.cleaned_data
    event = _choice_object(EventEdition, data["event"])
    gate = _choice_object(Gate, data["gate"])
    try:
        admin.grant_role(
            actor=request.user,
            target=account,
            role_key=data["role"],
            event=event,
            all_events=data["event"] == EVERY,
            organization=_choice_object(Organization, data["organization"]),
            venue=gate.venue if gate is not None else None,
            gate=gate,
            active_from=data["active_from"],
            active_until=data["active_until"],
            reason=data["reason"],
            correlation_id=_correlation_id(),
        )
    except admin.AccountAdministrationError as exc:
        _refuse(request, request.user, account.pk, exc)
    else:
        messages.success(request, _("The role was granted. It applies on the next request."))
    return _back(account)


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["POST"])
def staff_account_revoke(request, pk):
    account = _account_or_404(request, pk)
    form = RoleRevokeForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return _back(account)
    try:
        admin.revoke_role(
            actor=request.user,
            target=account,
            membership_id=form.cleaned_data["membership_id"],
            reason=form.cleaned_data["reason"],
            correlation_id=_correlation_id(),
        )
    except admin.AccountAdministrationError as exc:
        _refuse(request, request.user, account.pk, exc)
    else:
        messages.success(request, _("The role was ended. It no longer applies."))
    return _back(account)


# ---------------------------------------------------------------------------
# Public credential setup (single-use link)
# ---------------------------------------------------------------------------


def _no_store(response):
    """Pages of the setup flow are never cached. They keep the site's
    `same-origin` referrer policy: `no-referrer` would make browsers send
    `Origin: null` with their forms (the password form, the language switch),
    which CSRF origin checking refuses; their URL never holds the secret."""
    response["Cache-Control"] = "no-store, private"
    return response


@never_cache
@require_http_methods(["GET"])
def credential_setup_start(request, token):
    """The only URL that carries the secret renders nothing. A valid link
    puts only its token id in the session; any other link clears it. Either
    way the browser continues at once on `/accounts/setup/` (the password
    form, or the "link not valid" page), so no page -- and no referrer -- is
    ever produced from the secret-bearing URL."""
    found = admin.find_open_token(token)
    if found is None:
        request.session.pop(admin.SETUP_SESSION_KEY, None)
    else:
        request.session[admin.SETUP_SESSION_KEY] = str(found.pk)
    response = _no_store(redirect("accounts:credential-setup"))
    response["Referrer-Policy"] = "no-referrer"
    return response


@sensitive_post_parameters("new_password1", "new_password2")
@never_cache
@require_http_methods(["GET", "POST"])
def credential_setup(request):
    token = admin.open_token_by_id(request.session.get(admin.SETUP_SESSION_KEY))
    if token is None:
        request.session.pop(admin.SETUP_SESSION_KEY, None)
        return _no_store(render(request, "accounts/credential_setup_invalid.html", status=404))
    form = CredentialSetupForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            user = admin.complete_credential_setup(
                token_id=token.pk,
                password=form.cleaned_data["new_password1"],
                correlation_id=_correlation_id(),
            )
        except admin.AccountAdministrationError as exc:
            if exc.code == "PASSWORD_INVALID":
                for message in getattr(exc, "messages", []):
                    form.add_error("new_password1", message)
                status = 400
            else:
                request.session.pop(admin.SETUP_SESSION_KEY, None)
                return _no_store(
                    render(request, "accounts/credential_setup_invalid.html", status=404)
                )
        else:
            request.session.pop(admin.SETUP_SESSION_KEY, None)
            if user.is_active:
                messages.success(request, _("Your password is set. You can now sign in."))
            else:
                messages.success(
                    request,
                    _(
                        "Your password is set. You can sign in once an administrator has "
                        "activated your account."
                    ),
                )
            return redirect("accounts:operational-sign-in")
    elif request.method == "POST":
        status = 400
    return _no_store(
        render(
            request,
            "accounts/credential_setup.html",
            {"form": form, "setup_email": token.user.email_normalized, "purpose": token.purpose},
            status=status,
        )
    )
