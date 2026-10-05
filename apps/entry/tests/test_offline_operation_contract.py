"""Operation input validation without a database or device credentials."""

from copy import deepcopy

import pytest

from apps.entry.offline_contract import OfflineContractError, validate_operation


def operation():
    return {
        "schema_version": 1,
        "operation_id": "12345678-1234-4234-8234-123456789012",
        "type": "ENTRY_DECISION",
        "device": "D" * 22,
        "event": "SYNTHETIC",
        "store": "S" * 22,
        "sequence": 1,
        "prev": "0" * 64,
        "occurred_at": 1800000010,
        "device_time": 1800000010,
        "server_offset": 0,
        "state": "OFFLINE_ACTIVE",
        "package": {
            "id": "P" * 22,
            "version": 1,
            "delta_version": None,
            "band": "FRESH",
            "data_cutoff_at": 1800000000,
            "critical_delta_cutoff_at": 1800000000,
        },
        "grant": "G" * 22,
        "gate": "GA",
        "zone": "MAIN",
        "credential": {"jti": "J" * 22, "kid": "v1"},
        "local": {
            "code": "VALID",
            "result": "ALLOWED",
            "reason": "",
            "blockers": [],
            "advisories": [],
            "latency_ms": 12,
        },
        "decision": "ADMIT",
        "decision_reason": "",
        "override": None,
    }


def test_minimal_decision_matches_contract():
    validate_operation(operation())


@pytest.mark.parametrize(
    "path",
    [
        ("type",),
        ("state",),
        ("decision",),
        ("decision_reason",),
        ("package", "band"),
        ("local", "code"),
        ("local", "result"),
        ("local", "reason"),
    ],
)
@pytest.mark.parametrize("invalid", [[], {}, True, None])
def test_untrusted_vocabulary_types_raise_contract_errors(path, invalid):
    value = operation()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = deepcopy(invalid)
    with pytest.raises(OfflineContractError):
        validate_operation(value)


@pytest.mark.parametrize("field", ["occurred_at", "device_time"])
@pytest.mark.parametrize("invalid", [True, -1, 2**40, 253402300799])
def test_timestamps_are_representable_with_query_boundary(field, invalid):
    value = operation()
    value[field] = invalid
    with pytest.raises(OfflineContractError):
        validate_operation(value)


def test_boolean_schema_is_not_version_one():
    value = operation()
    value["schema_version"] = True
    with pytest.raises(OfflineContractError):
        validate_operation(value)


@pytest.mark.parametrize("signature", [None, {}, "synthetic-prohibited-value", "A" * 85])
def test_unusable_signature_field_never_reaches_row_intake(signature):
    import json

    from apps.entry.services.offline_sync import _envelope_usable

    value = operation()
    assert not _envelope_usable(
        {
            "operation_id": value["operation_id"],
            "sequence": 1,
            "op": json.dumps(value),
            "signature": signature,
        }
    )
