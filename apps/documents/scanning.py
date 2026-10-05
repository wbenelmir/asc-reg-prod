"""Malware-scanning adapters (TRD `SEC-006`).

`ClamdScanner` is the real adapter: it streams the upload to a ClamAV `clamd`
daemon with the documented `INSTREAM` command, over a Unix socket or a private
TCP connection, with bounded timeouts and a bounded stream size. The two stub
scanners exist for local development and tests only; `get_scanner()` refuses
them unless the settings module explicitly allows local stubs, and
`config/settings/validation.py` requires `ClamdScanner` in staging and
production.

The result contract is fail-closed:

* only the exact clamd answer `stream: OK` is a clean result;
* `stream: <signature> FOUND` is a positive result (`clean=False`);
* everything else -- no connection, a timeout, a disconnection, a truncated,
  oversized or malformed answer, a clamd `ERROR` answer, or a stream above the
  configured size limit -- raises `ScannerUnavailable` with a stable reason
  code. Callers treat that as "not scanned", so the file is never stored as
  clean and never becomes accessible.

Nothing here logs or returns document bytes, a filename, the signature name or
the raw clamd answer; log records carry the reason code only.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import BinaryIO, Protocol

logger = logging.getLogger("asc2026.documents.scanning")

CLAMD_SCANNER_BACKEND = "apps.documents.scanning.ClamdScanner"

#: Bytes sent per INSTREAM chunk. clamd accepts any chunk up to StreamMaxLength.
CLAMD_CHUNK_BYTES = 64 * 1024
#: Upper bound on one clamd answer; real answers are a few dozen bytes.
CLAMD_MAX_RESPONSE_BYTES = 4096
#: The INSTREAM length prefix is an unsigned 32-bit integer.
CLAMD_PROTOCOL_MAX_CHUNK = 2**32 - 1

_FOUND_PATTERN = re.compile(r"^stream: (?P<signature>[\x21-\x7e][\x20-\x7e]{0,254}) FOUND$")


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    reason_code: str | None = None


class ScannerUnavailable(Exception):
    """The scanner gave no verdict. `reason_code` is stable and non-sensitive."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ScannerAdapter(Protocol):
    def scan(self, content: BinaryIO) -> ScanResult: ...


class DeterministicStubScanner:
    """Local/test only. Always reports clean; never contacts a real service."""

    local_test_only = True

    def scan(self, content: BinaryIO) -> ScanResult:
        return ScanResult(clean=True)


class RejectingStubScanner:
    """Local/test only. Always reports infected -- for scanner-failure tests."""

    local_test_only = True

    def scan(self, content: BinaryIO) -> ScanResult:
        return ScanResult(clean=False, reason_code="STUB_REJECTED")


class UnavailableStubScanner:
    """Local/test only. Always fails to give a verdict -- for outage tests."""

    local_test_only = True

    def scan(self, content: BinaryIO) -> ScanResult:
        raise ScannerUnavailable("STUB_UNAVAILABLE")


