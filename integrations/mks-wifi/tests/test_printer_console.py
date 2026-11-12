"""Read-only printer terminal parity and API boundaries."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mks_wifi_bridge.motion_timeline import MotionTimeline
from mks_wifi_bridge.observatory import PrinterService, create_app
from mks_wifi_bridge.octoprint_backend import OctoPrintBackend, OctoPrintError
from mks_wifi_bridge.printer_console import parse_serial_log

FIXTURE = (
    b"2026-11-12 19:05:00,100 - Send: M27\n"
    b"2026-11-12 19:05:00,120 - Recv: ok\n"
    b"2026-11-12 19:05:00,180 - Recv: M27 7\n"
    b"2026-11-12 19:05:05,100 - Send: M27\n"
    b"2026-11-12 19:05:05,180 - Recv: M27 8\n"
    b"2026-11-12 19:05:07,010 - Recv: token=top-secret\n"
    b"2026-11-12 19:05:08,050 - Changing monitoring state\n"
)


def test_real_send_recv_lines_preserve_order_and_timestamps() -> None:
    log = parse_serial_log(FIXTURE, limit=4)
    assert log["source"] == "octoprint_serial_log"
    assert log["status"] == "available"
    assert [x["text"] for x in log["events"]] == [
        "Recv: M27 7",
        "Send: M27",
        "Recv: M27 8",
        "Recv: token=[REDACTED]",
    ]
    assert log["events"][0]["observed_at"] == "2026-11-12 19:05:00,180"


def test_malformed_log_cannot_fabricate_messages() -> None:
    assert parse_serial_log(b"printing progress 30%\nno serial events\n")["events"] == []
    assert parse_serial_log(b"Send: M105\nRecv: T:210 /210\n")["events"][1]["text"] == (
        "Recv: T:210 /210"
    )
    with pytest.raises(ValueError):
        parse_serial_log(b"Recv: ok", limit=501)


class ConsoleBackend(OctoPrintBackend):
    def __init__(self) -> None:
        super().__init__("http://octoprint:80", "fixture-key")
        self.error = False

    async def serial_console(self, limit: int = 200) -> dict[str, Any]:
        if self.error:
            raise OctoPrintError("OctoPrint serial log is unavailable")
        return parse_serial_log(FIXTURE, limit=limit)

    async def get(self, endpoint: str) -> dict[str, Any]:
        if endpoint == "/api/connection":
            return {"current": {"state": "Operational"}}
        if endpoint == "/api/printer":
            return {
                "state": {"flags": {"operational": True}},
                "temperature": {
                    "tool0": {"actual": 22, "target": 0},
                    "bed": {"actual": 21, "target": 0},
                },
            }
        if endpoint == "/api/job":
            return {"job": {"file": {}}, "progress": {}}
        if endpoint == "/api/files":
            return {"files": []}
        raise AssertionError("Unexpected OctoPrint endpoint")


def test_console_requires_auth_and_returns_actual_octoprint_events(tmp_path: Any) -> None:
    backend = ConsoleBackend()
    service = PrinterService(
        host=backend.host,
        port=backend.port,
        backend=backend,
        interval=30,
        client_factory=lambda *args, **kwargs: pytest.fail("MKS socket must not open"),
    )
    app = create_app(
        service,
        bind_host="0.0.0.0",
        auth_token="test-console-secret",
        timeline=MotionTimeline(tmp_path / "console.sqlite3"),
    )
    auth = {"Authorization": "Bearer test-console-secret"}
    with TestClient(app) as client:
        assert client.get("/api/printer/console").status_code == 401
        result = client.get("/api/printer/console?limit=3", headers=auth)
        assert result.status_code == 200
        events = result.json()["events"]
        assert len(events) == 3
        assert events[-1]["text"] == "Recv: token=[REDACTED]"
        assert client.get("/api/printer/console?limit=501", headers=auth).status_code == 422
        backend.error = True
        degraded = client.get("/api/printer/console", headers=auth).json()
        assert degraded["status"] == "unavailable"
        assert degraded["events"] == []
        page = client.get("/", headers=auth).text
        assert page.count('class="printer-log" data-printer-log') == 2
        assert "/api/printer/console?limit=250" in page

class RealTimeStub(OctoPrintBackend):
    """OctoPrint's live buffer has lines even when serial.log is empty."""

    def __init__(self, *, response: dict[str, Any] | None = None, error: str | None = None):
        super().__init__("http://octoprint:80", "fixture-key")
        self.response = response
        self.error = error
        self.fallback_reads = 0

    def _get_sync(self, endpoint: str) -> dict[str, Any]:
        assert endpoint == "/api/plugin/neptune_terminal?limit=250"
        if self.error:
            raise OctoPrintError(self.error)
        return self.response or {}

    def _read_serial_log_sync(self, limit: int) -> dict[str, Any]:
        self.fallback_reads += 1
        return parse_serial_log(b"", limit=limit)


