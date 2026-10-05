"""A synthetic local HTTPS peer for the transport-deadline tests (IDV-C1, R-IDV-03).

It listens on 127.0.0.1 only, with a throwaway CA and server certificate
generated at run time into a temporary folder (no key material is ever
committed). Each response is a scripted list of `(delay_seconds, bytes)` steps,
so a test can make the peer slow at the TLS handshake, in the headers, in the
body, or before the end of the stream. It is a test fixture, never a ministry
stand-in: it knows nothing of the real service.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

Step = tuple[float, bytes]
Script = Callable[[str, str], list[Step]]


def _write_certificates(directory: Path) -> tuple[Path, Path, Path]:
    """A CA and a server certificate for 127.0.0.1, valid for one hour."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "IDV C1 synthetic test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "synthetic-ca.pem"
    cert_path = directory / "synthetic-server.pem"
    key_path = directory / "synthetic-server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


def response_steps(
    status: int = 200,
    body: bytes = b"{}",
    *,
    headers: dict[str, str] | None = None,
    content_length: bool = True,
) -> list[Step]:
    """A complete response sent at once."""
    lines = [f"HTTP/1.1 {status} SYNTHETIC"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    if content_length:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    return [(0.0, head + body)]


@dataclass
class TlsPeer:
    """Serve `script(method, path)` for every connection until stopped."""

    directory: Path
    script: Script
    handshake_delay: float = 0.0
    hold_after_seconds: float = 0.0
    connections: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ca_path, cert_path, key_path = _write_certificates(self.directory)
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(cert_path, key_path)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def __enter__(self) -> TlsPeer:
        thread = threading.Thread(target=self._serve, daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._listener.close()
        for thread in self._threads:
            thread.join(timeout=15)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _address = self._listener.accept()
            except TimeoutError, OSError:
                continue
            self.connections += 1
            thread = threading.Thread(target=self._handle, args=(raw,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _sleep(self, seconds: float) -> bool:
        return not self._stop.wait(seconds)

    def _handle(self, raw: socket.socket) -> None:
        try:
            raw.settimeout(20)
            if self.handshake_delay and not self._sleep(self.handshake_delay):
                return
            with self._context.wrap_socket(raw, server_side=True) as tls:
                data = b""
                while b"\r\n\r\n" not in data and len(data) < 65536:
                    chunk = tls.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                head, _sep, rest = data.partition(b"\r\n\r\n")
                request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
                method, path = request_line.split(" ")[:2]
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _colon, value = line.decode("latin-1").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                while len(rest) < length:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                self.requests.append((method, path))
                for delay, payload in self.script(method, path):
                    if delay and not self._sleep(delay):
                        return
                    tls.sendall(payload)
                if self.hold_after_seconds:
                    self._sleep(self.hold_after_seconds)
        except OSError, ssl.SSLError, ValueError:
            pass
        finally:
            raw.close()


def trickle(head: bytes, body: bytes, *, interval: float) -> list[Step]:
    """Send `head` at once, then `body` one byte per `interval` seconds."""
    return [(0.0, head), *((interval, body[index : index + 1]) for index in range(len(body)))]


def elapsed(start: float) -> float:
    return time.monotonic() - start