class ClamdScanner:
    """ClamAV `clamd` adapter using the `zINSTREAM` command.

    Configuration (settings, from the environment): `CLAMD_SOCKET_PATH` for a
    Unix socket, or `CLAMD_HOST` and `CLAMD_PORT` for a private TCP endpoint
    (exactly one of the two); `CLAMD_CONNECT_TIMEOUT_SECONDS`;
    `CLAMD_SCAN_TIMEOUT_SECONDS`, a deadline for the whole exchange after the
    connection is open; and `CLAMD_MAX_STREAM_BYTES`, which must not exceed the
    daemon's own `StreamMaxLength`.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        connect_timeout: float | None = None,
        scan_timeout: float | None = None,
        max_stream_bytes: int | None = None,
        chunk_bytes: int = CLAMD_CHUNK_BYTES,
    ) -> None:
        from django.conf import settings

        self.socket_path = (
            socket_path if socket_path is not None else getattr(settings, "CLAMD_SOCKET_PATH", "")
        ) or ""
        self.host = (host if host is not None else getattr(settings, "CLAMD_HOST", "")) or ""
        self.port = int(port if port is not None else getattr(settings, "CLAMD_PORT", 3310))
        self.connect_timeout = float(
            connect_timeout
            if connect_timeout is not None
            else getattr(settings, "CLAMD_CONNECT_TIMEOUT_SECONDS", 5)
        )
        self.scan_timeout = float(
            scan_timeout
            if scan_timeout is not None
            else getattr(settings, "CLAMD_SCAN_TIMEOUT_SECONDS", 60)
        )
        self.max_stream_bytes = int(
            max_stream_bytes
            if max_stream_bytes is not None
            else getattr(settings, "CLAMD_MAX_STREAM_BYTES", 25 * 1024 * 1024)
        )
        if not 1 <= chunk_bytes <= CLAMD_PROTOCOL_MAX_CHUNK:
            raise ValueError("chunk_bytes is out of range")
        self.chunk_bytes = chunk_bytes

    # -- public operations ----------------------------------------------------

    def scan(self, content: BinaryIO) -> ScanResult:
        try:
            return self._scan(content)
        except ScannerUnavailable as exc:
            logger.warning("malware scan unavailable", extra={"reason_code": exc.reason_code})
            raise

    def ping(self) -> bool:
        """`zPING`: True only for the exact answer `PONG` (operator checks)."""
        with self._connect() as sock:
            deadline = time.monotonic() + self.scan_timeout
            self._send(sock, b"zPING\0", deadline)
            return self._read_answer(sock, deadline) == "PONG"

    def version(self) -> str:
        """`zVERSION`: the daemon's version line (operator checks)."""
        with self._connect() as sock:
            deadline = time.monotonic() + self.scan_timeout
            self._send(sock, b"zVERSION\0", deadline)
            return self._read_answer(sock, deadline)

    # -- protocol -------------------------------------------------------------

    def _scan(self, content: BinaryIO) -> ScanResult:
        size = _remaining_size(content)
        if size is not None and size > self.max_stream_bytes:
            raise ScannerUnavailable("SIZE_LIMIT")
        with self._connect() as sock:
            deadline = time.monotonic() + self.scan_timeout
            sent = 0
            try:
                self._send(sock, b"zINSTREAM\0", deadline)
                while True:
                    chunk = content.read(self.chunk_bytes)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if sent > self.max_stream_bytes:
                        raise ScannerUnavailable("SIZE_LIMIT")
                    self._send(sock, struct.pack("!I", len(chunk)) + chunk, deadline)
                self._send(sock, struct.pack("!I", 0), deadline)
            except ScannerUnavailable as exc:
                if exc.reason_code != "DISCONNECTED":
                    raise
                # clamd closes the stream early when its own limit is hit; it
                # may still have written its answer. Read it if it is there.
                try:
                    answer = self._read_answer(sock, deadline)
                except ScannerUnavailable:
                    raise exc from None
                return self._verdict(answer)
            return self._verdict(self._read_answer(sock, deadline))

    def _verdict(self, answer: str) -> ScanResult:
        if answer == "stream: OK":
            return ScanResult(clean=True)
        if _FOUND_PATTERN.match(answer):
            return ScanResult(clean=False, reason_code="INFECTED")
        if answer.endswith(" ERROR"):
            if "size limit exceeded" in answer.lower():
                raise ScannerUnavailable("SIZE_LIMIT")
            raise ScannerUnavailable("SCANNER_ERROR")
        raise ScannerUnavailable("MALFORMED_RESPONSE")

    def _connect(self) -> socket.socket:
        if bool(self.socket_path) == bool(self.host):
            raise ScannerUnavailable("NOT_CONFIGURED")
        try:
            if self.socket_path:
                family = getattr(socket, "AF_UNIX", None)
                if family is None:
                    raise ScannerUnavailable("NOT_CONFIGURED")
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.settimeout(self.connect_timeout)
                try:
                    sock.connect(self.socket_path)
                except BaseException:
                    sock.close()
                    raise
                return sock
            return socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except ScannerUnavailable:
            raise
        except TimeoutError:
            raise ScannerUnavailable("CONNECT_TIMEOUT") from None
        except OSError:
            raise ScannerUnavailable("CONNECTION_FAILED") from None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScannerUnavailable("TIMEOUT")
        return remaining

    def _send(self, sock: socket.socket, data: bytes, deadline: float) -> None:
        try:
            sock.settimeout(self._remaining(deadline))
            sock.sendall(data)
        except ScannerUnavailable:
            raise
        except TimeoutError:
            raise ScannerUnavailable("TIMEOUT") from None
        except OSError:
            raise ScannerUnavailable("DISCONNECTED") from None

    def _read_answer(self, sock: socket.socket, deadline: float) -> str:
        """Read one NUL-terminated answer, tolerating partial reads."""
        buffer = bytearray()
        while b"\0" not in buffer:
            try:
                sock.settimeout(self._remaining(deadline))
                piece = sock.recv(1024)
            except ScannerUnavailable:
                raise
            except TimeoutError:
                raise ScannerUnavailable("TIMEOUT") from None
            except OSError:
                raise ScannerUnavailable("DISCONNECTED") from None
            if not piece:
                raise ScannerUnavailable("TRUNCATED_RESPONSE")
            buffer.extend(piece)
            if len(buffer) > CLAMD_MAX_RESPONSE_BYTES:
                raise ScannerUnavailable("MALFORMED_RESPONSE")
        raw, _, trailing = bytes(buffer).partition(b"\0")
        if trailing:
            raise ScannerUnavailable("MALFORMED_RESPONSE")
        try:
            answer = raw.decode("ascii")
        except UnicodeDecodeError:
            raise ScannerUnavailable("MALFORMED_RESPONSE") from None
        if any(ord(char) < 0x20 for char in answer):
            raise ScannerUnavailable("MALFORMED_RESPONSE")
        return answer.strip(" ")


def _remaining_size(content: BinaryIO) -> int | None:
    try:
        position = content.tell()
        end = content.seek(0, 2)
        content.seek(position)
    except (AttributeError, OSError, ValueError):  # fmt: skip
        return None
    return end - position


class LocalStubScannerRefused(ScannerUnavailable):
    """A local/test stub scanner was configured where stubs are not allowed.

    A `ScannerUnavailable`, so every caller refuses the upload safely."""

    def __init__(self) -> None:
        super().__init__("LOCAL_STUB_REFUSED")


def get_scanner() -> ScannerAdapter:
    """Return the configured adapter (mirrors `apps.people.nin_provider`'s factory).

    A class marked `local_test_only` is refused unless
    `MALWARE_SCANNER_ALLOW_LOCAL_STUBS` is True, which only the local and test
    settings set. Startup validation already refuses it in staging and
    production; this is the second, runtime barrier.
    """
    from importlib import import_module

    from django.conf import settings

    module_path, _, class_name = settings.MALWARE_SCANNER_BACKEND.rpartition(".")
    module = import_module(module_path)
    scanner_class = getattr(module, class_name)
    if getattr(scanner_class, "local_test_only", False) and not getattr(
        settings, "MALWARE_SCANNER_ALLOW_LOCAL_STUBS", False
    ):
        raise LocalStubScannerRefused()
    return scanner_class()
