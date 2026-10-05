"""Advisory-lock ordering and trusted-proxy client-network identity tests (ADR-0007).

No database access required for these unit tests -- `order_lock_keys()` is
pure, and `resolve_client_network_identity()` never opens a connection.
Real multi-connection advisory-lock behavior is exercised against
PostgreSQL in `tests/concurrency/`.
"""

from __future__ import annotations

from apps.core.concurrency import (
    ADVISORY_LOCK_CLASS_NETWORK_ISSUANCE,
    ADVISORY_LOCK_CLASS_RECIPIENT_ISSUANCE,
    lock_key,
    normalize_client_address,
    objid_from_fingerprint,
    order_lock_keys,
    resolve_client_network_identity,
)


def test_objid_from_fingerprint_is_deterministic() -> None:
    digest = b"\x00\x00\x00\x01" + b"rest-of-digest-ignored-------"
    assert objid_from_fingerprint(digest) == 1


def test_order_lock_keys_is_sorted_and_deduplicated() -> None:
    pairs = [(2, 5), (1, 9), (1, 2), (2, 5)]
    assert order_lock_keys(pairs) == [(1, 2), (1, 9), (2, 5)]


def test_order_lock_keys_is_the_same_regardless_of_input_order() -> None:
    a = [lock_key(ADVISORY_LOCK_CLASS_RECIPIENT_ISSUANCE, b"digest-one-------------------")]
    b = [lock_key(ADVISORY_LOCK_CLASS_NETWORK_ISSUANCE, b"digest-two-------------------")]
    assert order_lock_keys(a + b) == order_lock_keys(b + a)


def test_normalize_client_address_folds_ipv4_mapped_ipv6() -> None:
    assert normalize_client_address("::ffff:203.0.113.5") == "203.0.113.5"


def test_normalize_client_address_strips_port_and_zone() -> None:
    assert normalize_client_address("203.0.113.5:8443") == "203.0.113.5"
    assert normalize_client_address("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_address("fe80::1%eth0") == "fe80::1"


def test_normalize_client_address_rejects_garbage() -> None:
    assert normalize_client_address("not-an-ip-address") is None


def test_local_ignores_forwarded_header_entirely() -> None:
    result = resolve_client_network_identity(
        remote_addr="127.0.0.1",
        forwarded_for="203.0.113.99",
        forwarding_enabled=False,
        trusted_proxy_cidrs=[],
        max_forwarded_hops=5,
    )
    assert result == "127.0.0.1"


def test_untrusted_direct_peer_is_never_overridden_by_forwarded_header() -> None:
    result = resolve_client_network_identity(
        remote_addr="198.51.100.1",  # not in any trusted CIDR
        forwarded_for="203.0.113.99",
        forwarding_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        max_forwarded_hops=5,
    )
    assert result == "198.51.100.1"


def test_trusted_proxy_honors_first_non_trusted_entry_from_the_right() -> None:
    result = resolve_client_network_identity(
        remote_addr="10.0.0.5",
        forwarded_for="203.0.113.7, 10.0.0.4, 10.0.0.5",
        forwarding_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        max_forwarded_hops=5,
    )
    assert result == "203.0.113.7"


def test_chain_of_entirely_trusted_proxies_falls_back_to_direct_peer() -> None:
    result = resolve_client_network_identity(
        remote_addr="10.0.0.5",
        forwarded_for="10.0.0.3, 10.0.0.4, 10.0.0.5",
        forwarding_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        max_forwarded_hops=5,
    )
    assert result == "10.0.0.5"


def test_over_long_forwarded_chain_falls_back_to_direct_peer() -> None:
    too_many_hops = ", ".join(["203.0.113.1"] * 10)
    result = resolve_client_network_identity(
        remote_addr="10.0.0.5",
        forwarded_for=too_many_hops,
        forwarding_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        max_forwarded_hops=5,
    )
    assert result == "10.0.0.5"


def test_malformed_forwarded_entry_falls_back_to_direct_peer() -> None:
    result = resolve_client_network_identity(
        remote_addr="10.0.0.5",
        forwarded_for="not-an-ip, 10.0.0.4",
        forwarding_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        max_forwarded_hops=5,
    )
    assert result == "10.0.0.5"


def test_spoofed_forwarded_header_without_trusted_cidr_configured_is_ignored() -> None:
    result = resolve_client_network_identity(
        remote_addr="10.0.0.5",
        forwarded_for="1.2.3.4",
        forwarding_enabled=True,
        trusted_proxy_cidrs=[],
        max_forwarded_hops=5,
    )
    assert result == "10.0.0.5"