def test_live_messages_win_over_empty_file_log() -> None:
    backend = RealTimeStub(
        response={
            "status": "available",
            "source": "octoprint_live_terminal_hook",
            "events": [
                {
                    "direction": "send",
                    "text": "Send: M27",
                    "observed_at": "2026-11-12T21:28:00+00:00",
                },
                {
                    "direction": "recv",
                    "text": "Recv: M27 8",
                    "observed_at": "2026-11-12T21:28:00+00:00",
                },
            ],
        }
    )
    result = asyncio.run(backend.serial_console(250))
    assert result["status"] == "available"
    assert result["source"] == "octoprint_live_terminal_hook"
    assert [item["text"] for item in result["events"]] == ["Send: M27", "Recv: M27 8"]
    assert backend.fallback_reads == 0


def test_missing_plugin_with_empty_serial_log_is_unavailable() -> None:
    backend = RealTimeStub(error="OctoPrint API returned HTTP 404")
    result = asyncio.run(backend.serial_console(250))
    assert backend.fallback_reads == 1
    assert result["status"] == "unavailable"
    assert result["events"] == []
    assert "not installed" in result["note"]


def test_auth_failure_does_not_hide_behind_empty_serial_log() -> None:
    backend = RealTimeStub(error="OctoPrint API returned HTTP 403")
    with pytest.raises(OctoPrintError, match="403"):
        asyncio.run(backend.serial_console(250))
    assert backend.fallback_reads == 0


def test_malformed_live_hook_events_cannot_impersonate_printer() -> None:
    backend = RealTimeStub(
        response={
            "status": "available",
            "source": "octoprint_live_terminal_hook",
            "events": [
                {
                    "direction": "recv",
                    "text": "Send: G28",
                    "observed_at": "2026-11-12T21:28:00+00:00",
                }
            ],
        }
    )
    with pytest.raises(OctoPrintError, match="invalid event"):
        asyncio.run(backend.serial_console(250))


def test_bundled_live_plugin_only_reads_messages_without_modification() -> None:
    """Load the single-file plugin with lightweight OctoPrint/Flask test shims."""
    import importlib.util
    import sys
    import types
    from pathlib import Path
    from unittest.mock import patch

    path = (
        Path(__file__).parents[2] / "octoprint" / "neptune_terminal.py"
    )
    assert path.is_file()
    plugin_module = types.ModuleType("octoprint.plugin")
    plugin_module.SimpleApiPlugin = type("SimpleApiPlugin", (), {})
    octoprint = types.ModuleType("octoprint")
    octoprint.plugin = plugin_module
    access = types.ModuleType("octoprint.access")
    permissions = types.ModuleType("octoprint.access.permissions")
    permissions.Permissions = types.SimpleNamespace(MONITOR=types.SimpleNamespace(can=lambda: True))
    flask = types.ModuleType("flask")
    flask.jsonify = lambda data: data
    flask.abort = lambda status: (_ for _ in ()).throw(ValueError(status))
    with patch.dict(
        sys.modules,
        {
            "octoprint": octoprint,
            "octoprint.plugin": plugin_module,
            "octoprint.access": access,
            "octoprint.access.permissions": permissions,
            "flask": flask,
        },
    ):
        spec = importlib.util.spec_from_file_location("_neptune_terminal_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.__plugin_load__()
        plugin = module.__plugin_implementation__
        assert plugin.received(None, "T:209 /210 B:60 /60") == "T:209 /210 B:60 /60"
        assert plugin.received(None, "M27 8") == "M27 8"
        assert plugin.sent(None, "sent", "M27", "sd_status_poll", "M27") is None
        request = types.SimpleNamespace(args={"limit": "10"})
        result = plugin.on_api_get(request)
        assert result["source"] == "octoprint_live_terminal_hook"
        assert [x["text"] for x in result["events"]] == [
            "Recv: T:209 /210 B:60 /60",
            "Recv: M27 8",
            "Send: M27",
        ]
        assert plugin.get_api_commands() == {}
