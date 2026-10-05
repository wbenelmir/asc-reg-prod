"""ClamAV clamd adapter: INSTREAM protocol tests against a SIMULATED clamd.

The server below is a small scripted stand-in on a loopback port (and a Unix
socket where the platform supports one). It proves the framing and the
fail-closed interpretation of every answer; it is NOT a live ClamAV check.
The live acceptance check (a harmless clean file and the standard antivirus
test file against the real daemon) is an operator step:
`manage.py check_malware_scanner` (docs/deployment/README.md).
"""

from __future__ import annotations

import io
import logging
import os
import socket
import socketserver
import struct
import tempfile
import threading
import time

import pytest

from apps.documents.scanning import (
    ClamdScanner,
    LocalStubScannerRefused,
    ScannerUnavailable,
    get_scanner,
)


class _Recorder:
    def __init__(self) -> None:
        self.commands: list[bytes] = []
        self.chunk_lengths: list[int] = []
        self.payload = bytearray()
        self.connections = 0


def _read_exact(rfile, size: int) -> bytes:
    data = b""
    while len(data) < size:
        piece = rfile.read(size - len(data))
        if not piece:
            break
        data += piece
    return data


def _read_command(rfile) -> bytes:
    command = b""
    while not command.endswith(b"\0"):
        piece = rfile.read(1)
        if not piece:
            break
        command += piece
    return command


def _make_handler(behaviour, recorder: _Recorder):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            recorder.connections += 1
            command = _read_command(self.rfile)
            recorder.commands.append(command)
            if command != b"zINSTREAM\0":
                behaviour(self, command)
                return
            if getattr(behaviour, "answer_before_stream", False):
                behaviour(self, command)
                return
            while True:
                header = _read_exact(self.rfile, 4)
                if len(header) < 4:
                    return
                (length,) = struct.unpack("!I", header)
                recorder.chunk_lengths.append(length)
                if length == 0:
                    break
                recorder.payload.extend(_read_exact(self.rfile, length))
            behaviour(self, command)

    return Handler


class _TcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def clamd():
    servers = []

    def start(behaviour):
        recorder = _Recorder()
        server = _TcpServer(("127.0.0.1", 0), _make_handler(behaviour, recorder))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        host, port = server.server_address
        return recorder, {"host": host, "port": port}

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _answer(text: bytes):
    def behaviour(handler, _command):
        handler.wfile.write(text)
        handler.wfile.flush()

    return behaviour


def _scanner(address, **overrides) -> ClamdScanner:
    options = {"connect_timeout": 2, "scan_timeout": 5, "max_stream_bytes": 1024 * 1024}
    options.update(overrides)
    return ClamdScanner(socket_path="", **address, **options)


def test_a_clean_answer_is_the_only_clean_result_and_the_framing_is_exact(clamd) -> None:
    recorder, address = clamd(_answer(b"stream: OK\0"))
    content = os.urandom(150_000)
    result = _scanner(address, chunk_bytes=64 * 1024).scan(io.BytesIO(content))
    assert result.clean is True and result.reason_code is None
    assert recorder.commands == [b"zINSTREAM\0"]
    assert recorder.chunk_lengths == [65536, 65536, 150_000 - 131072, 0]
    assert bytes(recorder.payload) == content


def test_a_found_answer_is_a_positive_result_without_the_signature_name(clamd) -> None:
    _recorder, address = clamd(_answer(b"stream: Example-Test-Signature FOUND\0"))
    result = _scanner(address).scan(io.BytesIO(b"synthetic test bytes"))
    assert result.clean is False
    assert result.reason_code == "INFECTED"
    assert "Example" not in repr(result)


