"""In-memory `OutboxPublisher` -- Prompt 2 foundation unit tests only.

Not used by any domain code. Once `core.OutboxEvent` exists (Prompt 3), the
persistent implementation satisfies the same `OutboxPublisher` protocol;
this class exists solely so the protocol shape and the "enqueue inside the
transaction" calling convention are testable before there is a database.
"""

from __future__ import annotations

import threading

from .contracts import OutboxMessage


class InMemoryOutboxPublisher:
    """Thread-safe, process-local list of enqueued messages."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: list[OutboxMessage] = []

    def enqueue(self, message: OutboxMessage) -> None:
        with self._lock:
            self._messages.append(message)

    @property
    def messages(self) -> list[OutboxMessage]:
        with self._lock:
            return list(self._messages)

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
