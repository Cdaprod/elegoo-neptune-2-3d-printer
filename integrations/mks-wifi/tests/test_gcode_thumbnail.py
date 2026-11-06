from __future__ import annotations

import base64

from mks_wifi_bridge.gcode_thumbnail import extract_gcode_thumbnail

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/1eoAAAAASUVORK5CYII="
)


def _block(width: int, height: int, data: bytes = PNG_1X1) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"; thumbnail begin {width}x{height} {len(data)}\n;{encoded}\n; thumbnail end\n"


def test_extracts_largest_browser_supported_orca_thumbnail() -> None:
    gcode = "G90\n" + _block(32, 32) + _block(300, 300) + "G1 X4\n"
    assert extract_gcode_thumbnail(gcode.encode()) == ("image/png", PNG_1X1)


def test_ignores_malformed_oversized_and_unsupported_thumbnail_blocks() -> None:
    malformed = "; thumbnail begin 64x64 10\n;@@not-base64@@\n; thumbnail end\n"
    unsupported = _block(100, 100, b"QOI" + bytes(32))
    oversized = "; thumbnail begin 5000x5000 2\n;AA==\n; thumbnail end\n"
    assert extract_gcode_thumbnail((malformed + unsupported + oversized).encode()) is None


def test_requires_a_complete_embedded_thumbnail_block() -> None:
    encoded = base64.b64encode(PNG_1X1).decode("ascii")
    assert (
        extract_gcode_thumbnail(f"; thumbnail begin 1x1 {len(PNG_1X1)}\n;{encoded}\n".encode())
        is None
    )
