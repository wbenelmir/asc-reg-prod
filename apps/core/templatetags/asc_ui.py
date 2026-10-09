"""Design-system template tags (UI/UX Completion Gate, Checkpoint 1).

Presentation only: these tags map stored status codes to a tone and icon, and
render the operations section navigation. They never decide what a user may
do; see `apps.core.navigation`.
"""

from __future__ import annotations

from django import template

from apps.core.display_labels import code_label as _code_label
from apps.core.navigation import operations_navigation

register = template.Library()

#: status family -> stored code -> (tone, icon). Tones are the design-system
#: semantic tones (`asc-chip-<tone>`); every chip also shows its localized
#: label, so colour is never the only carrier of meaning (UI/UX §12.1).
_STATUS_TONES: dict[str, dict[str, tuple[str, str]]] = {
    "registration": {
        "DRAFT": ("neutral", "edit"),
        "SUBMITTED": ("info", "send"),
        "UNDER_REVIEW": ("info", "clock"),
        "ADDITIONAL_INFORMATION_REQUIRED": ("warning", "alert-triangle"),
        "APPROVED": ("success", "check-circle"),
        "NOT_APPROVED": ("danger", "x-circle"),
        "WITHDRAWN": ("neutral", "ban"),
    },
    "processing": {
        "PENDING_ASSIGNMENT": ("neutral", "clock"),
        "ASSIGNED": ("info", "user"),
        "VERIFICATION_PENDING": ("warning", "user-check"),
        "DUPLICATE_REVIEW": ("warning", "layers"),
        "REVIEW_IN_PROGRESS": ("info", "refresh"),
        "AWAITING_APPLICANT": ("warning", "clock"),
        "QUALIFICATION_COMPLETE": ("success", "check-circle"),
        "CLOSED": ("neutral", "lock"),
    },
    "review_case": {
        "QUEUED": ("neutral", "list"),
        "ASSIGNED": ("info", "user"),
        "IN_PROGRESS": ("info", "refresh"),
        "WAITING": ("warning", "clock"),
        "COMPLETED": ("success", "check-circle"),
        "CANCELLED": ("neutral", "ban"),
    },
    # Stage 3 families (apps.invitations, apps.reviews, apps.exports,
    # apps.communications, bulk accreditation rows).
    "campaign": {
        "DRAFT": ("neutral", "edit"),
        "ACTIVE": ("success", "check-circle"),
        "SUSPENDED": ("warning", "clock"),
        "EXPIRED": ("neutral", "clock"),
        "CLOSED": ("neutral", "lock"),
    },
    "invitation_link": {
        "ACTIVE": ("success", "check-circle"),
        "ROTATED": ("neutral", "refresh"),
        "REVOKED": ("danger", "ban"),
        "EXPIRED": ("neutral", "clock"),
    },
    "delegation_batch": {
        "UPLOADED": ("neutral", "inbox"),
        "VALIDATED": ("info", "check"),
        "APPLYING": ("info", "refresh"),
        "APPLIED": ("success", "check-circle"),
        "FAILED": ("danger", "x-circle"),
    },
    "delegation_row": {
        "PENDING": ("neutral", "clock"),
        "VALID": ("success", "check"),
        "INVALID": ("danger", "x-circle"),
        "APPLIED": ("success", "check-circle"),
    },
    "information_request": {
        "DRAFT": ("neutral", "edit"),
        "SENT": ("info", "send"),
        "RESPONSE_IN_PROGRESS": ("info", "refresh"),
        "SUBMITTED": ("warning", "inbox"),
        "CLOSED": ("success", "check-circle"),
        "CANCELLED": ("neutral", "ban"),
        "OVERDUE": ("warning", "alert-triangle"),
    },
    "export": {
        "PENDING": ("neutral", "clock"),
        "READY": ("success", "check-circle"),
        "EXPIRED": ("neutral", "lock"),
    },
    "message": {
        "QUEUED": ("neutral", "clock"),
        "SENDING": ("info", "refresh"),
        "SENT": ("info", "send"),
        "DELIVERED": ("success", "check-circle"),
        "DEFERRED": ("warning", "clock"),
        "FAILED": ("danger", "x-circle"),
        "BOUNCED": ("danger", "alert-octagon"),
        "UNDELIVERABLE": ("danger", "ban"),
        "SUPPRESSED": ("neutral", "ban"),
        "CANCELLED": ("neutral", "ban"),
    },
    # IDV-3: identity verification (apps.people.IdentityStatus). Verified by the
    # ministry and verified manually are distinct icons, as well as labels.
    "identity": {
        "PENDING": ("info", "clock"),
        "API_VERIFIED": ("success", "shield"),
        "MANUAL_REVIEW": ("warning", "user-check"),
        "MANUALLY_VERIFIED": ("success", "check-circle"),
        "RETURNED_FOR_CORRECTION": ("warning", "edit"),
        "REJECTED": ("danger", "x-circle"),
    },
    # Bulk accreditation rows: preview eligibility and execution outcome.
    "bulk_row": {
        "ELIGIBLE": ("success", "check-circle"),
        "NOT_ELIGIBLE": ("warning", "alert-triangle"),
        "SUCCEEDED": ("success", "check-circle"),
        "FAILED": ("danger", "x-circle"),
    },
    # Review decision workbook: the preview and its rows (apps.reviews.workbook).
    "decision_import": {
        "PREVIEWED": ("info", "clock"),
        "APPLIED": ("success", "check-circle"),
        "REFUSED": ("danger", "x-circle"),
        "EXPIRED": ("neutral", "lock"),
    },
    "decision_row": {
        "NO_CHANGE": ("neutral", "info"),
        "VALID": ("success", "check"),
        "INVALID": ("danger", "x-circle"),
    },
}

