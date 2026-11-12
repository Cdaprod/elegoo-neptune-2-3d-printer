from __future__ import annotations

import asyncio
import base64
import sqlite3
import time
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mkswifi import PrinterState, PrinterStatus, Temperature

from mks_wifi_bridge.executor import CommandExecutor
from mks_wifi_bridge.local_gcode_index import LocalGcodeIndexer
from mks_wifi_bridge.motion_timeline import MotionTimeline
from mks_wifi_bridge.observatory import PrinterService, create_app, utc_now


class FakeClient:
    calls = 0
    active = 0
    max_active = 0
    fail = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def get_status(self) -> PrinterStatus:
        type(self).calls += 1
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            await asyncio.sleep(0.001)
            if type(self).fail:
                from mkswifi import MksWifiConnectionError

                raise MksWifiConnectionError("mock connection lost")
            return PrinterStatus(
                state=PrinterState.IDLE,
                nozzles={0: Temperature(24.0, 0.0)},
                bed=Temperature(23.0, 0.0),
                progress=0,
                elapsed=timedelta(seconds=0),
            )
        finally:
            type(self).active -= 1

    async def get_firmware(self) -> str:
        await asyncio.sleep(0.001)
        return "TFT28/32"


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeClient.calls = 0
    FakeClient.active = 0
    FakeClient.max_active = 0
    FakeClient.fail = False


def make_service(**kwargs: Any) -> PrinterService:
    options = {
        "host": "127.0.0.1",
        "port": 8080,
        "interval": 0.03,
        "timeout": 0.1,
        "client_factory": FakeClient,
    }
    options.update(kwargs)
    return PrinterService(**options)


def wait_for_success(service: PrinterService) -> None:
    deadline = time.monotonic() + 1
    while not service.connected and time.monotonic() < deadline:
        time.sleep(0.005)
    assert service.connected


def test_polling_worker_caches_real_client_result_and_stops() -> None:
    service = make_service(interval=30.0)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        response = client.get("/api/printer/status")
        assert response.status_code == 200
        body = response.json()
        assert body["connection"]["connected"] is True
        assert body["telemetry"]["nozzle_temperature"]["value"] == 24.0
        assert body["firmware_identification"] == "TFT28/32"
        assert body["print_job"]["status"] == "idle"
        assert body["print_job"]["active"] is False
        dashboard = client.get("/").text
        assert 'aria-label="Print job tracking"' in dashboard
        assert 'id="overview-job-history"' in dashboard
        assert 'id="sd-local-index-pick"' in dashboard
        assert "current MKS interface exposes filenames, not G-code contents" in dashboard
        assert client.get("/health").json()["poller_running"] is True
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        poll_count = FakeClient.calls
        for _ in range(20):
            assert client.get("/api/printer/status").status_code == 200
        assert FakeClient.calls == poll_count
    assert service.worker_task is None
    assert FakeClient.calls >= 1


