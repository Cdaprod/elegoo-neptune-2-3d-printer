from __future__ import annotations

import pytest

from mks_wifi_bridge.capabilities import CapabilityRegistry


def test_capability_transitions_persist_evidence_and_recover_interrupted_test(tmp_path) -> None:
    path = tmp_path / "capabilities.json"
    registry = CapabilityRegistry(path)
    assert registry.get("home_x")["status"] == "verified"
    assert registry.get("home_y")["status"] == "ready_for_commissioning"

    registry.begin("home_y", "operation-1", ["G28 Y"])
    restarted = CapabilityRegistry(path)
    interrupted = restarted.get("home_y")
    assert interrupted["status"] == "temporarily_blocked"
    assert interrupted["history"][-1]["evidence"]["kind"] == "interrupted_test_recovery"
    assert restarted.safe_to_retry("home_y", "home_y")["status"] == "ready_for_commissioning"

    restarted.begin("home_y", "operation-2", ["G28 Y"])
    verified = restarted.verify(
        "home_y",
        {"id": "operation-2", "macro_id": "home_y", "commands": ["G28 Y"]},
        {"operator_observation": "operator_observed_expected_result"},
    )
    assert verified["status"] == "verified"
    assert verified["evidence"][-1]["commands"] == ["G28 Y"]
    assert CapabilityRegistry(path).get("home_y")["status"] == "verified"


def test_upload_remains_uncommissionable_and_unknown_outcome_requires_explicit_reconcile(
    tmp_path,
) -> None:
    registry = CapabilityRegistry(tmp_path / "capabilities.json")
    upload = registry.get("sd_upload")
    assert upload["status"] == "temporarily_blocked"
    assert upload["commissionable"] is False
    with pytest.raises(ValueError, match="Only a failed or blocked"):
        registry.safe_to_retry("home_y", "home_y")
    with pytest.raises(ValueError, match="installed MKS firmware"):
        registry.safe_to_retry("sd_upload", "anything")
