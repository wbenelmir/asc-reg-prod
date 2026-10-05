"""Templates, messages, delivery attempts and suppression -- services (Phase 2 Prompt 5).

`send_registration_confirmation` (Phase 1) and the delegation-claim
delivery helpers (`apps.invitations.services`, Phase 2 Prompt 2) remain
their own established, already-approved pathways and are unchanged by this
module. `queue_communication` is the shared, generic pipeline for every
Prompt 5 communication purpose (information request, response
confirmation, decision/status, account access).
"""

from __future__ import annotations

from .messaging import (
    CommunicationIdempotencyConflictError,
    InvalidMessageStateTransitionError,
    is_terminal_status,
    queue_communication,
    transition_message_status,
)
from .rendering import render_message, resolve_template_version

__all__ = [
    "queue_communication",
    "InvalidMessageStateTransitionError",
    "is_terminal_status",
    "transition_message_status",
    "render_message",
    "resolve_template_version",
    "CommunicationIdempotencyConflictError",
]