def test_an_answer_split_across_many_reads_is_reassembled(clamd) -> None:
    def trickle(handler, _command):
        for byte in b"stream: OK\0":
            handler.wfile.write(bytes([byte]))
            handler.wfile.flush()
            time.sleep(0.005)

    _recorder, address = clamd(trickle)
    assert _scanner(address).scan(io.BytesIO(b"abc")).clean is True


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (b"stream: maybe\0", "MALFORMED_RESPONSE"),
        (b"stream: OK extra\0", "MALFORMED_RESPONSE"),
        (b"STREAM: OK\0", "MALFORMED_RESPONSE"),
        (b"stream: OK\0trailing", "MALFORMED_RESPONSE"),
        (b"stream: \xff\xfe FOUND\0", "MALFORMED_RESPONSE"),
        (b"stream: OK\nstream: OK\0", "MALFORMED_RESPONSE"),
        (b"x" * 5000, "MALFORMED_RESPONSE"),
        (b"INSTREAM size limit exceeded. ERROR\0", "SIZE_LIMIT"),
        (b"Can't allocate memory ERROR\0", "SCANNER_ERROR"),
        (b"stream: OK", "TRUNCATED_RESPONSE"),
        (b"", "TRUNCATED_RESPONSE"),
    ],
)
def test_every_other_answer_raises_without_a_verdict(clamd, answer, reason) -> None:
    _recorder, address = clamd(_answer(answer))
    with pytest.raises(ScannerUnavailable) as excinfo:
        _scanner(address).scan(io.BytesIO(b"synthetic"))
    assert excinfo.value.reason_code == reason


def test_a_server_that_never_answers_times_out_within_the_bound(clamd) -> None:
    def silent(handler, _command):
        time.sleep(3)

    _recorder, address = clamd(silent)
    started = time.monotonic()
    with pytest.raises(ScannerUnavailable) as excinfo:
        _scanner(address, scan_timeout=1).scan(io.BytesIO(b"synthetic"))
    assert excinfo.value.reason_code == "TIMEOUT"
    assert time.monotonic() - started < 2.5


def test_a_size_limit_answer_sent_before_the_stream_ends_is_honoured(clamd) -> None:
    def early_limit(handler, _command):
        handler.wfile.write(b"INSTREAM size limit exceeded. ERROR\0")
        handler.wfile.flush()
        handler.connection.shutdown(socket.SHUT_RDWR)

    early_limit.answer_before_stream = True
    _recorder, address = clamd(early_limit)
    with pytest.raises(ScannerUnavailable) as excinfo:
        _scanner(address, chunk_bytes=1024).scan(io.BytesIO(os.urandom(600_000)))
    assert excinfo.value.reason_code in {"SIZE_LIMIT", "DISCONNECTED"}


def test_content_above_the_limit_is_refused_before_any_connection(clamd) -> None:
    recorder, address = clamd(_answer(b"stream: OK\0"))
    with pytest.raises(ScannerUnavailable) as excinfo:
        _scanner(address, max_stream_bytes=10).scan(io.BytesIO(b"x" * 11))
    assert excinfo.value.reason_code == "SIZE_LIMIT"
    assert recorder.connections == 0


def test_an_unsized_stream_is_cut_at_the_limit(clamd) -> None:
    class Unsized(io.RawIOBase):
        def __init__(self, total: int) -> None:
            self.left = total

        def readable(self) -> bool:
            return True

        def seekable(self) -> bool:
            return False

        def tell(self) -> int:
            raise OSError("not seekable")

        def read(self, size: int = -1) -> bytes:
            size = self.left if size < 0 else min(size, self.left)
            self.left -= size
            return b"y" * size

    _recorder, address = clamd(_answer(b"stream: OK\0"))
    with pytest.raises(ScannerUnavailable) as excinfo:
        _scanner(address, max_stream_bytes=100, chunk_bytes=40).scan(Unsized(500))
    assert excinfo.value.reason_code == "SIZE_LIMIT"


def test_a_closed_port_is_connection_failed() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(ScannerUnavailable) as excinfo:
        _scanner({"host": "127.0.0.1", "port": port}).scan(io.BytesIO(b"synthetic"))
    # Refused on Linux; Windows retries a refused loopback SYN until the connect
    # timeout. Both are "no verdict".
    assert excinfo.value.reason_code in {"CONNECTION_FAILED", "CONNECT_TIMEOUT"}


