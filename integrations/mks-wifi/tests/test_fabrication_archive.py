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
    inode = store.verified(sha).stat().st_ino
    assert store.ingest(source) == sha
    assert store.verified(sha).stat().st_ino == inode
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
    for identity in ("one", "two"):
        store.link_execution(identity, sha, "operator_supplied", "unknown")
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
    store.link_execution("historic", sha, "operator_supplied", "unknown")
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


def test_expected_digest_rejection_never_publishes_artifact(tmp_path):
    from io import BytesIO

    from mks_wifi_bridge.fabrication_ingestion import ArtifactSubmission, FabricationIngestion

    store = archive(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        FabricationIngestion(store).submit(
            ArtifactSubmission("bad.gcode", "http", "upload:bad", expected_sha256="0" * 64),
            BytesIO(b"wrong contents"),
        )
    assert not list(store.root.glob("*.gcode"))
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM fabrication_artifacts").fetchone()[0] == 0


def test_printer_sd_discovery_never_matches_filename_and_size(tmp_path):
    store = archive(tmp_path)
    source = tmp_path / "PART.GCO"
    source.write_bytes(b"G1 X10 E1")
    sha = store.ingest(source, "removable_media")
    entry = {"name": "PART.GCO", "kind": "file", "size_bytes": source.stat().st_size}
    store.observe_discovery([entry])
    row = store.collection("discovered")["items"][0]
    assert row["sha256"] is None
    assert row["lifecycle"]["identity"] == "discovered_not_archived"
    assert row["lifecycle"]["execution"] == "not_started"
    store.observe_discovery(
        [{**entry, "verified_sha256": sha, "identity_evidence": "verified_content_snapshot"}]
    )
    row = store.collection("discovered")["items"][0]
    assert row["sha256"] == sha and row["lifecycle"]["archive"] == "archived"
    store.observe_discovery([], available=False)
    source.unlink()
    row = store.collection("discovered")["items"][0]
    assert row["lifecycle"]["discovery"] == "unavailable"
    assert row["lifecycle"]["archive"] == "archived"
    assert store.verified(sha).is_file()


def test_backup_restore_preserves_originals_and_execution_identity(tmp_path):
    from mks_wifi_bridge.archive_backup import backup, restore

    store = archive(tmp_path)
    source = tmp_path / "source.gcode"
    source.write_bytes(b"G90\nG1 X10 E1")
    sha = store.ingest(source)
    with store.db() as db:
        db.execute("INSERT INTO gcode_files VALUES(?,?)", ("g", sha))
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            ("old", "source.gcode", "2000", "2001", "failed", 10, "g", "printer_sd"),
        )
    store.link_execution("old", sha, "operator_supplied", "native_sd")
    store.collection("history")
    path = tmp_path / "backup.tar.gz"
    backup(store.timeline.path, path)
    source.unlink()
    target = tmp_path / "restored"
    restore(path, target)
    restored = FabricationArchive(type(store.timeline)(path=str(target / "ledger.sqlite")))
    assert restored.verified(sha).read_bytes() == b"G90\nG1 X10 E1"
    assert restored.collection("history")["items"][0]["id"] == "old"
    assert restored.collection("history")["items"][0]["state"] == "failed"
    with pytest.raises(ValueError):
        restore(path, target)


def test_legacy_filename_association_is_not_verified_execution_identity(tmp_path):
    store = archive(tmp_path)
    source = tmp_path / "same.gcode"
    source.write_bytes(b"G1 X20 E1")
    sha = store.ingest(source)
    with store.db() as db:
        db.execute("INSERT INTO gcode_files VALUES(?,?)", ("guessed_by_name", sha))
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            (
                "native",
                "same.gcode",
                "2000",
                "2001",
                "unknown",
                None,
                "guessed_by_name",
                "printer_sd",
            ),
        )
    row = store.collection("history")["items"][0]
    assert row["sha256"] is None
    assert row["lifecycle"]["execution"] == "unknown"
    with pytest.raises(ValueError):
        store.stage_again("native")
    store.link_execution("native", sha, "operator_supplied", "native_sd")
    row = store.collection("history")["items"][0]
    assert row["identity_evidence"] == "operator_supplied"
    assert row["execution_transport"] == "native_sd"


def test_print_bay_archive_delivery_is_confirmed_profile_checked_and_receive_only(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from test_observatory import make_service

    import mks_wifi_bridge.observatory as module
    from mks_wifi_bridge.motion_timeline import MotionTimeline

    monkeypatch.setenv("NEPTUNE_HMI_IMPORT_ENABLED", "true")
    calls = []

    async def get(endpoint):
        calls.append(endpoint)
        if endpoint == "/api/connection":
            return {"current": {"state": "Operational", "port": "/dev/ttyUSB-printer"}}
        assert endpoint == "/api/printerprofiles"
        return {
            "profiles": {
                "current": {
                    "current": True,
                    "volume": {
                        "origin": "lowerleft",
                        "formFactor": "rectangular",
                        "width": 220,
                        "depth": 220,
                        "height": 250,
                    },
                }
            }
        }

    backend = SimpleNamespace(get=get, base_url="http://octoprint", api_key="test-only")
    timeline = MotionTimeline(tmp_path / "bay.sqlite")
    store = FabricationArchive(timeline)
    source = tmp_path / "plate.gcode"
    source.write_bytes(b"G90\nM82\nG1 X0 Y0 Z0.2\nG1 X10 Y10 E1\n")
    sha = store.ingest(source)
    uploads = []

    def upload(base, key, name, data):
        uploads.append((name, data))
        return {"selected": False, "print_started": False}

    monkeypatch.setattr(module, "upload_local_gcode", upload)
    client = TestClient(module.create_app(make_service(backend=backend), timeline=timeline))
    stage = client.post(f"/api/fabrication/artifacts/{sha}/stage").json()
    endpoint = f"/api/fabrication/staging/{stage['id']}/deliver"
    assert client.post(endpoint, json={}).status_code == 422
    assert not uploads
    assert (
        client.post(f"/api/fabrication/artifacts/{sha}/stage?transport=native_sd").status_code
        == 423
    )
    confirmation = {"confirmation": "DELIVER " + stage["id"], "compatibility_acknowledged": True}
    result = client.post(endpoint, json=confirmation)
    assert result.status_code == 200 and result.json()["print_started"] is False
    assert len(uploads) == 1 and uploads[0][1] == source.read_bytes()
    assert calls == ["/api/connection", "/api/printerprofiles"]
    assert client.post(endpoint, json=confirmation).status_code == 409
    assert len(uploads) == 1


def test_quality_is_append_only_and_never_rewrites_machine_outcome(tmp_path):
    store = archive(tmp_path)
    with store.db() as db:
        db.execute(
            "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
            ("failed", "unknown.gcode", "2000", "2001", "failed", None, None, "printer_sd"),
        )
    first = store.assess_quality("failed", "accepted", "Usable despite interrupted final travel")
    second = store.assess_quality("failed", "rejected", "Later inspection found separation")
    assert first["id"] != second["id"]
    detail = store.execution_detail("failed")
    assert detail["execution"]["state"] == "failed"
    assert len(detail["quality_assessments"]) == 2
    assert all(row["source"] == "operator_supplied" for row in detail["quality_assessments"])
    assert detail["execution"]["sha256"] is None
