"""Event-edition read selectors."""

from __future__ import annotations

from apps.events.models import EventEdition, EventEditionStatus


class NoOpenEventEdition(Exception):
    """Raised when no EventEdition currently accepts registrations. Fails closed."""


class MultipleOpenEventEditions(NoOpenEventEdition):
    """Raised when more than one EventEdition is open for registration. Fails closed.

    A subclass of `NoOpenEventEdition` so every existing call site that
    catches the parent to show the generic "not currently open" screen
    still fails closed for this case too, without silently picking
    `.first()` (Prompt 4 final closure pass §10).
    """


def current_event_edition() -> EventEdition:
    """Return the EventEdition currently open for registration.

    V1 is single-event: exactly one EventEdition is expected to carry
    `REGISTRATION_OPEN` at a time. Fails closed (raises) rather than
    silently guessing -- whether none is open, or more than one is.
    """
    open_editions = EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN)
    count = open_editions.count()
    if count == 0:
        raise NoOpenEventEdition("No EventEdition is currently open for registration.")
    if count > 1:
        raise MultipleOpenEventEditions(
            "More than one EventEdition is currently open for registration."
        )
    return open_editions.get()


def events_with_channel_control(user):
    """Event editions whose registration channels `user` may manage.

    A superuser sees every edition. Otherwise an active, time-valid scoped
    membership whose group grants `events.manage_registration_channels`
    gives either every edition (event scope left empty) or its one edition.
    Organization-, venue- and gate-narrowed memberships never count: an
    event's channels are an event-wide setting.
    """
    from apps.accounts.policies import effective_scoped_memberships

    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return EventEdition.objects.none()
    if user.is_superuser:
        return EventEdition.objects.order_by("-starts_at")
    memberships = effective_scoped_memberships(user).filter(
        organization_id__isnull=True,
        group__permissions__content_type__app_label="events",
        group__permissions__codename="manage_registration_channels",
    )
    if memberships.filter(event_edition_id__isnull=True).exists():
        return EventEdition.objects.order_by("-starts_at")
    return EventEdition.objects.filter(
        pk__in=memberships.values_list("event_edition_id", flat=True)
    ).order_by("-starts_at")
