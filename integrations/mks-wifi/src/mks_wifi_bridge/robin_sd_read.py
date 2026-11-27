"""Read-only Robin Nano SD retrieval: explicit capability negotiation and bounded chunks.

Stock MKS WiFi exposes M20 names, not file bytes. There is deliberately no
auto-probing of unknown G-code or an unverified firmware read command.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

MAX_SD_BYTES = 50 * 1024 * 1024
MAX_SD_CHUNK_BYTES = 4096
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,199}\.(?:gcode|gco|gc|g)", re.I)


class SdReadError(RuntimeError):
    """The board, transport, file, or integrity checks rejected a read."""


@dataclass(frozen=True)
class SdCapability:
    driver: str
    status: str
    can_list: bool
    can_read: bool
    source: str
    evidence: str
    firmware_verified: bool
    note: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "driver": self.driver,
            "status": self.status,
            "can_list": self.can_list,
            "can_read": self.can_read,
            "source": self.source,
            "evidence": self.evidence,
            "firmware_verified": self.firmware_verified,
            "note": self.note,
        }


STOCK_CAPABILITY = SdCapability(
    driver="mks_wifi_stock",
    status="unsupported",
    can_list=True,
    can_read=False,
    source="makerbase_mks_wifi_public_source",
    evidence="source_confirmed_not_device_verified",
    firmware_verified=False,
    note=(
        "M20 lists files. Stock MKS WiFi does not expose a verified remote SD byte-read "
        "endpoint. The ESP TRANSFER_GET_FILE state sends HTTP-upload bytes to the MCU; "
        "it is not a file download. The installed Elegoo firmware is unverified."
    ),
)


def validate_sd_name(filename: str) -> str:
    """Only basename G-code files; paths and firmware command injection are forbidden."""
    if not isinstance(filename, str) or not SAFE_NAME.fullmatch(filename) or ".." in filename:
        raise SdReadError("Use a root-level .gcode, .gco, .gc or .g filename with no path")
    return filename


class ChunkedSdReader(Protocol):
    """Contract for a future independently verified *read-only* firmware extension.

    Implementations MUST NOT open a second MKS TCP connection while Observatory or
    OctoPrint owns it. A transport adapter must serialize through the existing
    printer session and refuse reads during printing, upload or unknown state.
    """

    async def stat(self, filename: str) -> tuple[int, str]:
        """Return byte length and an immutable per-file read token."""

    async def read(self, filename: str, offset: int, count: int, token: str) -> bytes:
        """Return bounded bytes for the token and offset. No printer writes."""


async def collect_sd_file(
    reader: ChunkedSdReader,
    filename: str,
    *,
    max_bytes: int = MAX_SD_BYTES,
    chunk_size: int = MAX_SD_CHUNK_BYTES,
    timeout_seconds: float = 30.0,
) -> tuple[bytes, str]:
    """Reassemble a verified test/extension transport, with strict byte accounting."""
    validate_sd_name(filename)
    if not 1 <= max_bytes <= MAX_SD_BYTES or not 1 <= chunk_size <= MAX_SD_CHUNK_BYTES:
        raise SdReadError("Invalid bounded SD transfer configuration")
    if not 0 < timeout_seconds <= 120:
        raise SdReadError("Invalid SD transfer timeout")

    max_chunks = (max_bytes + chunk_size - 1) // chunk_size

    async def transfer() -> tuple[bytes, str]:
        length, token = await reader.stat(filename)
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or not 1 <= length <= max_bytes
            or not isinstance(token, str)
            or not 1 <= len(token) <= 128
        ):
            raise SdReadError("Firmware did not provide a valid bounded file snapshot")
        digest = hashlib.sha256()
        result = bytearray()
        for offset in range(0, length, chunk_size):
            if offset // chunk_size >= max_chunks:
                raise SdReadError("SD chunk limit exceeded")
            needed = min(chunk_size, length - offset)
            block = await reader.read(filename, offset, needed, token)
            if not isinstance(block, bytes) or len(block) != needed:
                raise SdReadError(f"Missing or invalid SD data at offset {offset}")
            result.extend(block)
            digest.update(block)
        final_length, final_token = await reader.stat(filename)
        if final_length != length or final_token != token:
            raise SdReadError("SD file changed during transfer")
        return bytes(result), digest.hexdigest()

    try:
        return await asyncio.wait_for(transfer(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise SdReadError("SD transfer deadline exceeded") from exc


def capability_for_device(*, firmware_verified: bool = False) -> dict[str, str | bool]:
    """Never advertise remote read support based on M115 or an M20 listing alone."""
    payload = STOCK_CAPABILITY.as_dict()
    if firmware_verified:
        payload["note"] += " No validated read extension has been installed or negotiated."
    return payload
