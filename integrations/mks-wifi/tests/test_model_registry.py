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


def test_portable_manifest_import_keeps_identity_and_requires_explicit_approval(tmp_path):
    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "export.sqlite")))
    registry = ModelRegistry(archive)
    model = registry.create("Logical model")
    revision = registry.revision(model["id"], "A")
    source = tmp_path / "part.gcode"
    source.write_bytes(b"G1 X10 E1")
    sha = archive.ingest(source)
    registry.representation(revision["id"], sha, "executable_gcode")
    registry.authority(revision["id"], sha, "executable_gcode", approve=True)
    manifest = registry.manifest(revision["id"])
    (tmp_path / "target").mkdir()
    target = FabricationArchive(SimpleNamespace(path=str(tmp_path / "target/import.sqlite")))
    imported = ModelRegistry(target)
    result = imported.import_manifest(manifest)
    assert result["model_id"] == model["id"]
    assert imported.manifest(revision["id"])["authorities"][0]["state"] == "candidate"
    assert not list(target.root.glob("*.gcode"))
    with pytest.raises(ValueError):
        imported.import_manifest(manifest, approve=True)
    target.ingest(source)
    imported.import_manifest(manifest, approve=True)
    assert imported.manifest(revision["id"])["authorities"][0]["state"] == "approved"


def test_observation_attachment_is_durable_and_does_not_stage_or_assess(tmp_path):
    from io import BytesIO

    from mks_wifi_bridge.fabrication_ingestion import ArtifactSubmission, FabricationIngestion
    from mks_wifi_bridge.motion_timeline import MotionTimeline

    archive = FabricationArchive(MotionTimeline(tmp_path / "observation.sqlite"))
    with archive.db() as db:
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            ("attempt", "part.gcode", "2000", "2001", "unknown", None, None, "printer_sd"),
        )
    sha = FabricationIngestion(archive).submit(
        ArtifactSubmission(
            "telemetry.json", "http", "observation:operator", artifact_kind="observation"
        ),
        BytesIO(b'{"source":"operator_supplied","note":"No outcome known"}'),
    )
    first = archive.attach_observation("attempt", sha, "telemetry")
    assert archive.attach_observation("attempt", sha, "telemetry")["id"] == first["id"]
    detail = archive.execution_detail("attempt")
    assert detail["execution"]["state"] == "unknown"
    assert len(detail["attachments"]) == 1 and detail["quality_assessments"] == []
    assert archive.stages() == []


def test_baseline_reprint_preserves_original_execution_and_exact_inputs(tmp_path):
    from mks_wifi_bridge.motion_timeline import MotionTimeline

    archive = FabricationArchive(MotionTimeline(tmp_path / "baseline.sqlite"))
    registry = ModelRegistry(archive)
    source = tmp_path / "part.gcode"
    source.write_bytes(b"G1 X10 E1")
    sha = archive.ingest(source)
    model = registry.create("Part")
    revision = registry.revision(model["id"], "A")
    registry.representation(revision["id"], sha, "executable_gcode")
    registry.authority(revision["id"], sha, "executable_gcode", approve=True)
    baseline = registry.baseline(revision["id"])
    with archive.db() as db:
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            ("old", "part.gcode", "2000", "2001", "failed", None, None, "printer_sd"),
        )
    archive.link_execution("old", sha, "operator_supplied", "native_sd")
    archive.link_baseline("old", baseline["id"])
    one, two = archive.stage_again("old"), archive.stage_again("old")
    assert one["id"] != two["id"]
    assert one["baseline_id"] == two["baseline_id"] == baseline["id"]
    assert all(stage["baseline_id"] == baseline["id"] for stage in archive.stages())
    assert archive.execution_detail("old")["execution"]["state"] == "failed"
    source.write_bytes(b"G1 X11 E2")
    other = archive.ingest(source)
    with pytest.raises(ValueError, match="exact executable"):
        archive.stage_artifact(other, baseline_id=baseline["id"])


def test_registry_api_paging_declarations_and_write_origin(tmp_path):
    from fastapi.testclient import TestClient
    from test_observatory import make_service

    from mks_wifi_bridge.motion_timeline import MotionTimeline
    from mks_wifi_bridge.observatory import create_app

    app = create_app(make_service(), timeline=MotionTimeline(tmp_path / "api.sqlite"))
    with TestClient(app) as client:
        for name in ["A", "B", "C"]:
            response = client.post(
                "/api/model-registry/actions", json={"operation": "create_model", "name": name}
            )
            assert response.status_code == 200
        first = client.get("/api/model-registry/models?limit=2").json()
        assert len(first["items"]) == 2 and first["next"]
        client.post(
            "/api/model-registry/actions", json={"operation": "create_model", "name": "New"}
        )
        second = client.get(
            "/api/model-registry/models", params={"limit": 2, "before": first["next"]}
        ).json()
        assert {row["name"] for row in second["items"]} == {"A"}
        assert client.get("/api/model-registry/models?before=bad").status_code == 422
        assert client.get("/api/model-registry/models?limit=0").status_code == 422
        assert [
            row["name"] for row in client.get("/api/model-registry/models?q=New").json()["items"]
        ] == ["New"]
        assert (
            client.post(
                "/api/model-registry/actions",
                json={"operation": "create_model", "name": "Bad"},
                headers={"Origin": "http://evil.example"},
            ).status_code
            == 403
        )
        for index in range(3):
            result = client.post(
                "/api/fabrication/ingest?filename=part" + str(index) + ".gcode",
                content=("G1 X" + str(index) + " E1").encode(),
                headers={"Content-Type": "application/octet-stream"},
            )
            assert result.status_code == 200
        page = client.get("/api/model-registry/artifacts?unassigned=true&limit=2").json()
        assert len(page["items"]) == 2 and page["next"]
        tail = client.get(
            "/api/model-registry/artifacts",
            params={"unassigned": True, "limit": 2, "before": page["next"]},
        ).json()
        assert len(tail["items"]) == 1
        assert not {row["sha256"] for row in page["items"]} & {
            row["sha256"] for row in tail["items"]
        }
        assert client.get("/api/fabrication/staging/queue").json()["items"] == []
