"""Regression tests: OctoPrint is the only printer I/O owner in OctoPrint mode."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mks_wifi_bridge.motion_timeline import MotionTimeline
from mks_wifi_bridge.observatory import PrinterService, create_app
from mks_wifi_bridge.octoprint_backend import OctoPrintBackend, OctoPrintError


class StubBackend(OctoPrintBackend):
    def __init__(self) -> None:
        super().__init__("http://octoprint:80", "test-secret")
        self.calls: list[str] = []
        self.disconnected = False

    async def get(self, endpoint: str) -> dict[str, Any]:
        self.calls.append(endpoint)
        if self.disconnected:
            raise OctoPrintError("OctoPrint disconnected")
        if endpoint == "/api/connection":
            return {"current": {"state": "Operational", "port": "remote://mks:8080"}}
        if endpoint == "/api/printer":
            return {
                "temperature": {
                    "tool0": {"actual": 24.0, "target": 0.0},
                    "bed": {"actual": 23.0, "target": 0.0},
                },
                "state": {"text": "Operational", "flags": {"operational": True}},
            }
        if endpoint == "/api/job":
            return {
                "job": {"file": {"name": "part.gcode", "size": 1234}},
                "progress": {"completion": 0.0, "printTime": 0, "printTimeLeft": None},
            }
        if endpoint == "/api/files":
            return {
                "files": [
                    {"name": "part.gcode", "type": "machinecode", "size": 1234, "origin": "local"},
                    {"name": "sd.gco", "type": "machinecode", "origin": "sdcard"},
                ]
            }
        raise AssertionError(f"Unexpected endpoint {endpoint}")


def forbidden_mks(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("OctoPrint mode opened a forbidden native MKS connection")


def make_service(backend: StubBackend) -> PrinterService:
    return PrinterService(
        host=backend.host,
        port=backend.port,
        backend=backend,
        client_factory=forbidden_mks,
        sd_reader=forbidden_mks,
        motion_query=forbidden_mks,
        motion_enabled=True,
        motion_query_enabled=True,
        interval=30.0,
    )


def test_backend_reads_realistic_octoprint_status_without_mks_tcp() -> None:
    backend = StubBackend()
    service = make_service(backend)
    result = asyncio.run(service.poll_once())
    assert result["connection"]["connected"] is True
    assert result["connection"]["transport"] == "OctoPrint API"
    assert result["telemetry"]["nozzle_temperature"]["value"] == 24.0
    assert result["telemetry"]["bed_temperature"]["value"] == 23.0
    assert result["telemetry"]["print_active"]["value"] is False
    assert result["telemetry"]["current_file"]["value"] == "part.gcode"
    assert backend.calls == ["/api/connection", "/api/printer", "/api/job"]


def test_octoprint_file_catalog_has_correct_provenance() -> None:
    backend = StubBackend()
    service = make_service(backend)
    result = asyncio.run(service.refresh_sd())
    assert result["source"] == "octoprint_local"
    assert result["entries"] == [
        {
            "name": "part.gcode",
            "kind": "file",
            "size_bytes": 1234,
            "source": "octoprint_local",
        }
    ]
    assert backend.calls == ["/api/files"]


def test_octoprint_failures_are_stale_and_never_fallback_to_mks() -> None:
    backend = StubBackend()
    service = make_service(backend)

    async def check() -> None:
        await service.poll_once()
        backend.disconnected = True
        failed = await service.poll_once()
        assert failed["connection"]["connected"] is False
        assert failed["stale"] is True
        assert failed["last_error"] == "OctoPrint disconnected"
        assert failed["telemetry"]["nozzle_temperature"]["value"] == 24.0

    asyncio.run(check())


def test_dashboard_exposes_octoprint_mode_and_blocks_direct_controls(tmp_path: Any) -> None:
    backend = StubBackend()
    service = make_service(backend)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="test-only-observatory-secret",
        control_enabled=True,
        timeline=MotionTimeline(tmp_path / "octoprint.sqlite3"),
    )
    headers = {"Authorization": "Bearer test-only-observatory-secret"}
    with TestClient(app) as client:
        response = client.get("/api/printer/status", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["printer_backend"] == "octoprint"
        assert payload["connection"]["transport"] == "OctoPrint API"
        assert payload["print_job"]["source"] == "OctoPrint API telemetry"
        catalog = client.get("/api/control/catalog", headers=headers).json()
        assert catalog["execution_enabled"] is False
        listing = client.get(
            "/api/printer/sdcard", params={"refresh": "true"}, headers=headers
        ).json()
        assert listing["printer_listing_source"] == "octoprint_local"
        arm = client.post(
            "/api/control/arm",
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
            headers=headers,
        )
        assert arm.status_code == 423
        execute = client.post("/api/control/execute", json={"macro_id": "home_x"}, headers=headers)
        assert execute.status_code == 423
        upload = client.post(
            "/api/printer/sdcard/upload",
            params={"filename": "test.gcode"},
            headers=headers,
        )
        assert upload.status_code == 423
        workflow = client.post(
            "/api/control/workflows",
            json={"macro_id": "filament_change_guided"},
            headers=headers,
        )
        assert workflow.status_code == 423


def test_octoprint_controls_are_opt_in_and_never_use_mks(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = StubBackend()
    operations: list[str] = []

    async def fake_execute(operation: str) -> dict[str, Any]:
        operations.append(operation)
        return {"accepted": True, "operation": operation}

    backend.execute_control = fake_execute  # type: ignore[method-assign]
    service = make_service(backend)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="test-only-observatory-secret",
        control_enabled=True,
        timeline=MotionTimeline(tmp_path / "controls.sqlite3"),
    )
    headers = {"Authorization": "Bearer test-only-observatory-secret"}
    payload = {
        "operation": "home_x",
        "confirmation": "CONFIRM HOME_X",
        "attended": True,
        "power_switch_reachable": True,
        "travel_path_clear": True,
    }
    with TestClient(app) as client:
        assert (
            client.post("/api/octoprint/control", json=payload, headers=headers).status_code == 423
        )
        monkeypatch.setenv("NEPTUNE_OCTOPRINT_CONTROLS_ENABLED", "true")
        # TestClient startup polls the stub, so telemetry is already fresh.
        response = client.post("/api/octoprint/control", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["verified_complete"] is False
        assert operations == ["home_x"]
        payload["confirmation"] = "wrong"
        assert (
            client.post("/api/octoprint/control", json=payload, headers=headers).status_code == 422
        )
        payload["confirmation"] = "CONFIRM HOME_X"
        payload["operation"] = "cancel"
        payload["confirmation"] = "CONFIRM CANCEL"
        assert (
            client.post("/api/octoprint/control", json=payload, headers=headers).status_code == 409
        )
        assert operations == ["home_x"]


def test_octoprint_command_adapter_rejects_unknown_commands() -> None:
    backend = OctoPrintBackend("http://octoprint", "fixture")
    with pytest.raises(ValueError):
        backend._post_sync("/api/printer/command", "M112")


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("http://octoprint:80/other", "abc"),
        ("http://admin:pass@octoprint:80", "abc"),
        ("ftp://octoprint", "abc"),
        ("http://octoprint:80", ""),
        ("http://octoprint:80?token=bad", "abc"),
    ],
)
def test_octoprint_rejects_unsafe_url_and_missing_key(url: str, key: str) -> None:
    with pytest.raises(ValueError):
        OctoPrintBackend(url, key)


def test_octoprint_rejects_arbitrary_api_paths() -> None:
    backend = OctoPrintBackend("http://octoprint", "fixture")
    with pytest.raises(ValueError):
        asyncio.run(backend.get("/api/printer/command"))


def test_octoprint_print_job_history_normalizes_plugin_records() -> None:
    backend = OctoPrintBackend("http://octoprint", "fixture")
    calls: list[str] = []

    def fake_get(endpoint: str) -> dict[str, Any]:
        calls.append(endpoint)
        return {
            "totalItemCount": 2,
            "allPrintJobs": [
                {
                    "databaseId": 42,
                    "fileName": "benchy.gcode",
                    "printStatusResult": "success",
                    "printStartDateTimeFormatted": "11.11.2026 12:30",
                    "printEndDateTimeFormatted": "11.11.2026 13:45",
                    "duration": 4500,
                    "filamentModels": {
                        "total": {"material": "PLA", "usedWeight": "15.50"}
                    },
                },
                {
                    "databaseId": 43,
                    "fileName": "bracket.gcode",
                    "printStatusResult": "cancel",
                    "printStartDateTimeFormatted": "10.11.2026 09:00",
                    "duration": 180,
                },
            ],
        }

    backend._get_sync = fake_get  # type: ignore[method-assign]
    result = asyncio.run(backend.print_job_history(100))
    assert len(calls) == 1
    assert calls[0].startswith("/plugin/PrintJobHistory/loadPrintJobHistoryByQuery?")
    assert result["total"] == 2
    assert result["sessions"][0]["outcome"] == "reported_completed"
    assert result["sessions"][0]["outcome_verified"] is False
    assert result["sessions"][0]["material"] == "PLA"
    assert result["sessions"][1]["outcome"] == "reported_cancelled"
    with pytest.raises(ValueError):
        asyncio.run(backend.print_job_history(501))


def test_octoprint_history_endpoint_is_authenticated_and_read_only(tmp_path: Any) -> None:
    backend = StubBackend()

    async def fake_history(limit: int) -> dict[str, Any]:
        return {"sessions": [], "total": 0, "returned": 0, "limit": limit}

    backend.print_job_history = fake_history  # type: ignore[method-assign]
    app = create_app(
        make_service(backend),
        bind_host="0.0.0.0",
        auth_token="test-only-observatory-secret",
        timeline=MotionTimeline(tmp_path / "history.sqlite3"),
    )
    with TestClient(app) as client:
        assert client.get("/api/print/history/octoprint").status_code == 401
        headers = {"Authorization": "Bearer test-only-observatory-secret"}
        result = client.get("/api/print/history/octoprint", headers=headers)
        assert result.status_code == 200
        assert result.json()["total"] == 0
        assert client.get(
            "/api/print/history/octoprint?limit=501", headers=headers
        ).status_code == 422
