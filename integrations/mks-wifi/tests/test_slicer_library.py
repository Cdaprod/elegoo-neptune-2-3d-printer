"""Package bodies/instances, provenance and idempotent canonical registration."""

import io
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from mks_wifi_bridge.fabrication_archive import FabricationArchive
from mks_wifi_bridge.fabrication_ingestion import ArtifactSubmission, FabricationIngestion
from mks_wifi_bridge.model_registry import ModelRegistry
from mks_wifi_bridge.slicer_library import SlicerLibrary, extract_project


def project(path, cycle=False):
    component = "2" if cycle else "1"
    raw = f'''<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
unit="millimeter">
<resources>
<object id="1" name="Real part">
<mesh>
<vertices>
<vertex x="0" y="0" z="0"/>
<vertex x="10" y="0" z="0"/>
<vertex x="0" y="10" z="0"/>
</vertices>
<triangles>
<triangle v1="0" v2="1" v3="2"/>
</triangles>
</mesh>
</object>
<object id="2">
<components>
<component objectid="{component}" transform="1 0 0 0 1 0 0 0 1 20 0 0"/>
</components>
</object>
</resources>
<build>
<item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 30 0"/>
<item objectid="1"/>
</build>
</model>'''
    with ZipFile(path, "w") as package:
        package.writestr("3D/3dmodel.model", raw)
        package.writestr(
            "Metadata/project_settings.config",
            json.dumps({"layer_height": "0.2", "printhost_apikey": "private"}),
        )


def test_bodies_instances_transform_and_no_required_slicer_thumbnail(tmp_path):
    source = tmp_path / "project.3mf"
    project(source)
    result = extract_project(source, tmp_path / "output")
    assert len(result["bodies"]) == 1 and len(result["build"]) == 2
    assert len(result["bodies"][0]["instances"]) == 2
    assert result["embedded_previews"] == [] and result["settings"] == {"layer_height": "0.2"}
    assert result["build"][0]["bodies"][0]["transforms"][0].endswith("0 30 0")
    assert result["build"][0]["bodies"][0]["transforms"][1].endswith("20 0 0")
    from mks_wifi_bridge.manufacturing.orca import stl_mesh

    points, _ = stl_mesh(tmp_path / "output" / result["plates"][0]["geometry_file"])
    assert (30.0, 30.0, 0.0) in points


def test_original_and_body_registration_is_atomic_idempotent_candidate_only(tmp_path):
    source = tmp_path / "project.3mf"
    project(source)
    result = extract_project(source, tmp_path / "output")
    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "registry.sqlite")))
    registry = ModelRegistry(archive)
    service = FabricationIngestion(archive)
    sha = service.submit(
        ArtifactSubmission(source.name, "orca_slicer", "source", artifact_kind="slicer_project"),
        io.BytesIO(source.read_bytes()),
    )
    for body in result["bodies"]:
        body["sha256"] = service.submit(
            ArtifactSubmission(
                "body.stl", "orca_slicer", "derived", artifact_kind="manufacturing_geometry"
            ),
            io.BytesIO((tmp_path / "output" / body["geometry_file"]).read_bytes()),
        )
    library = SlicerLibrary(archive)
    first = library.register(sha, result)
    second = library.register(sha, result)
    assert first == second and len(registry.library()) == 1
    assert registry.library()[0]["identity_state"] == "candidate"
    manifest = registry.manifest(first["bodies"][0]["revision_id"])
    assert manifest["authorities"] == [] and len(manifest["representations"]) == 2
    assert library.projects()[0]["body_count"] == 1
    assert archive.stages() == []
    with archive.db() as db:
        assert db.execute("SELECT COUNT(*) FROM artifact_relationships").fetchone()[0] == 1
    bad = {**result, "bodies": [{**result["bodies"][0], "object_id": "missing"}]}
    with pytest.raises(ValueError):
        library.register(sha, bad)
    assert len(registry.library()) == 1


def test_cycle_and_unsafe_package_are_rejected(tmp_path):
    source = tmp_path / "cycle.3mf"
    project(source, cycle=True)
    with pytest.raises(ValueError, match="Cyclic"):
        extract_project(source, tmp_path / "cycle")
    with ZipFile(source, "w") as package:
        package.writestr("../escape.model", "<model/>")
    with pytest.raises(ValueError, match="Unsafe"):
        extract_project(source, tmp_path / "unsafe")
