"""RSD/1 experimental USB protocol. Never connects to a printer itself.

M794 is private to the pinned experimental firmware, NOT a stock Marlin command.
Only the OctoPrint owner may emit it after capability advertisement and opt-in.
"""

from __future__ import annotations

import hmac
import json
import re
import zlib
from collections.abc import Awaitable, Callable

from .robin_sd_read import MAX_SD_BYTES, SdReadError

VERSION = 1
CHUNK = 192
OPERATIONS = ("CAPABILITIES", "LIST", "STAT", "OPEN", "READ", "CLOSE", "CANCEL")
ALIAS = re.compile(r"[A-Z0-9_~-]{1,8}\.(?:GCO|GC|G)", re.I)


def integer(value: object, maximum: int = 0xFFFFFFFF) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise SdReadError("Invalid unsigned protocol integer")
    return value


def command(
    operation: str,
    *,
    sequence: int,
    session: int = 0,
    offset: int = 0,
    count: int = 0,
    filename: str = "",
) -> str:
    if operation not in OPERATIONS:
        raise SdReadError("Unknown read-only operation")
    for value in (sequence, session, offset):
        integer(value)
    integer(count, CHUNK)
    if operation in {"STAT", "OPEN"} and (
        not isinstance(filename, str) or not ALIAS.fullmatch(filename)
    ):
        raise SdReadError("Use the root-level DOS alias advertised by LIST")
    if filename and operation not in {"STAT", "OPEN"}:
        raise SdReadError("Unexpected filename")
    result = f"M794 V1 O{OPERATIONS.index(operation)} Q{sequence} S{session} A{offset} B{count}"
    if filename:
        result += " P" + "".join(f"{ord(c):03d}" for c in filename.upper())
    if len(result) > 95:
        raise SdReadError("Command exceeds Marlin command buffer")
    return result


def parse_reply(line: str, *, operation: str, sequence: int, session: int | None = None) -> dict:
    if not isinstance(line, str) or len(line) > 768 or not line.startswith("RSD1 "):
        raise SdReadError("Invalid or oversized RSD frame")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SdReadError("Duplicate packet field")
            result[key] = value
        return result

    try:
        frame = json.loads(line[5:], object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise SdReadError("Malformed RSD frame") from exc
    if not isinstance(frame, dict) or type(frame.get("v")) is not int or frame.get("v") != VERSION:
        raise SdReadError("Unsupported protocol version")
    if frame.get("op") != operation or integer(frame.get("q")) != sequence:
        raise SdReadError("Unexpected operation or sequence")
    if session is not None and integer(frame.get("s")) != session:
        raise SdReadError("Unexpected transfer session")
    if frame.get("error"):
        raise SdReadError("Firmware rejected read: " + str(frame["error"])[:80])
    return frame


class ProtocolReader:
    """Serialized request/reply adapter with explicit session and offset accounting."""

    def __init__(self, exchange: Callable[[dict], Awaitable[str]]):
        self.exchange = exchange
        self.sequence = 0
        self.session = 0
        self.filename = ""
        self.offset = 0
        self.token = ""
        self.length = 0

    async def request(self, operation: str, **fields) -> dict:
        self.sequence += 1
        request = {
            "operation": operation,
            "sequence": self.sequence,
            "session": self.session,
            **fields,
        }
        command(**request)  # Validate before crossing the OctoPrint API boundary.
        reply = await self.exchange(request)
        return parse_reply(
            reply,
            operation=operation,
            sequence=self.sequence,
            session=self.session if operation in {"READ", "CLOSE", "CANCEL"} else None,
        )

    async def capabilities(self) -> dict:
        frame = await self.request("CAPABILITIES")
        if (
            frame.get("chunk") != CHUNK
            or frame.get("max_bytes") != MAX_SD_BYTES
            or frame.get("hash") != "sha256"
        ):
            raise SdReadError("Incompatible firmware transfer bounds")
        return frame

    async def listing(self) -> list[dict]:
        result = []
        for cursor in range(4096):
            frame = await self.request("LIST", offset=cursor)
            if frame.get("end") is True:
                return result
            name = frame.get("name", "")
            if not isinstance(name, str) or not ALIAS.fullmatch(name):
                raise SdReadError("Invalid firmware directory entry")
            integer(frame.get("size"), MAX_SD_BYTES)
            display = name
            if "display_hex" in frame:
                try:
                    encoded = frame["display_hex"]
                    if not isinstance(encoded, str) or len(encoded) > 400:
                        raise ValueError()
                    display = bytes.fromhex(encoded).decode("utf-8", errors="replace")
                    if not display or any(ord(c) < 32 for c in display):
                        raise ValueError()
                except (ValueError, TypeError) as exc:
                    raise SdReadError("Invalid firmware display name") from exc
            result.append(
                {
                    "name": name,
                    "size": frame["size"],
                    "provider": "printer_sd",
                    "original_path": name,
                    "original_filename": display,
                    "geometry_available": False,
                }
            )
        raise SdReadError("Directory exceeds entry limit")

    async def stat(self, filename: str) -> tuple[int, str]:
        frame = await self.request("STAT", filename=filename)
        length = integer(frame.get("size"), MAX_SD_BYTES)
        token = frame.get("token")
        if not length or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{1,64}", token):
            raise SdReadError("Invalid file snapshot")
        return length, token

    async def open(self, filename: str) -> tuple[int, str]:
        self.length, self.token = await self.stat(filename)
        frame = await self.request("OPEN", filename=filename)
        self.session = integer(frame.get("s"))
        if not self.session:
            raise SdReadError("Invalid session identifier")
        if frame.get("token") != self.token or frame.get("size") != self.length:
            raise SdReadError("File changed before OPEN")
        self.filename, self.offset = filename, 0
        return self.length, self.token

    async def read(self, filename: str, offset: int, count: int, token: str) -> bytes:
        if (
            not self.session
            or filename != self.filename
            or token != self.token
            or offset != self.offset
        ):
            raise SdReadError("Read does not match open snapshot")
        if not 1 <= count <= CHUNK or offset + count > self.length:
            raise SdReadError("Read exceeds snapshot")
        frame = await self.request("READ", offset=offset, count=count)
        try:
            data = bytes.fromhex(frame["data"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SdReadError("Invalid chunk encoding") from exc
        if (
            len(data) != count
            or frame.get("offset") != offset
            or frame.get("token") != token
            or integer(frame.get("crc")) != zlib.crc32(data)
        ):
            raise SdReadError("Chunk integrity or snapshot mismatch")
        self.offset += count
        return data

    async def close(self, *, cancel: bool = False, expected_sha256: str | None = None):
        if self.session:
            try:
                frame = await self.request("CANCEL" if cancel else "CLOSE")
                if not cancel:
                    digest = frame.get("sha256")
                    if (
                        not isinstance(digest, str)
                        or not expected_sha256
                        or not hmac.compare_digest(digest, expected_sha256)
                    ):
                        raise SdReadError("End-to-end firmware SHA-256 mismatch")
            finally:
                self.session = 0
