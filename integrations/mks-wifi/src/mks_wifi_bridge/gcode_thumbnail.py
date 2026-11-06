"""Extract the embedded PNG/JPEG preview used by OrcaSlicer G-code exports."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

MAX_THUMBNAIL_BYTES = 512 * 1024
MAX_THUMBNAIL_DIMENSION = 1024
_BEGIN = re.compile(r"^\s*;\s*thumbnail\s+begin\s+(\d+)x(\d+)(?:\s+\d+)?\s*$", re.I)
_END = re.compile(r"^\s*;\s*thumbnail\s+end\s*$", re.I)
MAX_GCODE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class GcodeThumbnail:
    mime_type: str
    image: bytes
    width: int
    height: int


def extract_gcode_thumbnail(gcode: bytes) -> tuple[str, bytes] | None:
    """Return the largest supported embedded preview, if present and valid.

    OrcaSlicer stores preview images as base64 comments between ``thumbnail
    begin`` and ``thumbnail end`` lines. Only browser-displayable PNG and JPEG
    payloads are accepted; unsupported or malformed blocks are ignored.
    """
    thumbnail = extract_gcode_thumbnail_details(gcode)
    return (thumbnail.mime_type, thumbnail.image) if thumbnail else None


def extract_gcode_thumbnail_details(gcode: bytes) -> GcodeThumbnail | None:
    """Extract image and declared dimensions for persistent local indexing."""
    if len(gcode) > MAX_GCODE_BYTES:
        return None
    try:
        text = gcode.decode("ascii", errors="ignore")
    except Exception:
        return None

    candidates: list[tuple[int, GcodeThumbnail]] = []
    lines = iter(text.splitlines())
    for line in lines:
        match = _BEGIN.match(line)
        if not match:
            continue
        width, height = map(int, match.groups())
        if not (1 <= width <= MAX_THUMBNAIL_DIMENSION and 1 <= height <= MAX_THUMBNAIL_DIMENSION):
            continue
        encoded: list[str] = []
        encoded_size = 0
        closed = False
        for image_line in lines:
            if _END.match(image_line):
                closed = True
                break
            payload = image_line.strip()
            if payload.startswith(";"):
                payload = payload[1:].strip()
            encoded_size += len(payload)
            if encoded_size > MAX_THUMBNAIL_BYTES * 2:
                encoded = []
                break
            encoded.append(payload)
        if not encoded or not closed:
            continue
        try:
            image_data = base64.b64decode("".join(encoded), validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(image_data) > MAX_THUMBNAIL_BYTES:
            continue
        if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime_type = "image/png"
        elif image_data.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        else:
            continue
        candidates.append((width * height, GcodeThumbnail(mime_type, image_data, width, height)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]