def test_gcode_timeline_api_indexes_analysis_without_printer_upload(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    service = make_service(interval=30.0)
    app = create_app(service, timeline=timeline)
    with TestClient(app) as client:
        response = client.post(
            "/api/timeline/gcode", json={"filename": "Path.gcode", "content": "G90\nG1 X12 F600\n"}
        )
        assert response.status_code == 200
        assert response.json()["segment_count"] == 1
        status = client.get("/api/timeline")
        assert status.status_code == 200
        assert status.json()["gcode_files"][0]["filename"] == "Path.gcode"
        assert status.json()["planned_history"][0]["source"] == "uploaded_gcode_plan"


def test_offline_motion_demo_endpoint_is_simulated_and_not_indexed(tmp_path) -> None:
    service = make_service(interval=30.0)
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    app = create_app(service, timeline=timeline)
    with TestClient(app) as client:
        wait_for_success(service)
        before_calls = FakeClient.calls
        response = client.get("/api/timeline/offline-demo")
        assert response.status_code == 200
        demo = response.json()
        assert demo["source"] == "simulated_offline_fixture"
        assert any(row["feedrate_mm_min"] > 0 for row in demo["feedrate_history"])
        assert any(row["feedrate_mm_min"] == 0 for row in demo["feedrate_history"])
        assert timeline.snapshot()["gcode_files"] == []
        assert FakeClient.calls == before_calls


def test_full_history_storage_returns_actionable_507_without_crashing_service(
    tmp_path, monkeypatch
) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")

    def fail_write(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(timeline, "store_gcode", fail_write)
    app = create_app(make_service(interval=30.0), timeline=timeline)
    with TestClient(app) as client:
        response = client.post(
            "/api/timeline/gcode",
            json={"filename": "analysis.gcode", "content": "G1 X1 F60"},
        )
        assert response.status_code == 507
        assert "Live telemetry remains available" in response.json()["detail"]
        assert response.json()["history_storage"]["status"] == "non_persistent"
        assert client.get("/health").status_code == 200


def test_sd_upload_endpoint_is_visible_but_default_off_without_printer_io() -> None:
    service = make_service(interval=30.0)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        status = client.get("/api/printer/sdcard/upload")
        assert status.status_code == 200
        assert status.json()["supported_by_source"] is True
        assert status.json()["configured"] is False
        response = client.post(
            "/api/printer/sdcard/upload?filename=demo.gcode",
            content=b"G1 X10 F600\n",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 423
        assert "No file was sent" in response.json()["detail"]
        assert FakeClient.calls == 1


def test_motion_endpoint_is_read_only_and_disabled_by_default() -> None:
    service = make_service(interval=30.0)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        motion = client.get("/api/printer/motion").json()
        status = client.get("/api/printer/status").json()
        assert motion["enabled"] is False
        assert motion["status"] == "disabled"
        assert motion["latest"] is None
        assert status["motion"]["enabled"] is False
        assert status["motion"]["operation_events"] == []
        assert status["motion"]["operator_reference"]["axes"] == {}
        assert status["command_activity"]["operations"] == []
        assert FakeClient.calls == 1


def test_external_homing_reference_api_is_authenticated_and_sends_no_command() -> None:
    executor = CommandExecutor()
    app = create_app(
        make_service(interval=30.0),
        bind_host="0.0.0.0",
        auth_token="dashboard-secret",
        executor=executor,
    )
    payload = {
        "axes": ["x", "y", "z"],
        "confirmation": "RECORD TFT-HOMED AXES",
        "attended": True,
        "no_competing_clients": True,
        "power_switch_reachable": True,
    }
    with TestClient(app) as client:
        wait_for_success(app.state.printer)
        denied = client.post("/api/control/references/home", json=payload)
        assert denied.status_code == 401
        response = client.post(
            "/api/control/references/home",
            json=payload,
            headers={"Authorization": "Bearer dashboard-secret"},
        )
        assert response.status_code == 200
        assert response.json()["commands_sent"] == []
        assert response.json()["axes"] == ["x", "y", "z"]
        state = client.get(
            "/api/control/state", headers={"Authorization": "Bearer dashboard-secret"}
        ).json()
        assert all(state["control"]["axes"][axis]["homed"] for axis in "xyz")
        assert all(
            state["control"]["axes"][axis]["position_source"]
            == "operator_reported_external_homing_reference"
            for axis in "xyz"
        )


def test_one_shot_motion_query_is_disabled_by_default_without_transport_call() -> None:
    calls = 0

    async def position_query(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "available", "axes": {}, "raw_lines": []}

    service = make_service(interval=30, motion_query=position_query)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        response = client.post("/api/printer/motion/query", json={"confirmation": "M114"})
        assert response.status_code == 423
        assert "No command was sent" in response.json()["detail"]
        assert calls == 0


def test_one_shot_motion_query_requires_exact_confirmation_and_fresh_idle_state() -> None:
    calls: list[tuple[str, int, float]] = []

    async def position_query(host: str, port: int, timeout: float) -> dict[str, Any]:
        calls.append((host, port, timeout))
        return {
            "status": "available",
            "latency_ms": 5,
            "axes": {
                axis: {"status": "available", "value_mm": value}
                for axis, value in zip("xyze", (10, 20, 3, 4), strict=True)
            },
            "raw_lines": ["X:10 Y:20 Z:3 E:4"],
        }

    service = make_service(interval=30, motion_query_enabled=True, motion_query=position_query)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        wrong = client.post("/api/printer/motion/query", json={"confirmation": "M114!"})
        assert wrong.status_code == 422
        accepted = client.post("/api/printer/motion/query", json={"confirmation": "M114"})
        assert accepted.status_code == 200
        assert accepted.json()["sample"]["axes"]["x"]["value_mm"] == 10
        assert accepted.json()["motion"]["latest"]["source_command"] == "M114"
        assert accepted.json()["motion"]["query_enabled"] is False
        assert accepted.json()["motion"]["polling_enabled"] is False
        assert accepted.json()["motion"]["latest"]["axes"]["x"]["value_mm"] == 10
        assert service.motion_payload()["latest"]["axes"]["x"]["value_mm"] == 10
        assert calls and calls[0][0:2] == ("127.0.0.1", 8080)
        limited = client.post("/api/printer/motion/query", json={"confirmation": "M114"})
        assert limited.status_code == 423
        assert len(calls) == 1


def test_one_shot_motion_timeout_consumes_gate_without_retry() -> None:
    calls = 0

    async def position_query(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "unavailable", "axes": {}, "raw_lines": [], "error": "timeout"}

    # The one-shot gate must not race the fast 30 ms background poller.
    service = make_service(interval=30, motion_query_enabled=True, motion_query=position_query)
    with TestClient(create_app(service)) as client:
        wait_for_success(service)
        response = client.post("/api/printer/motion/query", json={"confirmation": "M114"})
        assert response.status_code == 200
        assert response.json()["sample"]["status"] == "unavailable"
        assert response.json()["motion"]["query_enabled"] is False
        retry = client.post("/api/printer/motion/query", json={"confirmation": "M114"})
        assert retry.status_code == 423
        assert calls == 1


def test_one_shot_motion_query_refuses_printing_state() -> None:
    class PrintingClient(FakeClient):
        async def get_status(self) -> PrinterStatus:
            status = await super().get_status()
            return PrinterStatus(
                state=PrinterState.PRINTING,
                nozzles=status.nozzles,
                bed=status.bed,
                progress=20,
            )

    calls = 0

    async def position_query(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "available", "axes": {}, "raw_lines": []}

    service = make_service(
        client_factory=PrintingClient,
        motion_query_enabled=True,
        motion_query=position_query,
    )
    with TestClient(create_app(service)) as client:
        wait_for_success(service)
        response = client.post("/api/printer/motion/query", json={"confirmation": "M114"})
        assert response.status_code == 409
        assert calls == 0


def test_motion_query_uses_shared_poller_and_is_rate_limited() -> None:
    queries: list[tuple[str, int, float]] = []

    async def position_query(host: str, port: int, timeout: float) -> dict[str, Any]:
        assert FakeClient.active == 0  # telemetry connection is closed first
        queries.append((host, port, timeout))
        return {
            "status": "available",
            "latency_ms": 4.2,
            "axes": {
                axis: {"status": "available", "value_mm": value}
                for axis, value in zip("xyze", (1, 2, 3, 4), strict=True)
            },
            "raw_lines": ["X:1 Y:2 Z:3 E:4"],
        }

    service = make_service(
        interval=30,
        motion_enabled=True,
        motion_interval=5,
        motion_query=position_query,
    )
    asyncio.run(service.poll_once())
    asyncio.run(service.poll_once())
    assert len(queries) == 1
    assert queries[0][0:2] == ("127.0.0.1", 8080)
    assert service.motion_latest["source_command"] == "M114"
    assert service.motion_latest["axes"]["z"]["value_mm"] == 3
    assert service.motion_latest["measurement_confidence"].startswith("firmware_reported")


def test_motion_polling_skips_non_idle_printer_state() -> None:
    calls = 0

    class PrintingClient(FakeClient):
        async def get_status(self) -> PrinterStatus:
            status = await super().get_status()
            return PrinterStatus(
                state=PrinterState.PRINTING,
                nozzles=status.nozzles,
                bed=status.bed,
                progress=20,
            )

    async def position_query(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "available", "axes": {}}

    service = make_service(
        client_factory=PrintingClient,
        motion_enabled=True,
        motion_interval=5,
        motion_query=position_query,
    )
    snapshot = asyncio.run(service.poll_once())
    assert calls == 0
    assert snapshot["motion"]["status"] == "skipped_non_idle_or_unknown"


def test_motion_query_and_telemetry_poll_remain_serialized() -> None:
    queries = 0

    async def position_query(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal queries
        queries += 1
        await asyncio.sleep(0.002)
        return {
            "status": "available",
            "axes": {axis: {"status": "available", "value_mm": 0.0} for axis in "xyze"},
        }

    service = make_service(
        motion_enabled=True,
        motion_interval=5,
        motion_query=position_query,
    )

    async def poll_concurrently() -> None:
        await asyncio.gather(service.poll_once(), service.poll_once())

    asyncio.run(poll_concurrently())
    assert queries == 1
    assert FakeClient.max_active == 1


def test_failed_poll_marks_connection_stale_and_keeps_last_values() -> None:
    service = make_service()
    asyncio.run(service.poll_once())
    assert service.telemetry is not None
    FakeClient.fail = True
    snapshot = asyncio.run(service.poll_once())
    assert snapshot["connection"]["connected"] is False
    assert snapshot["stale"] is True
    assert snapshot["last_error"] == "mock connection lost"
    assert snapshot["telemetry"]["nozzle_temperature"]["value"] == 24.0


def test_printer_io_is_serialized() -> None:
    service = make_service()

    async def run_concurrently() -> None:
        await asyncio.gather(service.poll_once(), service.poll_once())

    asyncio.run(run_concurrently())
    assert FakeClient.max_active == 1


def test_sd_listing_is_manual_and_cached() -> None:
    calls = 0

    async def sd_reader(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {
            "command": "M20",
            "status": "complete_listing",
            "raw_response": "Begin file list\nFolder.DIR\npart.gcode\nEnd file list",
        }

    service = make_service(sd_reader=sd_reader)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        assert client.get("/api/printer/sdcard").json()["status"] == "not_requested"
        assert calls == 0
        rejected = client.get(
            "/api/printer/sdcard?refresh=true", headers={"Origin": "http://attacker.example"}
        )
        assert rejected.status_code == 403
        assert calls == 0
        listing = client.get("/api/printer/sdcard?refresh=true").json()
        assert listing["status"] == "available"
        assert listing["entries"] == [
            {
                "name": "Folder/",
                "kind": "directory",
                "size_bytes": None,
                "preview_available": False,
                "ambiguous": False,
                "preview_source": "none",
                "local_preview": False,
                "local_match": "unmatched",
                "local_metadata": None,
            },
            {
                "name": "part.gcode",
                "kind": "file",
                "size_bytes": None,
                "preview_available": False,
                "ambiguous": False,
                "preview_source": "none",
                "local_preview": False,
                "local_match": "unmatched",
                "local_metadata": None,
            },
        ]
        assert client.get("/api/printer/sdcard").json()["entries"] == listing["entries"]
        assert calls == 1


def test_sd_browser_separates_fresh_m20_from_read_only_local_thumbnail_index(tmp_path) -> None:
    source = tmp_path / "volume"
    source.mkdir()
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/1eoAAAAASUVORK5CYII="
    )
    encoded = base64.b64encode(image).decode("ascii")
    (source / "back-dso138.gcode").write_text(
        "; estimated printing time = 12m\n"
        "; thumbnail begin 320x320 68\n"
        f";{encoded}\n"
        "; thumbnail end\n"
    )
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    indexer = LocalGcodeIndexer(source, timeline, require_mount=False)
    service = make_service(interval=30.0)
    service.sd_status = "available"
    service.sd_entries = [{"name": "back-dso138.gcode", "kind": "file", "size_bytes": None}]
    service.sd_refreshed_at = utc_now()
    app = create_app(service, timeline=timeline, local_gcode_indexer=indexer)

    with TestClient(app) as client:
        wait_for_success(service)
        listing = client.get("/api/printer/sdcard").json()
        entry = listing["entries"][0]
        assert listing["printer_listing_source"] == "firmware_m20"
        assert entry["preview_available"] is True
        assert entry["preview_source"] == "cached_local_gcode"
        assert entry["local_metadata"]["metadata"]["estimated_print_time"] == "12m"
        preview = client.get("/api/printer/sdcard/preview/back-dso138.gcode")
        assert preview.status_code == 200
        assert preview.content == image
        local_preview = client.get(
            "/api/printer/sdcard/local-preview", params={"path": "back-dso138.gcode"}
        )
        assert local_preview.status_code == 200
        assert local_preview.content == image


def test_orca_thumbnail_is_cached_without_sending_gcode_to_printer(tmp_path) -> None:
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/1eoAAAAASUVORK5CYII="
    )
    block = base64.b64encode(image).decode("ascii")
    gcode = f"; thumbnail begin 100x100 {len(image)}\n;{block}\n; thumbnail end\nG90\nG1 X1 F300\n"

    async def sd_reader(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {
            "command": "M20",
            "status": "complete_listing",
            "raw_response": "Begin file list\npart.gcode\nEnd file list",
        }

    service = make_service(sd_reader=sd_reader)
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    app = create_app(service, timeline=timeline)
    with TestClient(app) as client:
        wait_for_success(service)
        cached = client.post(
            "/api/printer/sdcard/preview?filename=part.gcode",
            content=gcode.encode(),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert cached.status_code == 200
        assert cached.json()["preview_available"] is True
        assert timeline.get_sd_thumbnail("part.gcode") == ("image/png", image)

        listing = client.get("/api/printer/sdcard?refresh=true").json()
        assert listing["entries"][0]["preview_available"] is True
        assert client.get("/api/printer/sdcard/preview/part.gcode").content == image
        assert service.sd_entries[0]["name"] == "part.gcode"


def test_first_layer_operation_requires_explicit_content_attestation(tmp_path) -> None:
    sent: list[str] = []

    class Transport:
        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            sent.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    executor = CommandExecutor(enabled=True, transport_factory=Transport)
    service = make_service(interval=30.0)
    service.sd_entries = [{"name": "test.gcode", "kind": "file", "size_bytes": None}]
    from datetime import UTC, datetime

    service.sd_refreshed_at = datetime.now(UTC)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="test-token",
        executor=executor,
        timeline=MotionTimeline(tmp_path / "timeline.sqlite3"),
    )
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        wait_for_success(service)
        armed = client.post(
            "/api/control/arm",
            headers=headers,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
        )
        assert armed.status_code == 200
        response = client.post(
            "/api/control/execute",
            headers=headers,
            json={
                "macro_id": "print_first_layer_sd",
                "parameters": {"filename": "test.gcode"},
                "confirmation": "Print first-layer-only SD file",
                "first_layer_contents_confirmed": False,
            },
        )
        assert response.status_code == 422
        assert "M20 listing cannot verify" in response.json()["detail"]
        assert sent == []


def test_websocket_receives_cached_snapshot_and_broadcast() -> None:
    service = make_service()
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        with client.websocket_connect("/ws/printer", headers={"origin": "http://testserver"}) as ws:
            initial = ws.receive_json()
            assert initial["connection"]["connected"] is True
            update = ws.receive_json()
            assert update["telemetry"]["nozzle_temperature"]["value"] == 24.0


def test_control_catalog_preview_and_blocked_execution_do_not_control_printer() -> None:
    service = make_service(interval=30.0)
    app = create_app(service)
    with TestClient(app) as client:
        wait_for_success(service)
        poll_count = FakeClient.calls
        catalog = client.get("/api/control/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["execution_enabled"] is False
        assert catalog.json()["capabilities"]
        home_x = next(item for item in catalog.json()["macros"] if item["id"] == "home_x")
        assert home_x["commissioning_status"] == "verified_historically_reference_cleared"
        assert home_x["current_home_reference"] is False
        assert home_x["compatibility"] == "operator_verified_on_this_printer"
        assert home_x["capability"]["status"] == "ready_for_commissioning"
        orbital = next(
            item
            for item in catalog.json()["macros"]
            if item["id"] == "neptune_3_axis_orbital_dance"
        )
        assert orbital["name"] == "Neptune 3-Axis Orbital Dance"
        assert orbital["execution_block_reason"] is None
        assert orbital["command_families"] == ["G1", "G28", "M400"]
        x_capability = next(
            item for item in catalog.json()["capabilities"] if item["command"] == "G28 X"
        )
        assert x_capability["status"] == "operator_verified_on_this_printer"
        assert client.get("/api/control/state").json()["control"]["enabled"] is False
        preview = client.post(
            "/api/control/preview",
            json={
                "macro_id": "purge_nozzle",
                "parameters": {"length_mm": 10, "feed_mm_min": 120},
            },
        )
        assert preview.status_code == 200
        assert preview.json()["commands"] == ["M83", "G1 E10 F120", "M82"]
        assert preview.json()["execution_enabled"] is False
        dance_preview = client.post(
            "/api/control/preview",
            json={
                "macro_id": "neptune_3_axis_orbital_dance",
                "parameters": {"amplitude_percent": 7, "feed_mm_min": 6000},
            },
        )
        assert dance_preview.status_code == 200
        assert dance_preview.json()["command_count"] == 120
        assert dance_preview.json()["commands"][0] == "M400"
        assert dance_preview.json()["commands"][-2:] == ["G28", "M400"]
        assert dance_preview.json()["execution_enabled"] is False
        assert dance_preview.json()["motion_classification"] == "planned_gcode_preview_only"
        assert (
            client.post(
                "/api/control/preview",
                json={"macro_id": "purge_nozzle", "parameters": {"length_mm": 10}},
            ).status_code
            == 422
        )
        blocked = client.post(
            "/api/control/execute",
            json={"macro_id": "cooldown", "confirmation": "Cooldown"},
        )
        assert blocked.status_code == 423
        assert "No command was sent" in blocked.json()["detail"]
        assert (
            client.post(
                "/api/control/execute",
                json={"macro_id": "cooldown", "confirmation": "Cooldown", "gcode": "M112"},
            ).status_code
            == 422
        )
        audit = client.get("/api/control/audit").json()["entries"]
        assert [item["result"] for item in audit] == [
            "preview_only",
            "preview_only",
            "preview_rejected",
            "blocked_execution_disabled",
        ]
        assert FakeClient.calls == poll_count


def test_lan_bind_requires_token_and_protects_http_and_websocket() -> None:
    service = make_service()
    with pytest.raises(ValueError, match="TOKEN is required"):
        create_app(service, bind_host="0.0.0.0", auth_token=None)

    loopback_bound_app = create_app(make_service())
    with TestClient(loopback_bound_app, client=("198.51.100.44", 53000)) as remote_client:
        wait_for_success(loopback_bound_app.state.printer)
        assert remote_client.get("/api/printer/status").status_code == 401
        assert remote_client.get("/api/printer/motion").status_code == 401
        assert remote_client.get("/api/control/catalog").status_code == 401
        assert (
            remote_client.post("/api/control/preview", json={"macro_id": "cooldown"}).status_code
            == 401
        )

    app = create_app(service, bind_host="0.0.0.0", auth_token="test-secret")
    with TestClient(app) as client:
        wait_for_success(service)
        assert client.get("/api/printer/status").status_code == 401
        assert (
            client.get(
                "/api/printer/status", headers={"Authorization": "Bearer test-secret"}
            ).status_code
            == 200
        )
        motion_response = client.get(
            "/api/printer/motion", headers={"Authorization": "Bearer test-secret"}
        )
        assert motion_response.status_code == 200
        assert motion_response.json()["enabled"] is False
        login = client.post(
            "/api/auth/session",
            json={"token": "test-secret"},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        assert client.get("/health").status_code == 200
        with client.websocket_connect("/ws/printer", headers={"Origin": "http://testserver"}) as ws:
            message = ws.receive_json()
            assert message["connection"]["connected"] is True
            assert message["motion"]["enabled"] is False


def test_control_api_uses_dashboard_auth_and_exact_confirmation() -> None:
    sent: list[str] = []

    class Transport:
        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            sent.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    executor = CommandExecutor(
        enabled=True,
        verified_commands=frozenset({"M104", "M140"}),
        verified_macros=frozenset({"cooldown"}),
        transport_factory=Transport,
    )
    service = make_service(interval=30)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="monitor-token",
        executor=executor,
    )
    with TestClient(app) as client:
        wait_for_success(service)
        monitor = {"Authorization": "Bearer monitor-token"}
        initially_locked = client.get("/api/control/catalog", headers=monitor).json()
        assert initially_locked["execution_enabled"] is False
        assert initially_locked["control"]["session_armed"] is False
        not_attended = client.post(
            "/api/control/arm",
            headers=monitor,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": False,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
        )
        assert not_attended.status_code == 422
        assert (
            client.get("/api/control/catalog", headers=monitor).json()["execution_enabled"] is False
        )
        no_power_access = client.post(
            "/api/control/arm",
            headers=monitor,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": False,
            },
        )
        assert no_power_access.status_code == 422
        arm = client.post(
            "/api/control/arm",
            headers=monitor,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
        )
        assert arm.status_code == 200
        assert arm.json()["session_armed"] is True
        assert arm.json()["session_seconds_remaining"] is None
        assert arm.json()["session_until"] == "operator_disarm_or_service_restart"
        payload = {
            "macro_id": "cooldown",
            "parameters": {},
            "confirmation": "Cooldown",
            "attended": True,
            "no_competing_clients": True,
            "power_switch_reachable": True,
            "clear_travel_path": False,
            "commissioning": False,
        }
        assert client.get("/api/control/catalog", headers=monitor).json()["execution_enabled"]
        denied = client.post("/api/control/execute", json=payload)
        assert denied.status_code == 401
        assert sent == []

        wrong = client.post(
            "/api/control/execute",
            headers=monitor,
            json={**payload, "confirmation": "yes"},
        )
        assert wrong.status_code == 409
        assert sent == []
        accepted = client.post(
            "/api/control/execute",
            headers=monitor,
            json=payload,
        )
        assert accepted.status_code == 202
        operation_id = accepted.json()["id"]
        deadline = time.monotonic() + 1
        operation = None
        while time.monotonic() < deadline:
            operation = client.get(
                f"/api/control/operations/{operation_id}", headers=monitor
            ).json()
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.005)
        assert operation["state"] == "transport_acknowledged"
        assert operation["physical_completion_confirmed"] is False
        assert sent == ["M104 S0", "M140 S0"]
        assert (
            client.post(
                f"/api/control/operations/{operation_id}/cancel",
                headers=monitor,
            ).status_code
            == 200
        )
        disarmed = client.post(
            "/api/control/disarm",
            headers=monitor,
        )
        assert disarmed.status_code == 200
        assert disarmed.json()["session_armed"] is False


def test_operator_mode_runs_unverified_fixed_operation_without_second_credential() -> None:
    sent: list[str] = []

    class Transport:
        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            sent.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    executor = CommandExecutor(enabled=True, transport_factory=Transport)
    service = make_service(interval=30)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="monitor-token",
        executor=executor,
    )
    with TestClient(app) as client:
        wait_for_success(service)
        monitor = {"Authorization": "Bearer monitor-token"}
        catalog = client.get("/api/control/catalog", headers=monitor).json()
        preheat = next(item for item in catalog["macros"] if item["id"] == "preheat_pla")
        assert preheat["capability"]["status"] == "ready_for_commissioning"
        assert preheat["capability"]["commissionable"] is True

        arm = client.post(
            "/api/control/arm",
            headers=monitor,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
        )
        assert arm.status_code == 200
        assert arm.json()["session_seconds_remaining"] is None
        assert arm.json()["session_until"] == "operator_disarm_or_service_restart"
        preview = client.post(
            "/api/control/preview",
            headers=monitor,
            json={
                "macro_id": "preheat_pla",
                "parameters": {"temperature_c": 200, "bed_temperature_c": 60},
                "mode": "operator",
            },
        )
        assert preview.status_code == 200
        assert preview.json()["execution_enabled"] is True
        assert preview.json()["commands"] == ["M104 S200", "M140 S60"]
        assert sent == []
        executed = client.post(
            "/api/control/execute",
            headers=monitor,
            json={
                "macro_id": "preheat_pla",
                "parameters": {"temperature_c": 200, "bed_temperature_c": 60},
                "confirmation": "Preheat PLA",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
                "clear_travel_path": False,
                "mode": "operator",
            },
        )
        assert executed.status_code == 202
        operation_id = executed.json()["id"]
        deadline = time.monotonic() + 1
        operation = None
        while time.monotonic() < deadline:
            operation = client.get(
                f"/api/control/operations/{operation_id}", headers=monitor
            ).json()
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.005)
        assert operation["state"] == "transport_acknowledged"
        assert operation["mode"] == "operator"
        assert operation["operator_authorized"] is True
        assert operation["command_dispatched"] is True
        assert operation["physical_completion_confirmed"] is False
        assert sent == ["M104 S200", "M140 S60"]
        armed = client.get("/api/control/catalog", headers=monitor).json()
        assert armed["control"]["session_armed"] is True
        assert armed["control"]["session_seconds_remaining"] is None
        disarmed = client.post("/api/control/disarm", headers=monitor)
        assert disarmed.status_code == 200
        assert disarmed.json()["session_armed"] is False


def test_home_x_requires_clear_path_and_records_operator_observation() -> None:
    sent: list[str] = []

    class Transport:
        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            sent.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    executor = CommandExecutor(
        enabled=True,
        verified_commands=frozenset({"G28", "G1", "G90", "G91"}),
        verified_macros=frozenset({"home_x", "jog_x_positive", "jog_x_negative"}),
        transport_factory=Transport,
    )
    service = make_service(interval=30)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="monitor-token",
        executor=executor,
    )
    with TestClient(app) as client:
        wait_for_success(service)
        monitor = {"Authorization": "Bearer monitor-token"}
        headers = monitor
        preview_before_arm = client.post(
            "/api/control/preview",
            headers=monitor,
            json={"macro_id": "home_x", "parameters": {}},
        ).json()
        assert preview_before_arm["commands"] == ["G28 X"]
        preview_retreat = client.post(
            "/api/control/preview",
            headers=monitor,
            json={"macro_id": "home_x", "parameters": {}, "retreat_from_home": True},
        ).json()
        assert preview_retreat["commands"] == ["G91", "G1 X30 F600", "G90", "G28 X"]
        home_all_preview = client.post(
            "/api/control/preview",
            headers=monitor,
            json={"macro_id": "home_axes", "parameters": {}},
        ).json()
        assert home_all_preview["commands"] == ["G28"]
        home_all_retreat = client.post(
            "/api/control/preview",
            headers=monitor,
            json={"macro_id": "home_axes", "parameters": {}, "retreat_from_home": True},
        ).json()
        assert home_all_retreat["commands"] == [
            "G91",
            "G1 X30 F600",
            "G1 Y30 F600",
            "G1 Z30 F180",
            "G90",
            "G28",
        ]
        assert preview_before_arm["execution_enabled"] is False
        armed = client.post(
            "/api/control/arm",
            headers=headers,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
        )
        assert armed.status_code == 200
        preview_after_arm = client.post(
            "/api/control/preview",
            headers=monitor,
            json={"macro_id": "home_x", "parameters": {}},
        ).json()
        assert preview_after_arm["execution_enabled"] is True
        payload = {
            "macro_id": "home_x",
            "parameters": {},
            "confirmation": "Home X",
            "attended": True,
            "no_competing_clients": True,
            "power_switch_reachable": True,
            "clear_travel_path": False,
            "axis_at_home": False,
        }
        blocked = client.post("/api/control/execute", headers=headers, json=payload)
        assert blocked.status_code == 422
        assert sent == []

        not_at_home = client.post(
            "/api/control/execute",
            headers=headers,
            json={
                **payload,
                "clear_travel_path": True,
                "axis_at_home": False,
                "retreat_from_home": True,
            },
        )
        assert not_at_home.status_code == 422
        assert "physically at their home switches" in not_at_home.json()["detail"]
        assert sent == []
        client.post("/api/control/disarm", headers=headers)
        unsafe_arm = client.post(
            "/api/control/arm",
            headers=headers,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "power_switch_reachable": False,
                "no_competing_clients": True,
            },
        )
        assert unsafe_arm.status_code == 422
        rearmed = client.post(
            "/api/control/arm",
            headers=headers,
            json={
                "confirmation": "ENABLE OPERATOR MODE",
                "attended": True,
                "no_competing_clients": True,
                "power_switch_reachable": True,
            },
        )
        assert rearmed.status_code == 200
        assert sent == []

        accepted = client.post(
            "/api/control/execute",
            headers=headers,
            json={
                **{
                    key: value
                    for key, value in payload.items()
                    if key not in {"attended", "no_competing_clients", "power_switch_reachable"}
                },
                "clear_travel_path": True,
                "axis_at_home": True,
                "retreat_from_home": True,
            },
        )
        assert accepted.status_code == 202
        operation_id = accepted.json()["id"]
        deadline = time.monotonic() + 1
        operation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            operation = client.get(
                f"/api/control/operations/{operation_id}", headers=monitor
            ).json()
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.005)
        assert operation is not None
        assert operation["state"] == "transport_acknowledged"
        assert operation["physical_completion_confirmed"] is False
        assert operation["awaiting_operator_observation"] is False
        assert sent == ["G91", "G1 X30 F600", "G90", "G28 X"]
        assert executor.homed_axes == {"x"}
        catalog = client.get("/api/control/catalog", headers=monitor).json()
        home_x = next(item for item in catalog["macros"] if item["id"] == "home_x")
        jog_x = next(item for item in catalog["macros"] if item["id"] == "jog_x_positive")
        x_capability = next(item for item in catalog["capabilities"] if item["command"] == "G28 X")
        jog_capability = next(item for item in catalog["capabilities"] if item["command"] == "G1 X")
        assert home_x["commissioning_status"] == "command_acknowledged_reference_estimate"
        assert jog_x["commissioning_status"] == "estimated_reference_available"
        assert jog_x["readiness"]["status"] == "ready_to_confirm"
        assert x_capability["status"] == "operator_verified_on_this_printer"
        assert jog_capability["status"] == "estimated_reference_available"
        motion = client.get("/api/printer/motion", headers=monitor).json()
        assert motion["latest"] is None
        assert motion["operator_reference"]["axes"]["x"]["homed"] is True
        assert motion["operator_reference"]["axes"]["x"]["position_mm"] == 0.0
        assert (
            motion["operator_reference"]["axes"]["x"]["reference_observation"]["kind"]
            == "firmware_homing_acknowledgment_estimate"
        )
        assert "not firmware position samples" in motion["operator_reference"]["note"]
        snapshot = client.get("/api/printer/status", headers=monitor).json()
        assert snapshot["motion"]["operator_reference"]["axes"]["x"]["homed"] is True


