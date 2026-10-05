"""PostgreSQL advisory-lock helpers and trusted-proxy client-network identity (ADR-0007).

Two independent concerns, both required by the OTP domain
(`apps.accounts.otp`) but owned here as shared `core` infrastructure:

1. Deterministic, deadlock-free acquisition of `pg_advisory_xact_lock`
   locks across every relevant lock class and active rate-limit key
   version.
2. Resolving the client IP address used for per-network throttling without
   ever trusting an arbitrary, unauthenticated header.

Neither function ever logs, stores, or returns a plaintext identity in a
form that could be reverse-engineered without the corresponding HMAC key --
callers combine these helpers with `apps.core.crypto` fingerprints.
"""

from __future__ import annotations

import ipaddress
import struct
from collections.abc import Iterable

# Fixed advisory-lock classid namespaces (ADR-0007). Class 3 (verification)
# is NOT acquired through this module -- the target row already exists by
# definition at verification time, so ordinary `SELECT ... FOR UPDATE`
# row-level locking is used instead.
ADVISORY_LOCK_CLASS_RECIPIENT_ISSUANCE = 1
ADVISORY_LOCK_CLASS_NETWORK_ISSUANCE = 2
ADVISORY_LOCK_CLASS_VERIFICATION_ROW = 3
ADVISORY_LOCK_CLASS_RECIPIENT_TEMPORARY_LOCK = 4
# Identity write serialization. These namespaces protect "no matching row
# exists yet" checks across active blind-index key versions during rotation.
ADVISORY_LOCK_CLASS_CONTACT_IDENTITY = 5
ADVISORY_LOCK_CLASS_OFFICIAL_IDENTIFIER = 6
# P4-4: serializes operational sign-in attempts for one typed email.
ADVISORY_LOCK_CLASS_OPERATIONAL_SIGN_IN = 7


def objid_from_fingerprint(digest: bytes) -> int:
    """First 4 bytes of a keyed HMAC digest, interpreted as a signed 32-bit integer.

    A 32-bit slice can collide across unrelated keys; ADR-0007 accepts this
    as a documented trade-off (a collision only causes benign extra
    serialization, never a missed limit).
    """
    return struct.unpack(">i", digest[:4])[0]


def lock_key(classid: int, digest: bytes) -> tuple[int, int]:
    """Return the `(classid, objid)` advisory-lock pair for one fingerprint digest."""
    return classid, objid_from_fingerprint(digest)


def order_lock_keys(pairs: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Deduplicate and sort `(classid, objid)` pairs ascending -- ADR-0007's total ordering.

    Every transaction that needs more than one advisory lock MUST acquire
    them in exactly this order (across every lock class and every active
    key version) for deadlock-freedom; no call site invents its own order.
    """
    return sorted(set(pairs))


def acquire_advisory_locks(connection, pairs: Iterable[tuple[int, int]]) -> None:
    """Acquire `pg_advisory_xact_lock` for every pair, in the deterministic total order.

    Transaction-scoped: locks release automatically at commit or rollback,
    so none can leak. Callers MUST acquire these before taking any related
    row lock, and MUST call this from inside the transaction whose outcome
    the lock is meant to serialize.
    """
    ordered = order_lock_keys(pairs)
    with connection.cursor() as cursor:
        for classid, objid in ordered:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [classid, objid])


# ---------------------------------------------------------------------------
# Trusted-proxy-aware client-network identity resolution (ADR-0007)
# ---------------------------------------------------------------------------


def _strip_zone_and_port(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return raw
        return raw[1:end]
    if raw.count(":") == 1:
        host, _, _port = raw.partition(":")
        return host
    return raw


def normalize_client_address(raw: str) -> str | None:
    """Normalize an address string; return `None` if it does not parse as an IP.

    Canonicalizes IPv6 compression, folds an IPv4-mapped IPv6 address to
    plain IPv4, and strips a zone identifier or port first.
    """
    candidate = _strip_zone_and_port(raw).split("%", 1)[0].strip()
    if not candidate:
        return None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            address = mapped
    return str(address)


def _is_trusted(
    address: str, trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)


def resolve_client_network_identity(
    *,
    remote_addr: str,
    forwarded_for: str | None,
    forwarding_enabled: bool,
    trusted_proxy_cidrs: list[str],
    max_forwarded_hops: int,
) -> str:
    """Return the normalized client address to use for per-network throttling.

    Local/test (`forwarding_enabled=False`): always the direct peer;
    `X-Forwarded-For` is ignored entirely, whatever it contains.

    Staging/production (`forwarding_enabled=True`): forwarded headers are
    honored only when the direct peer is itself inside a configured
    trusted-proxy CIDR. The chain is then walked from the right, skipping
    each entry while it is itself a trusted proxy; the first non-trusted
    entry is the client. An over-long chain, a malformed entry, or a chain
    made entirely of trusted proxies (no genuine client entry) all fall
    back to the direct peer -- never the bare leftmost or rightmost value.
    """
    direct = normalize_client_address(remote_addr) or remote_addr

    if not forwarding_enabled or not forwarded_for:
        return direct

    networks = [ipaddress.ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs]
    if not networks or not _is_trusted(direct, networks):
        return direct

    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    if not hops or len(hops) > max_forwarded_hops:
        return direct

    normalized_hops: list[str] = []
    for hop in hops:
        normalized = normalize_client_address(hop)
        if normalized is None:
            return direct
        normalized_hops.append(normalized)

    for hop in reversed(normalized_hops):
        if not _is_trusted(hop, networks):
            return hop

    return direct


def resolve_request_client_network_identity(request) -> str:
    """Convenience wrapper reading the trusted-proxy settings from `django.conf.settings`.

    `request` is a Django `HttpRequest` (typed loosely to avoid a hard
    Django import at module load for the pure-function tests above).
    """
    from django.conf import settings

    return resolve_client_network_identity(
        remote_addr=request.META.get("REMOTE_ADDR", ""),
        forwarded_for=request.META.get("HTTP_X_FORWARDED_FOR"),
        forwarding_enabled=settings.TRUSTED_PROXY_FORWARDING_ENABLED,
        trusted_proxy_cidrs=settings.TRUSTED_PROXY_CIDRS,
        max_forwarded_hops=settings.TRUSTED_PROXY_MAX_FORWARDED_HOPS,
    )