@pytest.mark.parametrize(
    "address",
    [
        {"socket_path": "", "host": ""},
        {"socket_path": "/run/clamav/clamd.ctl", "host": "127.0.0.1"},
    ],
)
def test_neither_or_both_endpoints_is_not_configured(address) -> None:
    scanner = ClamdScanner(port=3310, connect_timeout=1, scan_timeout=1, **address)
    with pytest.raises(ScannerUnavailable) as excinfo:
        scanner.scan(io.BytesIO(b"synthetic"))
    assert excinfo.value.reason_code == "NOT_CONFIGURED"


def test_ping_and_version_need_the_exact_answers(clamd) -> None:
    def respond(handler, command):
        if command == b"zPING\0":
            handler.wfile.write(b"PONG\0")
        elif command == b"zVERSION\0":
            handler.wfile.write(b"ClamAV 1.4.1/27000/Thu Oct  1 08:00:00 2026\0")
        handler.wfile.flush()

    _recorder, address = clamd(respond)
    scanner = _scanner(address)
    assert scanner.ping() is True
    assert scanner.version().startswith("ClamAV ")


def test_logs_carry_the_reason_code_only(clamd, caplog) -> None:
    _recorder, address = clamd(_answer(b"stream: something unexpected\0"))
    secret_bytes = b"synthetic-document-content-7f3a"
    with caplog.at_level(logging.WARNING, logger="asc2026.documents.scanning"):
        with pytest.raises(ScannerUnavailable):
            _scanner(address).scan(io.BytesIO(secret_bytes))
    text = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert "MALFORMED_RESPONSE" in text
    assert "synthetic-document-content" not in text
    assert "something unexpected" not in text


@pytest.mark.skipif(
    not hasattr(socketserver, "UnixStreamServer"), reason="no Unix sockets on this platform"
)
def test_the_unix_socket_endpoint_speaks_the_same_protocol() -> None:
    recorder = _Recorder()
    path = os.path.join(tempfile.mkdtemp(), "clamd.sock")
    server = socketserver.UnixStreamServer(path, _make_handler(_answer(b"stream: OK\0"), recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scanner = ClamdScanner(socket_path=path, host="", connect_timeout=2, scan_timeout=5)
        assert scanner.scan(io.BytesIO(b"abc")).clean is True
        assert recorder.commands == [b"zINSTREAM\0"]
    finally:
        server.shutdown()
        server.server_close()


def test_get_scanner_refuses_a_local_stub_unless_stubs_are_allowed(settings) -> None:
    settings.MALWARE_SCANNER_BACKEND = "apps.documents.scanning.DeterministicStubScanner"
    settings.MALWARE_SCANNER_ALLOW_LOCAL_STUBS = False
    with pytest.raises(LocalStubScannerRefused) as excinfo:
        get_scanner()
    assert isinstance(excinfo.value, ScannerUnavailable)
    settings.MALWARE_SCANNER_ALLOW_LOCAL_STUBS = True
    assert get_scanner().scan(io.BytesIO(b"x")).clean is True


def test_get_scanner_builds_the_clamd_adapter_from_settings(settings) -> None:
    settings.MALWARE_SCANNER_BACKEND = "apps.documents.scanning.ClamdScanner"
    settings.MALWARE_SCANNER_ALLOW_LOCAL_STUBS = False
    settings.CLAMD_SOCKET_PATH = ""
    settings.CLAMD_HOST = "clamd.internal.example.invalid"
    settings.CLAMD_PORT = 3311
    settings.CLAMD_CONNECT_TIMEOUT_SECONDS = 3
    settings.CLAMD_SCAN_TIMEOUT_SECONDS = 30
    settings.CLAMD_MAX_STREAM_BYTES = 9_000_000
    scanner = get_scanner()
    assert isinstance(scanner, ClamdScanner)
    assert (scanner.host, scanner.port) == ("clamd.internal.example.invalid", 3311)
    assert (scanner.connect_timeout, scanner.scan_timeout) == (3.0, 30.0)
    assert scanner.max_stream_bytes == 9_000_000
