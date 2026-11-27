"""Archive invariants without physical printer activity."""

import sqlite3
from types import SimpleNamespace

import pytest

from mks_wifi_bridge.fabrication_archive import FabricationArchive


def archive(tmp_path):
    timeline = SimpleNamespace(path=str(tmp_path / "timeline.sqlite"))
    with sqlite3.connect(timeline.path) as db:
        db.executescript("""CREATE TABLE gcode_files(id TEXT,sha256 TEXT);
        CREATE TABLE print_sessions(id TEXT,filename TEXT,started_at TEXT,ended_at TEXT,
        state TEXT,progress REAL,file_id TEXT,source TEXT);""")
    return FabricationArchive(timeline)


def test_deduplication_source_removal_restart_and_independent_staging(tmp_path):
    store = archive(tmp_path)
    source = tmp_path / "part.gcode"
    source.write_text("G90\nM82\nG1 X0 Y0 Z.2\nG1 X10 E1\n")
    sha = store.ingest(source)
    assert store.ingest(source) == sha
    source.unlink()
    store = FabricationArchive(store.timeline)
    assert store.verified(sha).read_text().startswith("G90")
    assert len(store.collection("unprinted")["items"]) == 1
    with store.db() as db:
        db.execute("INSERT INTO gcode_files VALUES(?,?)", ("g", sha))
        for identity, state in [("one", "failed"), ("two", "cancelled")]:
            db.execute(
                "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
                (identity, "part.gcode", "2026-01-01", "2026-01-02", state, 20, "g", "observed"),
            )
    first = store.stage_again("one")
    second = store.stage_again("one")
    assert first["id"] != second["id"]
    assert first["physical_execution"] is False
    assert not store.collection("unprinted")["items"]
    page = store.collection("history", limit=1)
    following = store.collection("history", before=page["next"], limit=1)
    assert page["items"][0]["id"] != following["items"][0]["id"]
    assert {page["items"][0]["state"], following["items"][0]["state"]} == {"failed", "cancelled"}
    store.verified(sha).write_text("corrupt")
    with pytest.raises(ValueError, match="integrity"):
        store.stage_again("one")


def test_same_filename_is_not_identity_and_metadata_does_not_invent_geometry(tmp_path):
    store = archive(tmp_path)
    source = tmp_path / "same.gcode"
    source.write_text("; first")
    first = store.ingest(source)
    source.write_text("; second")
    assert store.ingest(source) != first
    with store.db() as db:
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            ("unknown", "same.gcode", "2026-01-01", None, "unknown", None, None, "observed"),
        )
    row = store.collection("history")["items"][0]
    assert row["sha256"] is None and not row["bytes_available"]
    with pytest.raises(ValueError):
        store.stage_again("unknown")


def test_execution_link_survives_geometry_cache_eviction(tmp_path):
    store = archive(tmp_path)
    source = tmp_path / "part.gcode"
    source.write_text("G1 X10 E1")
    sha = store.ingest(source)
    with store.db() as db:
        db.execute("INSERT INTO gcode_files VALUES(?,?)", ("g", sha))
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            ("historic", "part.gcode", "2000-01-01", "2000-01-02", "failed", 5, "g", "observed"),
        )
    store.collection("history")
    with store.db() as db:
        db.execute("UPDATE print_sessions SET file_id=NULL")
        db.execute("DELETE FROM gcode_files")
    assert store.collection("history")["items"][0]["sha256"] == sha
    assert store.stage_again("historic")["sha256"] == sha


def test_source_neutral_ingestion_deduplicates_bytes_and_preserves_provenance(tmp_path):
    from io import BytesIO

    from mks_wifi_bridge.fabrication_ingestion import ArtifactSubmission, FabricationIngestion

    store = archive(tmp_path)
    service = FabricationIngestion(store)
    content = b"G90\nG1 X10 E1\n"
    identities = []
    for provider, uri in [("removable", "/card/part.gcode"), ("wireless_handoff", "drop:42")]:
        submission = ArtifactSubmission("part.gcode", provider, uri)
        identities.append(service.submit(submission, BytesIO(content)))
        assert service.submit(submission, BytesIO(content)) == identities[-1]
    assert identities[0] == identities[1]
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM fabrication_artifacts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM artifact_provenance").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM artifact_stages").fetchone()[0] == 0
    assert len(list(store.root.glob("*.gcode"))) == 1
    with pytest.raises(ValueError):
        service.submit(
            ArtifactSubmission("part.step", "http", "upload:part.step", "design"), BytesIO(content)
        )
    assert not list(store.root.glob(".ingest-*"))


def test_http_ingestion_is_archive_only_and_enforces_origin(tmp_path):
    from fastapi.testclient import TestClient
    from test_observatory import make_service

    from mks_wifi_bridge.motion_timeline import MotionTimeline
    from mks_wifi_bridge.observatory import create_app

    app = create_app(make_service(), timeline=MotionTimeline(tmp_path / "api.sqlite"))
    with TestClient(app) as client:
        headers = {"Content-Type": "application/octet-stream"}
        result = client.post(
            "/api/fabrication/ingest?filename=part.gcode",
            content=b"G90\nG1 X10 E1",
            headers=headers,
        )
        assert result.status_code == 200
        assert result.json()["archived"] and not result.json()["printer_command_sent"]
        assert (
            client.post(
                "/api/fabrication/ingest?filename=part.gcode",
                content=b"G90",
                headers={**headers, "Origin": "http://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/fabrication/ingest?filename=../part.gcode", content=b"G90", headers=headers
            ).status_code
            == 422
        )
