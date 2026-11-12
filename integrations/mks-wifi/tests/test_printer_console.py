"""Read-only printer terminal parity and API boundaries."""

from __future__ import annotations

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
