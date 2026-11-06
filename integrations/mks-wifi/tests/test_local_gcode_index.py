from __future__ import annotations

import base64
import os
from pathlib import Path

from mks_wifi_bridge.local_gcode_index import LocalGcodeIndexer
from mks_wifi_bridge.motion_timeline import MotionTimeline

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/1eoAAAAASUVORK5CYII="
)


def _gcode(name: str = "back-dso138.gcode", *, layer_height: float = 0.2) -> bytes:
    encoded = base64.b64encode(PNG).decode("ascii")
    return (
        "; estimated printing time (normal mode) = 12m 30s\n"
        "; filament used [mm] = 321.5\n"
        f"; layer_height = {layer_height}\n"
        "; THUMBNAIL_BLOCK_START\n"
        "; thumbnail begin 320x320 68\n"
        f";{encoded}\n"
        "; thumbnail end\n"
        "; THUMBNAIL_BLOCK_END\n"
        "G90\nG1 X1 F1200\n"
    ).encode()


def test_indexes_thumbnail_metadata_read_only_and_matches_live_m20(tmp_path: Path) -> None:
    source = tmp_path / "volume"
    source.mkdir()
    file_path = source / "back-dso138.gcode"
    original = _gcode()
    file_path.write_bytes(original)
    before = file_path.stat()
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    # A normal OrcaSlicer export folder works without being a mounted volume.
    indexer = LocalGcodeIndexer(source, timeline)

    indexed = indexer.scan()
    assert indexed["status"] == "available"
    assert indexed["files_with_previews"] == 1
    record = indexed["files"][0]
    assert record["relative_path"] == "back-dso138.gcode"
    assert record["thumbnail_width"] == 320
    assert record["thumbnail_height"] == 320
    assert record["metadata"]["estimated_print_time"] == "12m 30s"
    assert record["metadata"]["filament_used_mm"] == 321.5
    assert record["metadata"]["layer_height_mm"] == 0.2
    assert file_path.read_bytes() == original
    assert (file_path.stat().st_size, file_path.stat().st_mtime_ns) == (
        before.st_size,
        before.st_mtime_ns,
    )

    entries = indexer.match_entries(
        [{"name": "BACK-DSO138.GCODE", "kind": "file", "size_bytes": None}], indexed
    )
    assert entries[0]["preview_available"] is True
    assert entries[0]["preview_source"] == "cached_local_gcode"
    assert entries[0]["local_match"] == "unambiguous"
    assert entries[0]["local_metadata"]["metadata"]["layer_height_mm"] == 0.2
    assert (
        indexer.match_entries(
            [
                {"name": "back-dso138.gcode", "kind": "file"},
                {"name": "BACK-DSO138.GCODE", "kind": "file"},
            ],
            indexed,
        )[0]["local_match"]
        == "ambiguous"
    )


def test_changed_file_replaces_metadata_and_removes_obsolete_thumbnail(tmp_path: Path) -> None:
    source = tmp_path / "volume"
    source.mkdir()
    file_path = source / "changed.gcode"
    file_path.write_bytes(_gcode())
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    indexer = LocalGcodeIndexer(source, timeline, require_mount=False)
    assert indexer.scan()["files"][0]["preview_available"] is True

    file_path.write_text("; layer_height = 0.3\nG1 X2 F600\n")
    os.utime(file_path, ns=(file_path.stat().st_atime_ns, file_path.stat().st_mtime_ns + 1_000_000))
    updated = indexer.scan()["files"][0]
    assert updated["preview_available"] is False
    assert updated["metadata"]["layer_height_mm"] == 0.3
    assert timeline.get_unique_local_gcode_preview("changed.gcode") is None


def test_filename_collisions_never_match_printer_but_local_previews_remain_visible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "volume"
    (source / "a").mkdir(parents=True)
    (source / "b").mkdir()
    (source / "a" / "same.gcode").write_bytes(_gcode())
    (source / "b" / "SAME.GCODE").write_bytes(_gcode(layer_height=0.25))
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    indexer = LocalGcodeIndexer(source, timeline, require_mount=False)

    indexed = indexer.scan()
    assert indexed["collisions"] == 2
    assert all(row["local_collision"] for row in indexed["files"])
    matched = indexer.match_entries([{"name": "same.gcode", "kind": "file"}], indexed)[0]
    assert matched["local_match"] == "ambiguous"
    assert matched["local_preview"] is False
    assert timeline.get_unique_local_gcode_preview("same.gcode") is None
    assert timeline.get_local_gcode_preview_by_path("a/same.gcode") == ("image/png", PNG)


def test_disconnected_volume_retains_cached_preview_without_claiming_printer_presence(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "volume"
    source.mkdir()
    (source / "offline.gcode").write_bytes(_gcode("offline.gcode"))
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    indexer = LocalGcodeIndexer(source, timeline, require_mount=False)
    indexer.scan()

    monkeypatch.setattr("mks_wifi_bridge.local_gcode_index.os.path.ismount", lambda _path: False)
    indexer.require_mount = True
    cached = indexer.scan()
    assert cached["status"] == "unavailable"
    assert cached["volume_available"] is False
    assert cached["files_with_previews"] == 1
    assert cached["files"][0]["source_present"] is True
    assert timeline.get_unique_local_gcode_preview("offline.gcode") == ("image/png", PNG)


def test_offline_export_cache_matches_fresh_printer_name_as_stale_preview(
    tmp_path: Path,
) -> None:
    source = tmp_path / "orca-exports"
    source.mkdir()
    (source / "part.gcode").write_bytes(_gcode("part.gcode"))
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    indexer = LocalGcodeIndexer(source, timeline)
    indexer.scan()

    indexer.source_dir = tmp_path / "temporarily-unavailable"
    offline = indexer.scan()
    matched = indexer.match_entries([{"name": "part.gcode", "kind": "file"}], offline)[0]

    assert offline["volume_available"] is False
    assert matched["preview_available"] is True
    assert matched["preview_stale"] is True
    assert matched["local_match"] == "unambiguous"
    assert timeline.get_unique_local_gcode_preview("part.gcode") == ("image/png", PNG)


def test_symlinked_gcode_files_are_not_read(tmp_path: Path) -> None:
    source = tmp_path / "volume"
    outside = tmp_path / "outside.gcode"
    source.mkdir()
    outside.write_bytes(_gcode())
    try:
        (source / "link.gcode").symlink_to(outside)
    except OSError:
        return
    indexer = LocalGcodeIndexer(
        source, MotionTimeline(tmp_path / "timeline.sqlite3"), require_mount=False
    )

    indexed = indexer.scan()

    assert indexed["files_seen"] == 0
    assert indexed["files"] == []
