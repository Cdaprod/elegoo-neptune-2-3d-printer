from __future__ import annotations

import asyncio
import socket
import unittest

from mks_wifi_bridge.client import list_sd_root, probe_printer


class MockMksTcpServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.commands: list[list[str]] = []
        self.closed = asyncio.Event()
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(512), 0.5)
            commands = [line for line in data.decode("ascii").splitlines() if line]
            self.commands.append(commands)
            if commands == ["M115"]:
                response = b"ok\r\nFIRMWARE_NAME:TFT24\r\n"
            elif commands == ["M20"]:
                response = b"Begin file list\r\nsample.gcode\r\nEnd file list\r\n"
            elif self.response_mode == "empty":
                response = b""
            elif self.response_mode == "malformed":
                response = b"unrecognized\r\n"
            elif self.response_mode == "partial":
                response = b"ok\r\nT:201.5 /205 B:60 /60\r\nM997 PRINTING\nM27 42"
            elif self.response_mode == "timeout":
                await reader.read()
                return
            else:
                response = (
                    b"ok\r\nT:201.5 /205 B:60 /60\r\nM997 PRINTING\r\n"
                    b"M27 42\r\nM994 1:/sample.gcode;12345\r\nM992 00:12:34\r\n"
                )
            for part in (response[:12], response[12:27], response[27:]):
                if part:
                    writer.write(part)
                    await writer.drain()
                    await asyncio.sleep(0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            finally:
                self.closed.set()

    response_mode = "success"

    async def test_success_and_only_approved_commands(self) -> None:
        self.response_mode = "success"
        report = await probe_printer("127.0.0.1", self.port, timeout=0.5)
        self.assertTrue(report["connection"]["reachable"])
        self.assertEqual(report["telemetry"]["nozzle_temperature"]["value"], 201.5)
        self.assertEqual(report["telemetry"]["bed_temperature"]["value"], 60.0)
        self.assertEqual(report["telemetry"]["printer_state"]["value"], "printing")
        self.assertEqual(report["telemetry"]["firmware_identification"]["value"], "TFT24")
        sent = [command for batch in self.commands for command in batch]
        self.assertEqual(
            sent,
            ["M105", "M997", "M27", "M994", "M992", "M115"],
        )
        self.assertFalse(
            any(command.startswith(("M23", "M24", "M104", "M140", "G0", "G28")) for command in sent)
        )

    async def test_partial_packet_is_parsed_and_connection_closes(self) -> None:
        self.response_mode = "partial"
        report = await probe_printer("127.0.0.1", self.port, timeout=0.5)
        self.assertTrue(report["connection"]["reachable"])
        self.assertEqual(report["telemetry"]["nozzle_temperature"]["value"], 201.5)
        self.assertEqual(report["telemetry"]["bed_temperature"]["value"], 60.0)
        self.assertTrue(self.closed.is_set())

    async def test_empty_response_is_reported_unreachable(self) -> None:
        self.response_mode = "empty"
        report = await probe_printer("127.0.0.1", self.port, timeout=0.3)
        self.assertFalse(report["connection"]["reachable"])
        self.assertTrue(report["errors"])

    async def test_read_timeout_is_bounded_and_connection_closes(self) -> None:
        self.response_mode = "timeout"
        report = await probe_printer("127.0.0.1", self.port, timeout=0.1)
        self.assertFalse(report["connection"]["reachable"])
        await asyncio.wait_for(self.closed.wait(), 0.3)

    async def test_malformed_response_marks_fields_unavailable(self) -> None:
        self.response_mode = "malformed"
        report = await probe_printer("127.0.0.1", self.port, timeout=0.1)
        self.assertTrue(report["connection"]["reachable"])
        self.assertEqual(
            report["telemetry"]["nozzle_temperature"]["status"],
            "unsupported_or_not_reported",
        )


def _unused_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConnectionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_refusal_is_reported(self) -> None:
        report = await probe_printer("127.0.0.1", _unused_tcp_port(), timeout=0.2)
        self.assertFalse(report["connection"]["reachable"])
        self.assertIn("Cannot connect", report["errors"][0]["message"])


class SdListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_m20_listing_uses_only_fixed_read_only_command(self) -> None:
        server = MockMksTcpServer()
        await server.asyncSetUp()
        try:
            result = await list_sd_root("127.0.0.1", server.port, timeout=0.3)
            self.assertEqual(result["status"], "complete_listing")
            self.assertIn("sample.gcode", result["raw_response"])
            self.assertEqual(server.commands, [["M20"]])
        finally:
            await server.asyncTearDown()
