"""The development simulation and the opaque correlation reference (IDV-2).

The former `LocalStubNinProvider.verify()` (a match for any NIN ending in an
even digit) is superseded: verification is now a lookup by NIN with a local
comparison (amendment A-13). The development simulation answers from
synthetic response bodies through the same strict contract code as the real
adapter, is labelled non-official, and never puts a NIN fragment in its
correlation reference (Prompt 6 P6-H-01 still holds). Synthetic data only.
"""

from __future__ import annotations

import json

import pytest

from apps.people import nin_provider as nin_provider_module
from apps.people.identity_contract import LookupKind, PresumeFlag
from apps.people.nin_provider import (
    SIMULATED_INVALID_NIN,
    SIMULATED_UNAVAILABLE_NIN,
    SIMULATION_FIXTURE,
    DisabledNinProvider,
    LocalSimulationNinProvider,
    LocalStubNinProvider,
    MinistryNinProvider,
    UnavailableNinProvider,
    UnavailableSimulationNinProvider,
    generate_opaque_local_reference,
)

SYNTHETIC_FOUND_NIN = "990000000000000010"
SYNTHETIC_UNKNOWN_NIN = "123456789012345678"


def test_generate_opaque_local_reference_never_takes_participant_data() -> None:
    reference = generate_opaque_local_reference(prefix="sim")
    assert reference.startswith("sim-")
    assert SYNTHETIC_FOUND_NIN not in reference


@pytest.mark.parametrize("nin_value", [SYNTHETIC_FOUND_NIN, SYNTHETIC_UNKNOWN_NIN])
def test_simulation_reference_is_opaque(nin_value: str) -> None:
    outcome = LocalSimulationNinProvider().lookup(nin_value)
    assert nin_value not in outcome.request_reference
    assert nin_value[:4] not in outcome.request_reference
    assert nin_value[-4:] not in outcome.request_reference
    assert outcome.request_reference.startswith("sim-")


def test_simulation_answers_through_the_strict_contract() -> None:
    found = LocalSimulationNinProvider().lookup(SYNTHETIC_FOUND_NIN)
    assert found.kind == LookupKind.FOUND
    assert found.facts.nin == SYNTHETIC_FOUND_NIN
    assert found.facts.presume == PresumeFlag.FALSE
    assert LocalSimulationNinProvider().lookup(SYNTHETIC_UNKNOWN_NIN).kind == LookupKind.NOT_FOUND


def test_simulation_failure_numbers() -> None:
    unavailable = LocalSimulationNinProvider().lookup(SIMULATED_UNAVAILABLE_NIN)
    assert unavailable.kind == LookupKind.UNAVAILABLE and unavailable.retryable
    assert LocalSimulationNinProvider().lookup(SIMULATED_INVALID_NIN).kind == (
        LookupKind.INVALID_RESPONSE
    )


def test_simulations_and_the_disabled_backend_are_never_official() -> None:
    for provider in (
        LocalSimulationNinProvider(),
        UnavailableSimulationNinProvider(),
        DisabledNinProvider(),
    ):
        assert provider.IS_OFFICIAL is False
    assert MinistryNinProvider.IS_OFFICIAL is True
    assert DisabledNinProvider().lookup(SYNTHETIC_FOUND_NIN).kind == LookupKind.NOT_CONFIGURED


def test_the_deprecated_names_are_simulations() -> None:
    assert LocalStubNinProvider is LocalSimulationNinProvider
    assert UnavailableNinProvider is UnavailableSimulationNinProvider


def test_the_simulation_fixture_is_marked_synthetic_and_uses_the_observed_shape() -> None:
    document = json.loads(SIMULATION_FIXTURE.read_text(encoding="utf-8"))
    assert "SYNTHETIC" in document["_notice"]
    for nin, body in document["responses"].items():
        assert len(nin) == 18 and nin.isdigit()
        assert "identite" in body
        if body["identite"] is not None:
            assert set(body["identite"]) <= {"nin", "nom_f", "pren_f", "d_nais", "presume"}


def test_simulation_uses_the_patched_reference_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        nin_provider_module, "generate_opaque_local_reference", lambda prefix="ref": "sim-fixed"
    )
    assert LocalSimulationNinProvider().lookup(SYNTHETIC_FOUND_NIN).request_reference == "sim-fixed"
