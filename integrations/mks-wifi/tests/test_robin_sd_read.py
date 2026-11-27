"""Emulated, read-only SD file-transfer protocol contract tests."""

from __future__ import annotations

import asyncio

import pytest

from mks_wifi_bridge.robin_sd_read import (
    SdReadError,
    capability_for_device,
    collect_sd_file,
    validate_sd_name,
)


class MemorySd:
    def __init__(self, data: bytes, *, fail_at: int | None = None, change=False):
        self.data = data
        self.fail_at = fail_at
        self.change = change
        self.stats = 0
        self.reads = []

    async def stat(self, filename):
        self.stats += 1
        return len(self.data), "snapshot-1" if self.stats == 1 or not self.change else "snapshot-2"

    async def read(self, filename, offset, count, token):
        self.reads.append((filename, offset, count, token))
        if offset == self.fail_at:
            return self.data[offset : offset + count - 1]
        return self.data[offset : offset + count]


def run(task):
    return asyncio.run(task)


def test_stock_wifi_never_claims_arbitrary_file_reads():
    c = capability_for_device()
    assert c["can_list"] is True
    assert c["can_read"] is False
    assert c["status"] == "unsupported"
    assert c["firmware_verified"] is False


def test_chunked_sd_read_roundtrip():
    original = b"G90\nM82\nG1 X10 Y10 E1\n" * 345
    transport = MemorySd(original)
    data, fingerprint = run(collect_sd_file(transport, "BRACKET.GCO", chunk_size=512))
    import hashlib

    assert data == original
    assert fingerprint == hashlib.sha256(original).hexdigest()
    assert transport.reads[0] == ("BRACKET.GCO", 0, 512, "snapshot-1")
    assert transport.reads[-1][1] + transport.reads[-1][2] == len(original)


@pytest.mark.parametrize(
    "name", ["../config.gcode", "folder/test.gcode", "M24\n.gcode", "", "data.txt"]
)
def test_untrusted_filename_rejected(name):
    with pytest.raises(SdReadError):
        validate_sd_name(name)


def test_truncated_chunk_refused():
    with pytest.raises(SdReadError, match="offset"):
        run(collect_sd_file(MemorySd(b"G1 X0" * 100, fail_at=128), "PART.gcode", chunk_size=128))


def test_modified_file_snapshot_refused():
    with pytest.raises(SdReadError, match="changed"):
        run(collect_sd_file(MemorySd(b"G1 X0", change=True), "PART.gcode"))


def test_zero_length_and_oversize_rejected():
    with pytest.raises(SdReadError, match="bounded"):
        run(collect_sd_file(MemorySd(b""), "EMPTY.gcode"))
    with pytest.raises(SdReadError, match="bounded"):
        run(collect_sd_file(MemorySd(b"x" * 1024), "BIG.gcode", max_bytes=100))


def test_invalid_chunk_size_rejected():
    with pytest.raises(SdReadError):
        run(collect_sd_file(MemorySd(b"G1 X0"), "TEST.gco", chunk_size=4097))


def test_exact_bytes_and_no_printer_motion_side_effect():
    transport = MemorySd(b"G1 X1\n")
    data, sha = run(collect_sd_file(transport, "TEST.gco"))
    assert data == b"G1 X1\n"
    assert len(sha) == 64
    assert transport.stats == 2
    assert all(call[0] == "TEST.gco" for call in transport.reads)
