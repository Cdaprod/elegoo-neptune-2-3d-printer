"""Read-only scanner for a local OrcaSlicer export folder or mounted volume."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .gcode_thumbnail import MAX_GCODE_BYTES, extract_gcode_thumbnail_details
from .motion_timeline import MotionTimeline

logger = logging.getLogger(__name__)
MAX_FILES_PER_SCAN = 2000
MAX_CACHED_PREVIEWS = 200
SCAN_TIME_LIMIT_SECONDS = 20.0
MAX_METADATA_HEADER_BYTES = 512 * 1024
SAFE_FIRMWARE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,199}\.gcode", re.I)
METADATA_PATTERNS = {
    "estimated_print_time": re.compile(
        r"^;\s*(?:estimated printing time(?: \(normal mode\))?|TIME)\s*[=:]\s*(.+)$", re.I
    ),
    "filament_used_mm": re.compile(r"^;\s*filament used \[mm\]\s*=\s*([\d.]+)", re.I),
    "layer_height_mm": re.compile(r"^;\s*layer_height\s*=\s*([\d.]+)", re.I),
    "first_layer_height_mm": re.compile(r"^;\s*first_layer_height\s*=\s*([\d.]+)", re.I),
}


def _header_metadata(data: bytes) -> dict[str, str | float]:
    text = data[:MAX_METADATA_HEADER_BYTES].decode("utf-8", errors="ignore")
    metadata: dict[str, str | float] = {}
    for line in text.splitlines():
        for key, pattern in METADATA_PATTERNS.items():
            match = pattern.match(line.strip())
            if not match or key in metadata:
                continue
            value = match.group(1).strip()
            if key.endswith("_mm"):
                try:
                    metadata[key] = float(value)
                except ValueError:
                    continue
            else:
                metadata[key] = value[:120]
    return metadata


def firmware_filename_key(filename: str) -> str | None:
    """Return a conservative M20/M23 comparison key for root-level G-code names."""
    if not SAFE_FIRMWARE_NAME.fullmatch(filename) or ".." in filename:
        return None
    return filename.casefold()


class LocalGcodeIndexer:
    """Index embedded thumbnails while never writing to the configured source."""

    def __init__(
        self,
        source_dir: str | Path | None,
        timeline: MotionTimeline,
        *,
        require_mount: bool = False,
        time_limit_seconds: float = SCAN_TIME_LIMIT_SECONDS,
    ) -> None:
        self.source_dir = Path(source_dir) if source_dir else None
        self.timeline = timeline
        self.require_mount = require_mount
        self.time_limit_seconds = time_limit_seconds
        self.last_status: dict[str, Any] = {
            "status": "disabled" if self.source_dir is None else "not_scanned",
            "files_seen": 0,
            "previews_updated": 0,
            "collisions": 0,
            "error": None,
        }

    def scan(self) -> dict[str, Any]:
        root = self.source_dir
        if root is None:
            return self._cached_status("disabled", "Local G-code indexing is not configured.")
        try:
            if not root.is_dir() or (self.require_mount and not os.path.ismount(root)):
                return self._cached_status(
                    "unavailable",
                    "Configured G-code source is unavailable; cached previews were retained.",
                )
            root_real = root.resolve(strict=True)
        except OSError as exc:
            return self._cached_status(
                "unavailable", f"Configured G-code source is unavailable: {exc}"
            )

        started = time.monotonic()
        paths: list[Path] = []
        truncated = False
        for directory, dirnames, filenames in os.walk(root_real, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames if not (Path(directory) / name).is_symlink()
            )
            for filename in sorted(filenames):
                candidate = Path(directory) / filename
                if candidate.suffix.lower() != ".gcode" or candidate.is_symlink():
                    continue
                paths.append(candidate)
                if (
                    len(paths) >= MAX_FILES_PER_SCAN
                    or time.monotonic() - started > self.time_limit_seconds
                ):
                    truncated = True
                    break
            if truncated:
                break

        records: list[dict[str, Any]] = []
        previews_updated = 0
        skipped = 0
        for candidate in paths:
            if time.monotonic() - started > self.time_limit_seconds:
                truncated = True
                break
            try:
                info = candidate.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_GCODE_BYTES:
                    skipped += 1
                    continue
                relative = candidate.relative_to(root_real).as_posix()
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    skipped += 1
                    continue
                cached = self.timeline.get_local_gcode_record(relative)
                if (
                    cached
                    and cached["source_size_bytes"] == info.st_size
                    and cached["source_mtime_ns"] == info.st_mtime_ns
                    and cached["source_ctime_ns"] == info.st_ctime_ns
                ):
                    records.append(cached | {"source_present": True})
                    continue
                content = candidate.read_bytes()
                after = candidate.stat(follow_symlinks=False)
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ):
                    skipped += 1
                    continue
                thumbnail = extract_gcode_thumbnail_details(content)
                fingerprint = hashlib.sha256(content).hexdigest()
                record = {
                    "relative_path": relative,
                    "filename": candidate.name,
                    "firmware_key": (
                        firmware_filename_key(candidate.name) if "/" not in relative else None
                    ),
                    "source_fingerprint": fingerprint,
                    "source_size_bytes": info.st_size,
                    "source_mtime_ns": info.st_mtime_ns,
                    "source_ctime_ns": info.st_ctime_ns,
                    "mime_type": thumbnail.mime_type if thumbnail else None,
                    "image": thumbnail.image if thumbnail else None,
                    "thumbnail_width": thumbnail.width if thumbnail else None,
                    "thumbnail_height": thumbnail.height if thumbnail else None,
                    "metadata": _header_metadata(content),
                    "preview_available": thumbnail is not None,
                }
                if not self.timeline.save_local_gcode_record(record):
                    skipped += 1
                    continue
                records.append(record | {"image": None, "source_present": True})
                previews_updated += int(thumbnail is not None)
            except (OSError, ValueError) as exc:
                logger.info("Skipping unreadable local G-code file %s: %s", candidate.name, exc)
                skipped += 1

        # Keep all persistent metadata visible even when a bounded scan reaches its cap.
        if truncated:
            seen = {record["relative_path"] for record in records}
            records.extend(
                row
                for row in self.timeline.list_local_gcode_records()
                if row["relative_path"] not in seen
            )
        name_counts = Counter(
            key
            for record in records
            if record.get("source_present", True)
            and (key := firmware_filename_key(record["filename"])) is not None
        )
        for record in records:
            key = firmware_filename_key(record["filename"])
            record["local_collision"] = key is None or (
                record.get("source_present", True) and name_counts[key] > 1
            )
            record.pop("image", None)

        if not truncated:
            present = {
                record["relative_path"] for record in records if record.get("source_present", True)
            }
            self.timeline.mark_local_gcode_sources(present)

        status = {
            "status": "partial" if truncated or skipped else "available",
            "volume_available": True,
            "source": "read_only_local_gcode_source",
            "indexed_at": self.timeline.now_iso(),
            "files_seen": len(paths),
            "files_with_previews": sum(bool(record.get("preview_available")) for record in records),
            "previews_updated": previews_updated,
            "collisions": sum(bool(record.get("local_collision")) for record in records),
            "skipped": skipped,
            "truncated": truncated,
            "error": None,
            "files": records,
        }
        self.timeline.save_local_index_state(
            {key: value for key, value in status.items() if key not in {"files", "error"}}
        )
        self.last_status = status
        return status

    def cached_status(self, *, volume_available: bool = False) -> dict[str, Any]:
        return self._cached_status(
            "cached" if self.source_dir else "disabled",
            None
            if not self.source_dir
            else "Showing persistent local index; source volume is not currently scanned.",
            volume_available=volume_available,
        )

    def _cached_status(
        self, status: str, error: str | None, *, volume_available: bool = False
    ) -> dict[str, Any]:
        files = self.timeline.list_local_gcode_records()
        # Keep collision knowledge while the configured source is offline. A
        # stale cache must not become a false unique match merely because its
        # source files cannot currently be inspected.
        keys = [
            key
            for record in files
            if (key := firmware_filename_key(record["filename"])) is not None
        ]
        counts = Counter(keys)
        for record in files:
            key = firmware_filename_key(record["filename"])
            record["local_collision"] = key is None or counts[key] > 1
            record.pop("image", None)
        value = {
            "status": status,
            "volume_available": volume_available,
            "source": "persistent_local_cache",
            "indexed_at": self.timeline.get_local_index_state().get("indexed_at")
            or self.timeline.local_index_updated_at(),
            "files_seen": self.timeline.get_local_index_state().get("files_seen", 0),
            "files_with_previews": sum(bool(record.get("preview_available")) for record in files),
            "previews_updated": 0,
            "collisions": sum(bool(record.get("local_collision")) for record in files),
            "skipped": self.timeline.get_local_index_state().get("skipped", 0),
            "truncated": self.timeline.get_local_index_state().get("truncated", False),
            "error": error,
            "files": files,
        }
        self.last_status = value
        return value

    def match_entries(
        self, entries: list[dict[str, Any]], local_status: dict[str, Any]
    ) -> list[dict[str, Any]]:
        local_by_key: dict[str, list[dict[str, Any]]] = {}
        local_name_counts: Counter[str] = Counter()
        source_available = bool(local_status.get("volume_available"))
        for record in local_status.get("files", []):
            key = record.get("firmware_key")
            if key and (record.get("source_present", False) or not source_available):
                local_by_key.setdefault(key, []).append(record)
            basename_key = firmware_filename_key(record.get("filename", ""))
            if basename_key and (record.get("source_present", False) or not source_available):
                local_name_counts[basename_key] += 1
        printer_counts = Counter(
            key
            for entry in entries
            if entry.get("kind") == "file"
            and (key := firmware_filename_key(str(entry.get("name", "")))) is not None
        )
        output: list[dict[str, Any]] = []
        for entry in entries:
            result = dict(entry)
            key = firmware_filename_key(str(entry.get("name", "")))
            candidates = local_by_key.get(key, []) if key else []
            if key and local_name_counts[key] > 1:
                result["local_preview"] = False
                result["preview_source"] = (
                    "observatory_cache" if result.get("preview_available") else "none"
                )
                result["local_metadata"] = None
                result["local_match"] = "ambiguous"
            elif (
                key
                and printer_counts[key] == 1
                and len(candidates) == 1
                and not candidates[0].get("local_collision")
            ):
                local = candidates[0]
                result["local_preview"] = bool(local.get("preview_available"))
                result["preview_available"] = bool(local.get("preview_available")) or bool(
                    result.get("preview_available")
                )
                result["preview_source"] = (
                    "cached_local_gcode" if local.get("preview_available") else "none"
                )
                result["preview_stale"] = not source_available
                result["local_metadata"] = {k: v for k, v in local.items() if k not in {"image"}}
                result["local_metadata"]["source_current"] = source_available
                result["local_match"] = "unambiguous"
            else:
                result["local_preview"] = False
                result["preview_source"] = (
                    "observatory_cache" if result.get("preview_available") else "none"
                )
                result["local_metadata"] = None
                result["local_match"] = (
                    "ambiguous" if candidates or (key and printer_counts[key] > 1) else "unmatched"
                )
            output.append(result)
        return output