_DEFAULT_TONE = ("neutral", "info")


def status_tone(family: str, value: str) -> tuple[str, str]:
    """Return `(tone, icon)` for a stored status code; unknown codes are neutral."""
    return _STATUS_TONES.get(family, {}).get(value, _DEFAULT_TONE)


@register.inclusion_tag("components/status_chip.html")
def status_chip(family: str, value: str, label: str, extra: str = "") -> dict:
    """Render a status chip: `{% status_chip family stored_value localized_label %}`,
    for example the "registration" family with `registration.public_status`
    and `registration.get_public_status_display`."""
    tone, icon = status_tone(family, value)
    return {"tone": tone, "icon": icon, "label": label, "value": value, "extra": extra}


def _navigation_for(context) -> list:
    """Compute the section list once per request (the shell asks twice: the
    brand link and the navigation itself)."""
    request = context.get("request")
    if request is None:
        return []
    cached = getattr(request, "_asc_operations_navigation", None)
    if cached is not None:
        return cached
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    items = (
        operations_navigation(
            user,
            view_name=getattr(match, "view_name", "") or "",
            namespace=getattr(match, "namespace", "") or "",
        )
        if user is not None
        else []
    )
    request._asc_operations_navigation = items
    return items


@register.inclusion_tag("components/ops_nav.html", takes_context=True)
def operations_nav(context) -> dict:
    return {"items": _navigation_for(context)}


@register.simple_tag(takes_context=True)
def operations_home_url(context) -> str:
    """The first area this user may open; the intake list otherwise, so the
    brand link never points at a section the navigation hides."""
    from apps.core.navigation import operations_home_url as home_url

    request = context.get("request")
    return home_url(getattr(request, "user", None), items=_navigation_for(context))


def _merge_tokens(existing: str, *additions: str) -> str:
    tokens = existing.split()
    for addition in additions:
        for token in addition.split():
            if token and token not in tokens:
                tokens.append(token)
    return " ".join(tokens)


@register.filter
def asc_control(bound_field, options: str = ""):
    """Render a form control with the design-system class and its
    accessibility wiring, whatever form it comes from.

    Idempotent with forms that already set these attributes (for example
    `BootstrapFormMixin`): classes and `aria-describedby` ids are merged,
    never duplicated. `options` is a space-separated list; `ltr` isolates an
    inherently left-to-right value (email, code, number) with `dir="ltr"`
    (UI/UX §11.3). Never changes the value, name, validation or type.
    """
    from django import forms

    widget = bound_field.field.widget
    attrs = dict(widget.attrs)
    if isinstance(widget, (forms.RadioSelect, forms.CheckboxSelectMultiple)):
        return bound_field.as_widget()
    if isinstance(widget, forms.CheckboxInput):
        css = "form-check-input"
    elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
        css = "form-select"
    else:
        css = "form-control"
    attrs["class"] = _merge_tokens(attrs.get("class", ""), css)
    auto_id = bound_field.auto_id
    described_by = attrs.get("aria-describedby", "")
    if bound_field.help_text and auto_id:
        described_by = _merge_tokens(described_by, f"{auto_id}_help")
    if bound_field.errors and auto_id:
        described_by = _merge_tokens(described_by, f"{auto_id}_error")
        attrs["aria-invalid"] = "true"
    if described_by:
        attrs["aria-describedby"] = described_by
    if bound_field.field.required and not isinstance(widget, forms.CheckboxInput):
        attrs.setdefault("aria-required", "true")
    if "ltr" in options.split() or isinstance(widget, (forms.EmailInput, forms.URLInput)):
        attrs.setdefault("dir", "ltr")
    return bound_field.as_widget(attrs=attrs)


@register.filter
def ltr_isolate(value):
    """Wrap an inherently left-to-right value (email, code, URL) in
    `<bdi dir="ltr">` so it keeps its order inside translated RTL text,
    including inside `{% blocktranslate with … %}` without changing the
    msgid (UI checkpoint rule: isolate the value, never the component).
    The value itself is escaped."""
    from django.utils.html import format_html

    if value in (None, ""):
        return value
    return format_html('<bdi dir="ltr">{}</bdi>', value)


@register.filter
def code_label(value, family: str):
    """Translated label for a stored code that is not a model choice
    (`apps.core.display_labels`): `{{ export.purpose_code|code_label:"export_purpose" }}`."""
    return _code_label(family, value)


@register.filter
def get_item(mapping, key):
    """`{{ mapping|get_item:key }}` for a dict prepared by the view (for
    example registration id -> public reference); missing keys give ""."""
    if not mapping:
        return ""
    return mapping.get(key, mapping.get(str(key), ""))
