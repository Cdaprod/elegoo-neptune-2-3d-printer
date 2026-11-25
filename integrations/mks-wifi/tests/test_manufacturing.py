# XML fixtures are intentionally literal for byte-preservation tests.
# ruff: noqa: E501
from __future__ import annotations

import json
from zipfile import ZipFile

import pytest
import yaml

from mks_wifi_bridge.manufacturing.controller import Controller
from mks_wifi_bridge.manufacturing.deployment import export_sd, upload
from mks_wifi_bridge.manufacturing.orca import CORE, PROD, OrcaProject
from mks_wifi_bridge.manufacturing.project import Manifest, load


def tetra(size=1):
    return "".join(
        "facet normal 0 0 1\nouter loop\n"
        + "".join(f"vertex {x * size} {y * size} {z * size}\n" for x, y, z in tri)
        + "endloop\nendfacet\n"
        for tri in [
            ((0, 0, 0), (0, 1, 0), (1, 0, 0)),
            ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
            ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
            ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ]
    )


def make_project(root):
    root.mkdir(parents=True)
    (root / "source.FCStd").write_text("CAD source")
    objects = [
        {
            "id": n,
            "export": n + ".stl",
            "cadObject": n,
            "meshPath": f"3D/{n}.model",
            "meshObjectId": str(i),
            "projectObjectId": str(i + 10),
        }
        for n, i in [("bracket", 1), ("crossbar", 2)]
    ]
    plates = [
        {"name": name, "objects": [obj], "output": f"PART0{i}.gcode"}
        for name, obj, i in zip(["Brackets", "Crossbar"], objects, [1, 2], strict=True)
    ]
    manifest = {
        "apiVersion": "cdaprod.dev/v1alpha1",
        "kind": "ManufacturingProject",
        "metadata": {"name": root.name, "repository": "example/mount"},
        "spec": {
            "source": {
                "engine": "freecad",
                "document": "source.FCStd",
                "parameters": {"Params.Access": 3},
            },
            "slicer": {"project": "source.3mf"},
            "plates": plates,
            "target": {"printer": "neptune-2"},
        },
    }
    (root / "manufacturing.yaml").write_text(yaml.safe_dump(manifest))
    settings = "<config>"
    components = ""
    build = ""
    for i, obj in enumerate(objects, 1):
        settings += f'''<object id="{i + 10}"><metadata key="support" value="preserve"/>
        <part id="{i}" subtype="normal_part"><metadata key="source_offset_x" value="0"/></part></object>
        <plate><metadata key="plater_id" value="{i}"/><metadata key="plater_name" value="{plates[i - 1]["name"]}"/>
        <metadata key="locked" value="true"/><model_instance><metadata key="object_id" value="{i + 10}"/>
        </model_instance></plate>'''
        components += f'<object id="{i + 10}"><components><component p:path="/{obj["meshPath"]}" objectid="{i}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>'
        build += f'<item objectid="{i + 10}" transform="1 0 0 0 1 0 0 0 1 30 40 0"/>'
    with ZipFile(root / "source.3mf", "w") as z:
        z.writestr("Metadata/model_settings.config", settings + "</config>")
        z.writestr("Metadata/project_settings.config", '{"wall_loops":3}')
        z.writestr("Metadata/plate_2.png", b"unchanged thumbnail")
        z.writestr(
            "3D/3dmodel.model",
            f'<model xmlns="{CORE}" xmlns:p="{PROD}"><resources>{components}</resources><build>{build}</build></model>',
        )
        for i, obj in enumerate(objects, 1):
            z.writestr(
                obj["meshPath"],
                f'<model xmlns="{CORE}"><resources><object id="{i}"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>',
            )
    return manifest


@pytest.fixture
def engine(tmp_path):
    projects = tmp_path / "projects"
    root = projects / "test-mount"
    make_project(root)
    calls = []

    def builder(root, manifest, output):
        report = {}
        for plate in manifest.spec.plates:
            for obj in plate.objects:
                size = (
                    manifest.spec.source.parameters["Params.Access"] if obj.id == "bracket" else 1
                )
                (output / obj.export).write_text(tetra(size))
                report[obj.id] = {"valid": True, "solids": 1}
        (output / "validation.json").write_text(json.dumps(report))

    def slicer(executable, project, index, output, profiles):
        calls.append(index)
        path = output / "plate.gcode"
        path.write_text(f";plate {index}\nG28\nG1 X10\n")
        return path

    return (
        Controller(
            tmp_path / "registry",
            projects,
            builder=builder,
            slicer=slicer,
            version=lambda exe: "OrcaSlicer-test",
        ),
        root,
        calls,
    )


