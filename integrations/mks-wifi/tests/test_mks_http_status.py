"""Hardware-free tests for MKS HTTP / OctoPrint SD-state reconciliation."""

from __future__ import annotations

import asyncio

import pytest

from mks_wifi_bridge.jobs import print_job_summary
from mks_wifi_bridge.mks_http_status import (
    MksHttpStatusError,
    MksHttpStatusReader,
    normalize_mks_http_status,
)
from mks_wifi_bridge.motion_timeline import MotionTimeline
from mks_wifi_bridge.observatory import PrinterService


def mks_state(progress: float = 8, state: str = "printing") -> dict:
    return normalize_mks_http_status(
        {
            "state": {
                "flags": {
                    "operational": True,
                    "printing": state == "printing",
                    "paused": state == "paused",
                }
            }
        },
        {
            "job": {"file": {"name": "enclosure.gcode"}},
            "progress": {"completion": progress / 100, "printTime": 123},
        },
    )


def host_snapshot(external: dict | None = None, *, active: bool = False) -> dict:
    return {
        "connection": {"connected": True, "transport": "OctoPrint API"},
        "stale": False,
        "observed_at": "2026-11-12T19:05:00+00:00",
        "telemetry": {
            "printer_state": {"status": "available", "value": "printing" if active else "idle"},
            "print_active": {"status": "available", "value": active},
            "external_sd": external or {"status": "unavailable", "state": "unknown"},
        },
    }


def test_m27_7_to_8_represents_cached_progress_in_source_protocol() -> None:
    first, second = mks_state(7), mks_state(8)
    assert [first["progress_percent"], second["progress_percent"]] == [7, 8]
    assert first["state"] == second["state"] == "printing"
    assert second["filename"] == "enclosure.gcode"
    assert mks_state(8, "paused")["state"] == "paused"
    assert mks_state(8, "idle")["progress_percent"] is None


def test_mks_http_parser_does_not_invent_state_or_invalid_progress() -> None:
    assert normalize_mks_http_status({}, {})["state"] == "unknown"
    assert (
        normalize_mks_http_status({"state": {"flags": {"operational": True}}}, {})["state"]
        == "unknown"
    )
    assert (
        normalize_mks_http_status(
            {"state": {"flags": {"printing": True}}}, {"progress": {"completion": 999}}
        )["progress_percent"]
        is None
    )
    with pytest.raises(ValueError):
        MksHttpStatusReader("public.example")
    with pytest.raises(ValueError):
        MksHttpStatusReader("8.8.8.8")


def test_only_two_read_only_http_requests_are_sent() -> None:
    reader = MksHttpStatusReader("192.168.0.95")
    paths: list[str] = []

    def fake_get(path: str) -> dict:
        paths.append(path)
        return (
            {"state": {"flags": {"operational": True, "printing": True}}}
            if path == "/api/printer"
            else {"job": {"file": {"name": "part.gcode"}}, "progress": {"completion": 0.08}}
        )

    reader._get_sync = fake_get
    assert asyncio.run(reader.read_state())["progress_percent"] == 8
    assert paths == ["/api/printer", "/api/job"]


def test_octoprint_host_idle_is_not_physical_idle() -> None:
    unknown = print_job_summary(host_snapshot())
    assert unknown["status"] == "unknown"
    assert unknown["active"] is None
    observed = print_job_summary(host_snapshot(mks_state(8)))
    assert observed["status"] == "printing"
    assert observed["progress_percent"]["value"] == 8
    assert observed["source"].startswith("MKS WiFi HTTP")
    assert observed["host_job_state"] == "idle"


def test_octoprint_host_job_wins_when_mks_cache_conflicts() -> None:
    observed = print_job_summary(host_snapshot(mks_state(8, "idle"), active=True))
    assert observed["status"] == "printing"
    assert observed["source"] == "OctoPrint API telemetry"
    assert observed["physical_activity"]["status"] == "conflict"


def test_mks_http_error_degrades_without_losing_octoprint_temperature() -> None:
    class Backend:
        async def read_state(self) -> dict:
            return {
                "printer_state": {"status": "available", "value": "idle"},
                "print_active": {"status": "available", "value": False},
                "nozzle_temperature": {"status": "available", "value": 210},
                "bed_temperature": {"status": "available", "value": 60},
            }

    class Broken:
        async def read_state(self) -> dict:
            raise MksHttpStatusError("MKS HTTP status unreachable or timed out")

    service = PrinterService(
        backend=Backend(),
        physical_status_reader=Broken(),
        interval=30,
    )
    snapshot = asyncio.run(service.poll_once())
    assert snapshot["connection"]["connected"] is True
    assert snapshot["telemetry"]["nozzle_temperature"]["value"] == 210
    assert snapshot["telemetry"]["external_sd"]["status"] == "unavailable"
    assert print_job_summary(snapshot)["status"] == "unknown"


def test_recorded_sd_sessions_end_without_claiming_physical_success(tmp_path) -> None:
    db = MotionTimeline(tmp_path / "sessions.sqlite3")
    for progress, active, state in [
        (7, True, "printing"),
        (8, True, "printing"),
        (None, False, "idle"),
    ]:
        sample = host_snapshot()
        sample["physical_source"] = "mks_wifi_http"
        sample["telemetry"].update(
            {
                "printer_state": {"value": state},
                "print_active": {"value": active},
                "progress_percent": {"value": progress},
                "current_file": {"value": "enclosure.gcode" if active else None},
                "nozzle_temperature": {"value": 210, "target_c": 210},
                "bed_temperature": {"value": 60, "target_c": 60},
            }
        )
        db.record_snapshot(sample)
    history = db.list_print_history()
    assert history["sessions"][0]["source"] == "MKS_HTTP_SD_telemetry"
    assert history["sessions"][0]["outcome"] == "ended_unverified"
