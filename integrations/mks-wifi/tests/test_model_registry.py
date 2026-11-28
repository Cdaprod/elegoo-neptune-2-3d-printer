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


def test_inventory_registers_real_fingerprints_without_hydration_or_model_assumptions(tmp_path):
    from mks_wifi_bridge.archive_backup import backup, restore

    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "metadata.sqlite")))
    registry = ModelRegistry(archive)
    sha = "a" * 64
    report = {
        "schema_version": 1,
        "read_only": True,
        "files": [
            {
                "path": "/approved/one/part.FCStd",
                "sha256": sha,
                "kind": "freecad",
                "size_bytes": 20,
            },
            {
                "path": "/approved/two/renamed.FCStd",
                "sha256": sha,
                "kind": "freecad",
                "size_bytes": 20,
            },
        ],
    }
    result = registry.import_inventory(report)
    assert result["hydrated"] is False and result["models_created"] == 0
    assert len(registry.artifact_candidates()) == 1
    assert len(registry.artifact_candidates()[0]["locations"]) == 2
    assert not list(archive.root.glob("*.gcode"))
    model = registry.create("Explicit logical object")
    revision = registry.revision(model["id"], "Independent design revision")
    registry.representation(revision["id"], sha, "native_design")
    assert (
        registry.authority(revision["id"], sha, "native_design", approve=True)["state"]
        == "approved"
    )
    # Authority approval is independent of archival hydration.
    with pytest.raises(FileNotFoundError):
        archive.verified(sha)
    packed = tmp_path / "metadata.tar.gz"
    assert backup(archive.timeline.path, packed)["originals"] == []
    restore(packed, tmp_path / "restored")


def test_source_surface_preview_and_typed_archive_restore(tmp_path):
    import hashlib
    from io import BytesIO

    from mks_wifi_bridge.archive_backup import backup, restore
    from mks_wifi_bridge.engineering_preview import EngineeringPreview
    from mks_wifi_bridge.fabrication_ingestion import ArtifactSubmission, FabricationIngestion

    source = tmp_path / "actual.stl"
    data = b"""solid triangle
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 10 0 0
vertex 0 10 0
endloop
endfacet
endsolid triangle
"""
    source.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "surface.sqlite")))
    registry = ModelRegistry(archive)
    registry.import_inventory(
        {
            "schema_version": 1,
            "read_only": True,
            "files": [{"path": str(source), "sha256": sha, "kind": "stl", "size_bytes": len(data)}],
        }
    )
    preview = EngineeringPreview(archive)
    model = preview.geometry(sha)
    assert model["source"] == "original_stl_surface"
    assert model["stats"]["rendered_triangles"] == 1
    assert preview.thumbnail(sha).startswith(b"\x89PNG")
    assert not list(archive.root.glob("*.gcode")) and not list(archive.root.glob("*.blob"))
    result = FabricationIngestion(archive).submit(
        ArtifactSubmission(
            "actual.stl", "http", "upload:actual.stl", artifact_kind="manufacturing_geometry"
        ),
        BytesIO(data),
    )
    assert result == sha and archive.verified(sha).read_bytes() == data
    with pytest.raises(ValueError, match="Only executable"):
        archive.stage_artifact(sha)
    packed = tmp_path / "typed.tar.gz"
    backup(archive.timeline.path, packed)
    restore(packed, tmp_path / "typed-restore")
    restored = FabricationArchive(
        SimpleNamespace(path=str(tmp_path / "typed-restore/ledger.sqlite"))
    )
    assert restored.verified(sha).read_bytes() == data
