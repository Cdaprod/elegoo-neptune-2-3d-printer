"""No-printer tests for the touchscreen G-code library importer."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from mks_wifi_bridge import observatory as observatory_module
from mks_wifi_bridge.observatory import PrinterService, create_app
from mks_wifi_bridge.octoprint_backend import OctoPrintBackend
from mks_wifi_bridge.octoprint_local_import import (
    LibraryImportError,
    multipart,
    upload_local_gcode,
    validate_gcode_filename,
)


class FakeResponse(BytesIO):
    def __init__(self, body: bytes = b"", status: int = 200):
        super().__init__(body)
        self.status = status


class FakeOctoFiles:
    def __init__(self, files=None):
        self.files = files or []
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append(request)
        assert request.full_url == "http://octoprint:80/api/files/local"
        assert request.get_header("X-api-key") == "secret-key"
        if request.get_method() == "GET":
            return FakeResponse(json.dumps({"files": self.files}).encode())
        assert request.get_method() == "POST"
        assert b'name="select"' in request.data
        assert b'name="print"' in request.data
        assert request.data.count(b"false") == 2
        assert b"command" not in request.data
        return FakeResponse(b"{}", status=201)


def test_upload_local_only_with_no_select_or_print():
    fake = FakeOctoFiles()
    result = upload_local_gcode(
        "http://octoprint:80", "secret-key", "Part.GCO", b"G1 X1 E1\n", opener=fake
    )
    assert result["status"] == "imported"
    assert result["printing"] is False
    assert result["selected"] is False
    assert [request.get_method() for request in fake.requests] == ["GET", "POST"]


def test_identical_existing_file_is_not_uploaded_again():
    data = b"G1 X20 E2\n"
    fake = FakeOctoFiles([{"name": "TEST.gcode", "hash": hashlib.sha1(data).hexdigest()}])
    result = upload_local_gcode(
        "http://octoprint:80", "secret-key", "TEST.gcode", data, opener=fake
    )
    assert result["status"] == "already_imported"
    assert len(fake.requests) == 1


def test_changed_name_collision_is_content_addressed_not_overwritten():
    fake = FakeOctoFiles([{"name": "Part.gcode", "hash": "0" * 40}])
    result = upload_local_gcode(
        "http://octoprint:80", "secret-key", "Part.gcode", b"G1 X50 E2\n", opener=fake
    )
    assert result["status"] == "imported"
    assert result["filename"].startswith("Part-")
    assert b'filename="Part.gcode"' not in fake.requests[-1].data


def test_slicer_names_are_normalized_but_traversal_is_rejected():
    normalized = validate_gcode_filename("Bracket (final) [0.20].gcode", b"G1 E1")
    assert normalized.endswith(".gcode")
    assert "(" not in normalized
    with pytest.raises(LibraryImportError):
        validate_gcode_filename("../private.gcode", b"G1 E1")
    with pytest.raises(LibraryImportError):
        validate_gcode_filename("nested/file.gcode", b"G1 E1")
    with pytest.raises(LibraryImportError):
        validate_gcode_filename("command.txt", b"G1 E1")
    with pytest.raises(LibraryImportError):
        validate_gcode_filename("no-file.gco", b"")


def test_multipart_matches_octoprint_safe_storage_semantics():
    boundary, payload = multipart("Bench.gco", b"G1 X10 E1")
    assert boundary.encode() in payload
    assert b'name="select"\r\nContent-Type: text/plain\r\n\r\nfalse\r\n' in payload
    assert b'name="print"\r\nContent-Type: text/plain\r\n\r\nfalse\r\n' in payload


async def paused_poller(self, publish_snapshot):
    await self.stop_event.wait()


def test_hmi_endpoint_requires_auth_origin_and_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(PrinterService, "run_poller", paused_poller)
    backend = OctoPrintBackend("http://octoprint:80", "secret-key")
    service = PrinterService(backend=backend, interval=30)
    monkeypatch.setenv("NEPTUNE_HMI_IMPORT_ENABLED", "false")
    app = create_app(
        service, bind_host="0.0.0.0", auth_token="dashboard-key"
    )
    with TestClient(app) as client:
        assert client.get("/api/hmi/import-status").status_code == 401
        headers = {"Authorization": "Bearer dashboard-key"}
        assert client.get("/api/hmi/import-status", headers=headers).json()["available"] is False
        assert (
            client.post(
                "/api/hmi/import-gcode?filename=Part.gcode",
                headers={**headers, "Origin": "http://testserver", "Content-Type": "application/octet-stream"},
                content=b"G1 X1 E1",
            ).status_code
            == 423
        )

    monkeypatch.setenv("NEPTUNE_HMI_IMPORT_ENABLED", "true")
    app = create_app(
        PrinterService(backend=backend, interval=30),
        bind_host="0.0.0.0", auth_token="dashboard-key",
    )
    writes = []

    def fake_upload(base, key, filename, data):
        writes.append((base, key, filename, data))
        return {
            "status": "imported", "filename": filename, "selected": False, "printing": False
        }

    monkeypatch.setattr(observatory_module, "upload_local_gcode", fake_upload)
    headers = {
        "Authorization": "Bearer dashboard-key",
        "Content-Type": "application/octet-stream",
        "Origin": "http://testserver",
    }
    with TestClient(app) as client:
        assert client.get("/api/hmi/import-status", headers=headers).json()["available"] is True
        assert (
            client.post(
                "/api/hmi/import-gcode?filename=Part.gcode",
                headers={**headers, "Origin": "http://evil.example"},
                content=b"G1 X1 E1",
            ).status_code == 403
        )
        assert (
            client.post(
                "/api/hmi/import-gcode?filename=../bad.gcode",
                headers=headers, content=b"G1 X1 E1",
            ).status_code == 422
        )
        response = client.post(
            "/api/hmi/import-gcode?filename=Part.gcode", headers=headers, content=b"G1 X1 E1"
        )
        assert response.status_code == 200
        assert response.json()["printer_command_sent"] is False
        assert writes == [("http://octoprint:80", "secret-key", "Part.gcode", b"G1 X1 E1")]