def test_parameter_invalidation_reuses_unaffected_plate(engine):
    c, root, calls = engine
    a = c.execute(root.name, "Brackets", "build-slice")
    b = c.execute(root.name, "Crossbar", "slice-stage")
    assert calls == [1, 2]
    assert c.execute(root.name, "Brackets", "slice")["cached"]
    m = yaml.safe_load((root / "manufacturing.yaml").read_text())
    m["spec"]["source"]["parameters"]["Params.Access"] = 4
    (root / "manufacturing.yaml").write_text(yaml.safe_dump(m))
    with pytest.raises(ValueError, match="stale"):
        c.stage(a["artifact"])
    with pytest.raises(ValueError, match="rebuild"):
        c.execute(root.name, "Brackets", "slice")
    c.execute(root.name, "Brackets", "build-slice")
    reused = c.execute(root.name, "Crossbar", "slice-stage")
    assert calls == [1, 2, 1]
    assert reused["cached"]
    row = c.artifact(reused["artifact"])
    assert json.loads(row["receipt"])["reusedFrom"] == b["artifact"]
    assert row["staged"] and c.fresh(row)


def test_replacement_preserves_unaffected_archive_entries(engine, tmp_path):
    c, root, _ = engine
    _, m = c.project(root.name)
    geometry = c.build(root, m)
    source = OrcaProject(root / "source.3mf")
    out = tmp_path / "updated.3mf"
    assert source.reconcile(m.spec.plates[0], geometry, out) == ["3D/bracket.model"]
    with ZipFile(out) as z:
        for name, content in source.data.items():
            if name != "3D/bracket.model":
                assert z.read(name) == content
    assert OrcaProject(out).plate(m.spec.plates[1])[0] == 2


def test_painted_mesh_and_wrong_membership_fail_closed(engine, tmp_path):
    c, root, _ = engine
    _, m = c.project(root.name)
    geometry = c.build(root, m)
    project = OrcaProject(root / "source.3mf")
    project.data["3D/bracket.model"] = project.data["3D/bracket.model"].replace(
        b'v1="0"', b'paint_supports="1" v1="0"'
    )
    with pytest.raises(ValueError, match="painting"):
        project.reconcile(m.spec.plates[0], geometry, tmp_path / "bad.3mf")
    m.spec.plates[0].objects[0].projectObjectId = "12"
    with pytest.raises(ValueError, match="membership"):
        project.plate(m.spec.plates[0])


def test_tampered_artifact_never_stages(engine):
    c, root, _ = engine
    result = c.execute(root.name, "Brackets", "build-slice")
    row = c.artifact(result["artifact"])
    (c.storage / row["path"] / "PART01.gcode").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        c.stage(row["id"])


def test_schema_paths_and_auto_print(engine):
    _, root, _ = engine
    m = yaml.safe_load((root / "manufacturing.yaml").read_text())
    m["spec"]["deployment"] = {"autoPrint": True}
    with pytest.raises(ValueError):
        Manifest.model_validate(m)
    m["spec"].pop("deployment")
    m["spec"]["source"]["document"] = "../../outside.FCStd"
    (root / "manufacturing.yaml").write_text(yaml.safe_dump(m))
    with pytest.raises(ValueError, match="escapes"):
        load(root)


def test_jobs_record_failures_and_success(engine):
    c, root, _ = engine
    c.enqueue(root.name, "Brackets", "slice")
    assert c.work_once()
    assert c.jobs()[0]["status"] == "failed"
    c.enqueue(root.name, "Brackets", "build-slice")
    assert c.work_once()
    assert c.jobs()[0]["status"] == "succeeded"
    assert not c.work_once()


def test_sd_checks_mount_staging_and_existing_content(engine, tmp_path, monkeypatch):
    c, root, _ = engine
    result = c.execute(root.name, "Brackets", "build-slice")
    mount = tmp_path / "NEPTUNE"
    mount.mkdir()
    with pytest.raises(ValueError, match="staged"):
        export_sd(c, result["artifact"], mount)
    c.stage(result["artifact"])
    with pytest.raises(ValueError, match="mounted"):
        export_sd(c, result["artifact"], mount)
    monkeypatch.setattr("os.path.ismount", lambda p: True)
    (mount / "PART01.gcode").write_text("existing user file")
    with pytest.raises(ValueError, match="already exists"):
        export_sd(c, result["artifact"], mount)
    assert (mount / "PART01.gcode").read_text() == "existing user file"
    (mount / "PART01.gcode").unlink()
    assert export_sd(c, result["artifact"], mount)["status"] == "exported"
    assert export_sd(c, result["artifact"], mount)["status"] == "already_present"


