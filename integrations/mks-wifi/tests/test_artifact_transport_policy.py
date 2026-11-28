"""Typed admission and canonical custody without printer side effects."""

import hashlib
import io
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from mks_wifi_bridge.artifact_policy import transport_kind, validate_transport_content
from mks_wifi_bridge.completed_ingestion import CompletedDirectoryAdapter
from mks_wifi_bridge.engineering_preview import EngineeringPreview
from mks_wifi_bridge.fabrication_archive import FabricationArchive


def package(*names):
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        for name in names:
            archive.writestr(name, "<document/>")
    return result.getvalue()


STL = (
    b"solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\n"
    b"vertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid test\n"
)


@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("project.3mf", package("[Content_Types].xml", "3D/3dmodel.model"), "slicer_project"),
        ("mesh.stl", STL, "manufacturing_geometry"),
        ("mesh.obj", b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", "manufacturing_geometry"),
        ("part.step", b"ISO-10303-21;\nEND-ISO-10303-21;", "manufacturing_geometry"),
        ("part.stp", b"ISO-10303-21;\nEND-ISO-10303-21;", "manufacturing_geometry"),
        ("part.FCStd", package("Document.xml"), "design_freecad"),
        ("plate.gcode", b"G90\nG1 X1 E1\n", "execution_gcode"),
        ("plate.gco", b"G90\nG1 X1 E1\n", "execution_gcode"),
        ("plate.gc", b"G90\nG1 X1 E1\n", "execution_gcode"),
    ],
)
def test_transport_paths_share_canonical_bytes(tmp_path, name, data, kind):
    archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "ledger.sqlite")))
    completed = tmp_path / "completed"
    sha = hashlib.sha256(data).hexdigest()
    for transport in ("ssh", "ios-share"):
        directory = completed / transport
        directory.mkdir(parents=True)
        (directory / (sha + "_" + name)).write_bytes(data)
    adapter = CompletedDirectoryAdapter(archive, completed, settle_seconds=0)
    adapter.scan()
    adapter.scan()
    assert archive.verified(sha).read_bytes() == data
    with archive.db() as db:
        assert db.execute("SELECT count(*) FROM fabrication_artifacts").fetchone()[0] == 1
        rows = db.execute("SELECT * FROM artifact_provenance").fetchall()
        assert len(rows) == 2
        assert all(row["filename"] == name and row["artifact_kind"] == kind for row in rows)
        assert db.execute("SELECT count(*) FROM artifact_stages").fetchone()[0] == 0
    if name.endswith(".stl"):
        assert EngineeringPreview(archive).geometry(sha)["stats"]["rendered_triangles"] == 1


@pytest.mark.parametrize("name", ["macro.py", "script.FCMacro", "evil.exe", "../bad.stl"])
def test_unsupported_names(name):
    with pytest.raises(ValueError):
        transport_kind(name)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("fake.3mf", b"G90\n"),
        ("fake.stl", b"not a mesh"),
        ("fake.gcode", b"\0binary"),
        ("unsafe.FCStd", package("Document.xml", "../escape")),
    ],
)
def test_content_mismatch(tmp_path, name, data):
    source = tmp_path / "incoming.part"
    source.write_bytes(data)
    with pytest.raises(ValueError):
        validate_transport_content(source, name)
