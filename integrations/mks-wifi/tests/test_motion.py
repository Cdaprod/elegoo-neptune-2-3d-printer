from __future__ import annotations

import asyncio
import unittest

from mks_wifi_bridge.motion import build_motion_sample, parse_m114, query_m114


def test_parse_m114_full_coordinates_and_ignores_step_count() -> None:
    result = parse_m114(["X:12.50 Y:-3.25 Z:0.4 E:1e1 Count X:999 Y:999 Z:999 E:999", "ok"])
    assert result["status"] == "available"
    assert [result["axes"][axis]["value_mm"] for axis in "xyze"] == [12.5, -3.25, 0.4, 10.0]


def test_missing_axes_are_unavailable_and_not_zero() -> None:
    result = parse_m114(["X:1.5 Z:3.0", "ok"])
    assert result["axes"]["x"]["value_mm"] == 1.5
    assert result["axes"]["y"] == {"status": "unsupported_or_not_reported", "value_mm": None}
    assert result["axes"]["e"]["value_mm"] is None


def test_malformed_or_empty_position_is_unavailable() -> None:
    assert parse_m114(["busy: processing", "ok"])["status"] == "unsupported_or_malformed"
    assert parse_m114([])["status"] == "empty_response"


def test_coordinate_reset_candidate_suppresses_velocity_and_activity() -> None:
    previous = {
        "poll_timestamp": "2026-10-30T10:00:00+00:00",
        "axes": {axis: {"value_mm": 42.0} for axis in "xyze"},
    }
    result = {
        "status": "available",
        "axes": {axis: {"status": "available", "value_mm": 0.0} for axis in "xyze"},
    }
    sample = build_motion_sample(
        result,
        observed_at="2026-10-30T10:00:01+00:00",
        printer_state="idle",
        previous=previous,
    )
    assert sample["interval"]["status"] == "coordinate_discontinuity"
    assert sample["interval"]["velocities_mm_s"]["x"] is None
    assert sample["interval"]["movement_axes"] == []


def test_plausible_consecutive_samples_produce_approximate_velocity() -> None:
    previous = {
        "poll_timestamp": "2026-10-30T10:00:00+00:00",
        "axes": {axis: {"value_mm": 10.0} for axis in "xyze"},
    }
    result = {
        "status": "available",
        "axes": {axis: {"status": "available", "value_mm": 12.0} for axis in "xyze"},
    }
    sample = build_motion_sample(
        result,
        observed_at="2026-10-30T10:00:02+00:00",
        printer_state="idle",
        previous=previous,
    )
    assert sample["interval"]["status"] == "estimated"
    assert sample["interval"]["deltas_mm"]["x"] == 2
    assert sample["interval"]["velocities_mm_s"]["x"] == 1
    assert sample["interval"]["confidence"] == "approximate_coordinate_delta"


class M114Server(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.commands: list[str] = []
        self.response: bytes | None = (
            b"X:10.0 Y:20.0 Z:3.0 E:4.0 Count X:100 Y:200 Z:30 E:40\r\nok\r\n"
        )
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.readline()
            self.commands.append(data.decode().strip())
            if self.response is None:
                await reader.read()
                return
            writer.write(self.response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_query_sends_only_m114_and_parses_response(self) -> None:
        result = await query_m114("127.0.0.1", self.port, timeout=0.5)
        self.assertEqual(self.commands, ["M114"])
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["axes"]["z"]["value_mm"], 3.0)
        self.assertEqual(result["acknowledgment"], "ok")
        self.assertGreaterEqual(result["latency_ms"], 0)

    async def test_unsupported_command_response_is_reported(self) -> None:
        self.response = b"Error:Unknown command: M114\r\n"
        result = await query_m114("127.0.0.1", self.port, timeout=0.5)
        self.assertEqual(result["status"], "unsupported_or_unavailable")
        self.assertTrue(all(item["value_mm"] is None for item in result["axes"].values()))
        self.assertIn("rejected", result["error"])

    async def test_m114_timeout_is_bounded(self) -> None:
        self.response = None
        result = await query_m114("127.0.0.1", self.port, timeout=0.05)
        self.assertEqual(result["status"], "unsupported_or_unavailable")
        self.assertLess(result["latency_ms"], 500)
