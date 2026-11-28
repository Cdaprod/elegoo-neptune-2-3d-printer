"""Actual multipart intake, scoped credentials and non-actuating compatibility."""

import hashlib

from fastapi.testclient import TestClient
from test_observatory import make_service

from mks_wifi_bridge.motion_timeline import MotionTimeline
from mks_wifi_bridge.observatory import create_app


def test_slicer_upload_is_content_addressed_and_never_a_printer_action(tmp_path, monkeypatch):
    monkeypatch.setenv("NEPTUNE_INGESTION_TOKEN", "slicer-scoped-test-token")
    app = create_app(make_service(), timeline=MotionTimeline(tmp_path / "intake.sqlite"))
    with TestClient(app) as client:
        assert client.get("/api/version").status_code == 401
        headers = {"X-Api-Key": "slicer-scoped-test-token"}
        version = client.get("/api/version", headers=headers).json()
        assert version["api"] == "0.1" and "text" not in version
        raw = b"G90\nG1 X10 Y10 E1\n"

        def upload(**fields):
            return client.post(
                "/api/files/local",
                headers=headers,
                files={"file": ("original.gcode", raw, "application/octet-stream")},
                data=fields,
            )

        first = upload(print="false", path="")
        assert first.status_code == 201
        body = first.json()
        sha = hashlib.sha256(raw).hexdigest()
        assert (
            body["sha256"] == sha
            and body["archived"]
            and not body["selected"]
            and not body["staged"]
            and not body["printing"]
        )
        assert upload(print="false").json()["sha256"] == sha
        assert upload(print="true").status_code == 423
        assert upload(select="true").status_code == 423
        assert upload(path="../outside").status_code == 422
        assert upload(plateindex="0").status_code == 422
        assert (
            client.post(
                "/api/files/local",
                headers={**headers, "X-Content-SHA256": "0" * 64},
                files={"file": ("changed.gcode", raw)},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/files/local",
                headers={**headers, "Content-Length": "1"},
                files={"file": ("oversized.gcode", raw)},
            ).status_code
            == 413
        )
        assert (
            client.get(
                "/api/version", headers={**headers, "Origin": "http://unrelated.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/fabrication/staging/queue").json()["items"] == []
        # Slicer credentials cannot access operator history or control interfaces.
        remote = TestClient(app, client=("198.51.100.8", 5000))
        assert remote.get("/api/fabrication/history", headers=headers).status_code == 401


def test_slicer_intake_disabled_without_scoped_token(tmp_path, monkeypatch):
    monkeypatch.delenv("NEPTUNE_INGESTION_TOKEN", raising=False)
    app = create_app(make_service(), timeline=MotionTimeline(tmp_path / "disabled.sqlite"))
    with TestClient(app) as client:
        assert client.get("/api/version").status_code == 503