def test_dashboard_credential_can_authorize_controls_without_a_second_token() -> None:
    executor = CommandExecutor(enabled=True, transport_factory=lambda: None)
    app = create_app(
        make_service(),
        bind_host="0.0.0.0",
        auth_token="same-token",
        control_token="same-token",
        executor=executor,
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/control/catalog", headers={"Authorization": "Bearer same-token"}
        )
        assert response.status_code == 200


def test_cooldown_commissioning_api_requires_auth_attendance_and_exact_macro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEPTUNE_COOLDOWN_COMMISSIONING_ENABLED", "true")

    class MockCommissioner:
        used = False
        operations: list[dict[str, Any]] = []

        async def run(self, _io_lock: asyncio.Lock, preflight: Any = None) -> dict[str, Any]:
            if preflight is not None:
                preflight()
            self.used = True
            return {
                "state": "transport_acknowledged",
                "results": [
                    {"command": "M104 S0", "acknowledgment": "ok"},
                    {"command": "M140 S0", "acknowledgment": "ok"},
                ],
            }

        def history(self) -> list[dict[str, Any]]:
            return self.operations

        def record(self, operation: dict[str, Any]) -> None:
            self.operations.append(operation)

    service = make_service(interval=30)
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="monitor-secret",
        commissioning_executor=MockCommissioner(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        wait_for_success(service)
        # Ensure print_active is explicitly present in the fake telemetry data.
        monitor = {"Authorization": "Bearer monitor-secret"}
        payload = {
            "macro_id": "cooldown",
            "confirmation": "Cooldown",
            "attended": True,
            "no_competing_clients": True,
        }
        assert client.post("/api/control/commissioning/cooldown", json=payload).status_code == 401
        wrong = client.post(
            "/api/control/commissioning/cooldown",
            headers=monitor,
            json={**payload, "macro_id": "preheat_pla"},
        )
        assert wrong.status_code == 422
        unattended = client.post(
            "/api/control/commissioning/cooldown",
            headers=monitor,
            json={**payload, "attended": False},
        )
        assert unattended.status_code == 422
        clients_active = client.post(
            "/api/control/commissioning/cooldown",
            headers=monitor,
            json={**payload, "no_competing_clients": False},
        )
        assert clients_active.status_code == 422
        accepted = client.post(
            "/api/control/commissioning/cooldown",
            headers=monitor,
            json=payload,
        )
        assert accepted.status_code == 202
        report = accepted.json()
        assert report["commands"] == ["M104 S0", "M140 S0"]
        assert report["telemetry_before"]["telemetry"]["printer_state"]["value"] == "idle"
        assert "telemetry_after" in report
        assert app.state.cooldown_executor.used is True
        assert (
            client.get("/api/control/commissioning/cooldown", headers=monitor).json()["enabled"]
            is False
        )
