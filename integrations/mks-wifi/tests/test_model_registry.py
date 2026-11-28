"""Logical identity, scoped authority and independent component revision invariants."""

from types import SimpleNamespace

import pytest

from mks_wifi_bridge.fabrication_archive import FabricationArchive
from mks_wifi_bridge.model_registry import ModelRegistry


def test_model_identity_branching_authority_and_immutable_baseline(tmp_path):
    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "registry.sqlite")))
    registry = ModelRegistry(archive)
    one = registry.create("Bracket", ["repo:one", "repo:two"])
    two = registry.create("Bracket", ["repo:one"])
    assert one["id"] != two["id"]
    revision = registry.revision(one["id"], "design-A")
    child = registry.revision(one["id"], "design-B", [revision["id"]])
    assert child["id"] != revision["id"]
    component = registry.revision(two["id"], "independent component")
    registry.component(child["id"], component["id"], "mount")
    with pytest.raises(ValueError):
        registry.component(component["id"], child["id"], "cycle")
    source = tmp_path / "part.gcode"
    source.write_bytes(b"G1 X10 E1")
    sha = archive.ingest(source)
    registry.representation(revision["id"], sha, "executable_gcode")
    candidate = registry.authority(revision["id"], sha, "executable_gcode")
    assert candidate["state"] == "candidate"
    with pytest.raises(ValueError):
        registry.baseline(revision["id"])
    approved = registry.authority(revision["id"], sha, "executable_gcode", approve=True)
    baseline = registry.baseline(revision["id"])
    assert registry.baseline(revision["id"])["id"] == baseline["id"]
    with pytest.raises(ValueError):
        registry.representation(revision["id"], sha, "executable_gcode", active=False)
    registry.retire_authority(approved["id"])
    registry.representation(revision["id"], sha, "executable_gcode", active=False)
    assert baseline["manifest"]["authorities"][0]["sha256"] == sha
    assert registry.model(one["id"])["contexts"] == ["repo:one", "repo:two"]