def test_octoprint_upload_never_selects_or_prints(engine):
    c, root, _ = engine
    result = c.execute(root.name, "Brackets", "build-slice")
    c.stage(result["artifact"])

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://octoprint/api/files/local"
            assert b'name="print"\r\n\r\nfalse' in request.data
            assert b'name="select"\r\n\r\nfalse' in request.data
            return Response()

    class Backend:
        base_url = "http://octoprint"
        api_key = "test-secret"
        timeout = 3
        _opener = Opener()

    assert upload(c, result["artifact"], Backend())["print_started"] is False


def test_deployment_agent_caches_offline_and_invalidates_stale_upstream(engine, tmp_path):
    from mks_wifi_bridge.manufacturing.sync import snapshot, sync

    c, root, _ = engine
    result = c.execute(root.name, "Brackets", "build-slice")
    c.stage(result["artifact"])

    class Upstream:
        def request(self, path, body=None, binary=False):
            if path == "/projects":
                return c.discover()
            if path == "/artifacts":
                return c.list_artifacts()
            if path.endswith("/snapshot"):
                return snapshot(root, load(root))
            parts = path.split("/")
            row = c.artifact(parts[2])
            return (c.storage / row["path"] / parts[-1]).read_bytes()

    local_projects = tmp_path / "pi-projects"
    local_projects.mkdir()
    pi = Controller(tmp_path / "pi-registry", local_projects)
    assert sync(pi, Upstream()) == 1
    assert pi.verified(result["artifact"], staged=True)
    # Local Pi has all required inputs and artifact bytes after build node disappears.
    assert pi.list_artifacts()[0]["fresh"]
    m = yaml.safe_load((root / "manufacturing.yaml").read_text())
    m["spec"]["source"]["parameters"]["Params.Access"] = 4
    (root / "manufacturing.yaml").write_text(yaml.safe_dump(m))
    assert sync(pi, Upstream()) == 0
    with pytest.raises(ValueError, match="stale"):
        pi.verified(result["artifact"], staged=True)


def test_slicer_version_change_invalidates_previous_artifact(engine):
    c, root, _ = engine
    result = c.execute(root.name, "Brackets", "build-slice")
    c.stage(result["artifact"])
    c.version = lambda exe: "OrcaSlicer-new-version"
    c.execute(root.name, "Brackets", "slice")
    with pytest.raises(ValueError, match="stale"):
        c.verified(result["artifact"], staged=True)


def test_print_bay_api_uses_existing_auth_and_never_runs_jobs_inline(engine, monkeypatch):
    from fastapi.testclient import TestClient

    from mks_wifi_bridge.observatory import create_app

    c, root, calls = engine
    app = create_app(bind_host="0.0.0.0", auth_token="test-token")
    app.state.manufacturing = c
    client = TestClient(app)
    assert client.get("/api/manufacturing/projects").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert client.get("/api/manufacturing/projects", headers=headers).json()[0]["name"] == root.name
    response = client.post(
        f"/api/manufacturing/projects/{root.name}/jobs",
        headers=headers,
        json={"plate": "Brackets", "action": "build-slice"},
    )
    assert response.status_code == 200
    assert calls == []
    assert c.jobs()[0]["status"] == "queued"
    assert client.get("/print-bay").status_code == 200
    assert (
        client.get(f"/api/manufacturing/projects/{root.name}/snapshot", headers=headers).status_code
        == 200
    )
    assert (
        client.post(
            "/api/manufacturing/artifacts/unknown/deploy",
            headers=headers,
            json={"confirmation": "UPLOAD unknown"},
        ).status_code
        == 423
    )
    assert (
        client.post(
            f"/api/manufacturing/projects/{root.name}/jobs",
            headers={**headers, "Origin": "https://evil.example"},
            json={"plate": "Brackets", "action": "build"},
        ).status_code
        == 403
    )


def test_automatic_build_slice_stage_is_a_nonphysical_job(engine):
    c, root, calls = engine
    c.enqueue(root.name, "Brackets", "build-slice-stage")
    assert c.work_once()
    assert c.jobs()[0]["status"] == "succeeded"
    assert c.list_artifacts()[0]["staged"] == 1
    assert c.list_artifacts()[0]["receipt"]["previews"]["tft"] == "missing"
    assert calls == [1]


def test_geometry_preview_is_embedded_and_commands_are_preserved(engine):
    from mks_wifi_bridge.gcode_thumbnail import extract_gcode_thumbnail_details
    from mks_wifi_bridge.manufacturing.preview import embed_png

    c, root, _ = engine
    result = c.execute(root.name, "Brackets", "build-slice")
    row, path = c.verified(result["artifact"])
    gcode = path / "PART01.gcode"
    original = gcode.read_bytes()
    assert original.endswith(b";plate 1\nG28\nG1 X10\n")
    thumbnail = extract_gcode_thumbnail_details(original)
    assert thumbnail and thumbnail.width == 320 and thumbnail.height == 320
    embed_png(gcode, path / "preview.png")
    assert gcode.read_bytes() == original
