"""Scope-filtered operational communication visibility."""

from __future__ import annotations

from apps.accounts.selectors import scope_filtered_queryset
from apps.communications.models import CommunicationMessage


def communication_messages_visible_to(user):
    return scope_filtered_queryset(
        user,
        CommunicationMessage.objects,
        app_label="communications",
        codename="view_communicationmessage",
        event_field="event_edition_id",
        organization_field="registration__source_organization_id",
    )
