"""Transaction-scoped outbox contracts and Prompt 3 persistence adapter."""

from .contracts import OutboxMessage, OutboxPublisher
from .memory import InMemoryOutboxPublisher

__all__ = ["OutboxMessage", "OutboxPublisher", "InMemoryOutboxPublisher"]
