"""Participant accreditation settings management (UPD-07).

The participant roles, badge types, access profiles and access rules of an
event edition are the reference data the accreditation assignments (one by
one, or in bulk) choose from. They are PARTICIPANT accreditation settings,
distinct from staff account roles. Until now they could only be created in the
Django admin, which is not mounted in staging or production, so the bulk
assignment page had nothing to offer.

Rules:

* `code` is the stable identifier (2-64 characters: capital letters, digits,
  `_` and `-`), unique per event, and never changes after creation: badges,
  passes and offline packages may refer to it. Names (English, French,
  Arabic) and the other settings may change.
* Deactivating hides a setting from new assignments (the assignment services
  already refuse inactive references); existing assignments are untouched.
  Nothing is deleted.
* An access rule's profile, zone and gate must belong to the same event; a
  gate requires its zone (also a database constraint).
* Every change is audited (codes and ids only) and needs
  `accreditation.add_<model>` or `accreditation.change_<model>` for that
  event (default model permissions; no staff group holds them, so they
  belong to the superadmin unless granted explicitly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder

CODE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9_-]{1,63}")


@dataclass(frozen=True)
class SettingKind:
    slug: str
    model_name: str
    label: object
    plural: object

    @property
    def model(self):
        from django.apps import apps

        return apps.get_model("accreditation", self.model_name)

    @property
    def codename_suffix(self) -> str:
        return self.model_name.lower()


SETTING_KINDS: dict[str, SettingKind] = {
    kind.slug: kind
    for kind in (
        SettingKind("role", "ParticipantRole", _("Participant role"), _("Participant roles")),
        SettingKind("badge-type", "BadgeType", _("Badge type"), _("Badge types")),
        SettingKind("access-profile", "AccessProfile", _("Access profile"), _("Access profiles")),
        SettingKind("access-rule", "AccessRule", _("Access rule"), _("Access rules")),
    )
}


class SettingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def permission(kind: SettingKind, action: str) -> str:
    return f"accreditation.{action}_{kind.codename_suffix}"


def _audit(actor, action_code: str, obj, *, after: dict) -> None:
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type=type(obj).__name__,
            target_uuid=obj.pk,
            event_edition_id=obj.event_edition_id,
            result="SUCCESS",
            after_summary=after,
        )
    )


def clean_code(raw: str) -> str:
    code = (raw or "").strip().upper()
    if not CODE_PATTERN.fullmatch(code):
        raise SettingError("code_format")
    return code


def _validate_rule_links(event_edition_id, values: dict) -> None:
    profile = values.get("access_profile")
    zone = values.get("zone")
    gate = values.get("gate")
    if profile is not None and profile.event_edition_id != event_edition_id:
        raise SettingError("wrong_event")
    if zone is not None and zone.venue.event_edition_id != event_edition_id:
        raise SettingError("wrong_event")
    if gate is not None:
        if zone is None:
            raise SettingError("gate_requires_zone")
        if gate.venue.event_edition_id != event_edition_id or gate.venue_id != zone.venue_id:
            raise SettingError("wrong_event")
    valid_from, valid_until = values.get("valid_from"), values.get("valid_until")
    if valid_from and valid_until and valid_until <= valid_from:
        raise SettingError("validity_range")


def create_setting(kind: SettingKind, *, actor, event_edition, code: str, values: dict):
    code = clean_code(code)
    if kind.slug in ("access-profile", "access-rule"):
        valid_from, valid_until = values.get("valid_from"), values.get("valid_until")
        if valid_from and valid_until and valid_until <= valid_from:
            raise SettingError("validity_range")
    if kind.slug == "access-rule":
        _validate_rule_links(event_edition.pk, values)
    try:
        with transaction.atomic():
            obj = kind.model.objects.create(
                event_edition=event_edition, code=code, is_active=True, **values
            )
            _audit(
                actor,
                action_codes.ACCREDITATION_SETTING_CREATED,
                obj,
                after={"kind": kind.model_name, "code": code},
            )
    except IntegrityError:
        raise SettingError("code_taken") from None
    return obj


def update_setting(kind: SettingKind, obj, *, actor, values: dict):
    """Change the names and settings (never the code or the event)."""
    with transaction.atomic():
        locked = kind.model.objects.select_for_update().get(pk=obj.pk)
        if kind.slug in ("access-profile", "access-rule"):
            valid_from, valid_until = values.get("valid_from"), values.get("valid_until")
            if valid_from and valid_until and valid_until <= valid_from:
                raise SettingError("validity_range")
        if kind.slug == "access-rule":
            _validate_rule_links(locked.event_edition_id, values)
        changed = [field for field, value in values.items() if getattr(locked, field) != value]
        if changed:
            for field in changed:
                setattr(locked, field, values[field])
            locked.save()
            _audit(
                actor,
                action_codes.ACCREDITATION_SETTING_UPDATED,
                locked,
                after={"kind": kind.model_name, "code": locked.code, "changed": changed},
            )
    return locked


def set_setting_active(kind: SettingKind, obj, *, actor, active: bool):
    with transaction.atomic():
        locked = kind.model.objects.select_for_update().get(pk=obj.pk)
        if locked.is_active != active:
            locked.is_active = active
            locked.save(update_fields=["is_active", "updated_at"])
            _audit(
                actor,
                action_codes.ACCREDITATION_SETTING_STATUS_CHANGED,
                locked,
                after={"kind": kind.model_name, "code": locked.code, "active": active},
            )
    return locked


def current_assignment_counts(kind: SettingKind, ids) -> dict:
    """Current assignments per setting id (one grouped query)."""
    from django.db.models import Count

    from apps.accreditation.models import (
        AccessProfileAssignment,
        AccessRuleAssignment,
        AssignmentStatus,
        BadgeTypeAssignment,
        ParticipantRoleAssignment,
    )

    model, field = {
        "role": (ParticipantRoleAssignment, "role_id"),
        "badge-type": (BadgeTypeAssignment, "badge_type_id"),
        "access-profile": (AccessProfileAssignment, "access_profile_id"),
        "access-rule": (AccessRuleAssignment, "access_rule_id"),
    }[kind.slug]
    rows = (
        model.objects.filter(status=AssignmentStatus.CURRENT, **{f"{field}__in": list(ids)})
        .values(field)
        .annotate(total=Count("pk"))
    )
    return {str(row[field]): row["total"] for row in rows}
