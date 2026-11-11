"""Backend-persisted, explicitly estimated G-code and print motion history."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)")
MAX_GCODE_BYTES = 50 * 1024 * 1024
MAX_SEGMENTS = 80_000
RETENTION_DAYS = 30
PRINT_HISTORY_RETENTION_DAYS = 365
MAX_TELEMETRY_ROWS = 100_000
MAX_STORED_GCODE_FILES = 8
MAX_STORED_SEGMENTS = 200_000
MAX_OPERATION_EVENTS = 100
MAX_FEEDRATE_SAMPLES = 1200
OFFLINE_DEMO_GCODE = """; Neptune Observatory built-in offline motion demonstration
G21
G90
M83
G28 X Y Z
G1 X45 Y20 E2 F1200
G4 P30000
G1 X85 Y20 E2 F1800
G4 P30000
G1 X85 Y70 E2 F900
G4 P30000
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _words(line: str) -> dict[str, float]:
    return {key: float(value) for key, value in WORD.findall(line)}


def _motion_timing(
    distance_mm: float, feed_mm_min: float, acceleration_mm_s2: float | None
) -> tuple[float, list[dict[str, float]], str]:
    """Return a start/stop velocity profile; do not assume planner look-ahead."""
    speed = max(feed_mm_min / 60.0, 0.1)
    if acceleration_mm_s2 is None or acceleration_mm_s2 <= 0:
        duration = distance_mm / speed
        profile = [
            {"offset_seconds": 0.0, "feedrate_mm_min": 0.0},
            {"offset_seconds": 0.0, "feedrate_mm_min": feed_mm_min},
            {"offset_seconds": duration, "feedrate_mm_min": 0.0},
        ]
        return duration, profile, "linear_feed_estimate_no_acceleration"

    accel = acceleration_mm_s2
    ramp_distance = speed * speed / accel
    if distance_mm <= ramp_distance:
        peak = math.sqrt(distance_mm * accel)
        ramp_time = peak / accel
        duration = 2.0 * ramp_time
        peak_feed = peak * 60.0
        profile = [
            {"offset_seconds": 0.0, "feedrate_mm_min": 0.0},
            {"offset_seconds": ramp_time, "feedrate_mm_min": peak_feed},
            {"offset_seconds": duration, "feedrate_mm_min": 0.0},
        ]
    else:
        ramp_time = speed / accel
        duration = 2.0 * ramp_time + (distance_mm - ramp_distance) / speed
        cruise_end = duration - ramp_time
        profile = [
            {"offset_seconds": 0.0, "feedrate_mm_min": 0.0},
            {"offset_seconds": ramp_time, "feedrate_mm_min": feed_mm_min},
            {"offset_seconds": cruise_end, "feedrate_mm_min": feed_mm_min},
            {"offset_seconds": duration, "feedrate_mm_min": 0.0},
        ]
    return duration, profile, "trapezoidal_estimate_from_gcode_acceleration"


def _profile_value(segment: dict[str, Any], elapsed: float) -> float:
    if segment.get("acceleration_mm_s2") is None:
        if elapsed <= 0 or elapsed >= float(segment.get("duration_seconds", 0.0)):
            return 0.0
        return float(segment.get("feed_mm_min", 0.0))
    profile = segment.get("feedrate_profile") or []
    if not profile:
        return 0.0
    if elapsed <= profile[0]["offset_seconds"]:
        return float(profile[0]["feedrate_mm_min"])
    for left, right in zip(profile, profile[1:], strict=False):
        if elapsed <= right["offset_seconds"]:
            span = right["offset_seconds"] - left["offset_seconds"]
            fraction = 1.0 if span <= 0 else (elapsed - left["offset_seconds"]) / span
            return (
                float(left["feedrate_mm_min"])
                + (float(right["feedrate_mm_min"]) - float(left["feedrate_mm_min"])) * fraction
            )
    return float(profile[-1]["feedrate_mm_min"])


def _axis_feedrates(segment: dict[str, Any], feedrate: float) -> dict[str, float]:
    """Allocate planned path feedrate to axes by their share of path distance."""
    deltas = segment.get("axis_deltas_mm") or {}
    distance = float(segment.get("path_distance_mm", segment.get("distance_mm", 0.0)) or 0.0)
    if distance <= 1e-9:
        return dict.fromkeys("xyze", 0.0)
    return {
        axis: round(abs(float(deltas.get(axis, 0.0) or 0.0)) / distance * feedrate, 3)
        for axis in "xyze"
    }


def _feedrate_for_samples(
    samples: list[dict[str, Any]], segments: list[dict[str, Any]], duration: float
) -> list[dict[str, Any]]:
    if not samples or not segments or duration <= 0:
        return []
    ends = [float(segment["end_seconds"]) for segment in segments]
    rows: list[dict[str, Any]] = []
    for sample in samples:
        state = str(sample.get("state") or "").lower()
        observed_at = sample.get("observed_at")
        if not observed_at:
            continue
        if state in {"idle", "paused", "complete", "completed"}:
            rows.append(
                {
                    "observed_at": observed_at,
                    "feedrate_mm_min": 0.0,
                    "axis_feedrates_mm_min": dict.fromkeys("xyze", 0.0),
                    "source": "firmware_reported_state",
                    "data_class": "firmware_reported_state_not_motor_feedback",
                    "confidence": "no_print_motion_reported",
                }
            )
            continue
        progress = sample.get("progress")
        if state != "printing" or not isinstance(progress, (int, float)):
            continue
        elapsed = duration * max(0.0, min(100.0, float(progress))) / 100.0
        index = bisect.bisect_left(ends, elapsed)
        if index >= len(segments):
            feedrate = 0.0
            axis_rates = dict.fromkeys("xyze", 0.0)
        else:
            segment = segments[index]
            feedrate = _profile_value(segment, elapsed - float(segment["start_seconds"]))
            axis_rates = _axis_feedrates(segment, feedrate)
        rows.append(
            {
                "observed_at": observed_at,
                "feedrate_mm_min": round(feedrate, 3),
                "axis_feedrates_mm_min": axis_rates,
                "source": "MKS_progress_mapped_to_G-code",
                "data_class": "estimated_planned_feedrate",
                "confidence": "progress_mapped_estimate",
                "progress_percent": progress,
            }
        )
    return rows


def _downsample_feedrate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound chart payloads while preserving local minima, maxima, and endpoints."""
    if len(rows) <= MAX_FEEDRATE_SAMPLES:
        return rows
    bucket_count = max(1, MAX_FEEDRATE_SAMPLES // 4)
    bucket_size = math.ceil(len(rows) / bucket_count)
    kept: set[int] = {0, len(rows) - 1}
    for start in range(0, len(rows), bucket_size):
        end = min(len(rows), start + bucket_size)
        values = [float(rows[index].get("feedrate_mm_min", 0.0)) for index in range(start, end)]
        kept.update(
            {
                start,
                end - 1,
                start + min(range(len(values)), key=values.__getitem__),
                start + max(range(len(values)), key=values.__getitem__),
            }
        )
    return [rows[index] for index in sorted(kept)]


def interpret_gcode(text: str, filename: str) -> dict[str, Any]:
    """Interpret the supported linear motion subset; home is a reference event only."""
    if not isinstance(text, str):
        raise TypeError("G-code content must be text")
    raw = text.encode("utf-8")
    if len(raw) > MAX_GCODE_BYTES:
        raise ValueError("G-code exceeds the 50 MB analysis limit")
    position = {axis: 0.0 for axis in "XYZE"}
    offsets = {axis: 0.0 for axis in "XYZE"}
    absolute_xyz = absolute_e = True
    scale, feed = 1.0, 1200.0
    axis_acceleration: dict[str, float | None] = dict.fromkeys("XYZE")
    print_acceleration: float | None = None
    travel_acceleration: float | None = None
    elapsed = 0.0
    homed: set[str] = set()
    segments: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    warnings: set[str] = set()
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        code = raw_line.split(";", 1)[0].strip().upper()
        if not code:
            continue
        command_match = re.match(r"(?:N\d+\s*)?([GMT]\d+(?:\.\d+)?)", code)
        if not command_match:
            continue
        command = command_match.group(1)
        values = _words(code)
        if command == "G20":
            scale = 25.4
        elif command == "G21":
            scale = 1.0
        elif command == "G90":
            absolute_xyz = True
        elif command == "G91":
            absolute_xyz = False
        elif command == "M82":
            absolute_e = True
        elif command == "M83":
            absolute_e = False
        elif command == "M201":
            for axis in "XYZE":
                if axis in values and values[axis] > 0:
                    axis_acceleration[axis] = values[axis]
        elif command == "M204":
            # Marlin variants use S for both, or P/T for print/travel.
            if values.get("S", 0) > 0:
                print_acceleration = travel_acceleration = values["S"]
            if values.get("P", 0) > 0:
                print_acceleration = values["P"]
            if values.get("T", 0) > 0:
                travel_acceleration = values["T"]
        elif command == "G28":
            axes = [axis for axis in "XYZ" if re.search(rf"\b{axis}(?:\s|$)", code)]
            axes = axes or list("XYZ")
            # A file's G28 is simulated only. Do not invent a traveled path.
            for axis in axes:
                homed.add(axis)
                position[axis] = offsets[axis]
            events.append(
                {
                    "kind": "simulated_home_reference",
                    "line": line_no,
                    "axes": [a.lower() for a in axes],
                    "time_seconds": elapsed,
                    "physical_homing_confirmed": False,
                }
            )
        elif command == "G92":
            reset = {}
            for axis in "XYZE":
                if axis in values:
                    logical = values[axis] * scale
                    offsets[axis] = position[axis] - logical
                    reset[axis.lower()] = logical
            events.append(
                {
                    "kind": "coordinate_reset",
                    "line": line_no,
                    "values": reset,
                    "time_seconds": elapsed,
                }
            )
        elif command == "G4":
            dwell = max(0.0, values.get("P", 0.0) / 1000.0, values.get("S", 0.0))
            if dwell:
                events.append(
                    {
                        "kind": "planned_idle_dwell",
                        "line": line_no,
                        "start_seconds": elapsed,
                        "end_seconds": elapsed + dwell,
                        "duration_seconds": dwell,
                        "feedrate_mm_min": 0.0,
                        "confidence": "gcode_dwell_command",
                    }
                )
                elapsed += dwell
        elif command in {"G0", "G1"}:
            start = dict(position)
            e_before = position["E"]
            for axis in "XYZE":
                if axis not in values:
                    continue
                delta = values[axis] * scale
                absolute = absolute_e if axis == "E" else absolute_xyz
                position[axis] = (delta + offsets[axis]) if absolute else position[axis] + delta
            if values.get("F", 0) > 0:
                feed = values["F"] * scale
            xyz = math.dist([start[a] for a in "XYZ"], [position[a] for a in "XYZ"])
            de = position["E"] - e_before
            if xyz < 1e-8 and abs(de) < 1e-8:
                continue
            distance = max(xyz, abs(de))
            kind = (
                "travel"
                if xyz > 1e-8 and de <= 0
                else "extrusion"
                if de > 1e-8
                else "retract"
                if de < -1e-8
                else "travel"
            )
            scalar_acceleration = travel_acceleration if kind == "travel" else print_acceleration
            axis_limits = [
                axis_acceleration[axis] / (abs(position[axis] - start[axis]) / distance)
                for axis in "XYZE"
                if distance > 0
                and axis_acceleration[axis] is not None
                and abs(position[axis] - start[axis]) > 1e-8
            ]
            acceleration = min(axis_limits) if axis_limits else scalar_acceleration
            if scalar_acceleration is not None:
                acceleration = (
                    min(acceleration, scalar_acceleration) if acceleration else scalar_acceleration
                )
            duration, profile, timing_model = _motion_timing(distance, feed, acceleration)
            segments.append(
                {
                    "line": line_no,
                    "kind": kind,
                    "from": {a.lower(): start[a] for a in "XYZE"},
                    "to": {a.lower(): position[a] for a in "XYZE"},
                    "axis_deltas_mm": {a.lower(): position[a] - start[a] for a in "XYZE"},
                    "feed_mm_min": feed,
                    "distance_mm": xyz,
                    "path_distance_mm": distance,
                    "extrusion_delta_mm": de,
                    "acceleration_mm_s2": acceleration,
                    "start_seconds": elapsed,
                    "end_seconds": elapsed + duration,
                    "duration_seconds": duration,
                    "velocity_mm_s": distance / duration if duration else 0.0,
                    "planned_feedrate_mm_min": distance / duration * 60 if duration else 0.0,
                    "feedrate_profile": profile,
                    "axis_velocities_mm_s": {
                        a.lower(): (position[a] - start[a]) / duration if duration else 0.0
                        for a in "XYZE"
                    },
                    "homed_axes": sorted(a.lower() for a in homed),
                    "reference_kind": "simulated_gcode_home"
                    if homed
                    else "unreferenced_gcode_coordinates",
                    "confidence": timing_model,
                }
            )
            elapsed += duration
            if len(segments) >= MAX_SEGMENTS:
                warnings.add(f"Analysis stopped at {MAX_SEGMENTS} motion segments.")
                break
        elif command in {"G2", "G3"}:
            warnings.add("Arc moves G2/G3 are not included in the linear motion timeline.")
    if not segments:
        warnings.add("No supported G0/G1 motion segments were found.")
    return {
        "filename": Path(filename).name[:240],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "segments": segments,
        "events": events,
        "estimated_duration_seconds": elapsed,
        "warnings": sorted(warnings),
        "source": "uploaded_gcode_analysis_only",
    }


class MotionTimeline:
    """Small SQLite timeline with bounded telemetry and expiring sessions."""

    def __init__(self, path: str | Path = "/data/neptune-observatory.db") -> None:
        self.path = str(path)
        self.storage_error: str | None = None
        self.lock = threading.RLock()
        self._segment_cache: dict[str, list[dict[str, Any]]] = {}
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Local development/test environments may not mount Docker's /data volume.
            self.path = str(Path.cwd() / ".localdata" / "neptune-observatory.db")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._db() as db:
                db.executescript(
                    """
                CREATE TABLE IF NOT EXISTS gcode_files(
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, duration REAL NOT NULL, warnings TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS segments(
                    file_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(file_id, seq),
                    FOREIGN KEY(file_id) REFERENCES gcode_files(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS gcode_events(
                    file_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(file_id, seq),
                    FOREIGN KEY(file_id) REFERENCES gcode_files(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS print_sessions(
                    id TEXT PRIMARY KEY, filename TEXT, started_at TEXT NOT NULL,
                    ended_at TEXT, state TEXT NOT NULL, progress REAL, file_id TEXT,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telemetry(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                    observed_at TEXT NOT NULL, state TEXT, filename TEXT, progress REAL,
                    nozzle REAL, nozzle_target REAL, bed REAL, bed_target REAL,
                    latency_ms REAL,
                    FOREIGN KEY(session_id) REFERENCES print_sessions(id)
                );
                CREATE TABLE IF NOT EXISTS operation_events(
                    id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, macro_id TEXT,
                    state TEXT, commands TEXT, command_results TEXT,
                    transport_acknowledged INTEGER, physical_completion_confirmed INTEGER,
                    operator_observation TEXT, error TEXT, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS machine_state_events(
                    id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
                    source TEXT NOT NULL, data_class TEXT NOT NULL, confidence TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sd_previews(
                    filename TEXT PRIMARY KEY COLLATE NOCASE, mime_type TEXT NOT NULL,
                    image BLOB NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_gcode_previews(
                    relative_path TEXT PRIMARY KEY, filename TEXT NOT NULL,
                    firmware_key TEXT, source_fingerprint TEXT NOT NULL,
                    source_size_bytes INTEGER NOT NULL, source_mtime_ns INTEGER NOT NULL,
                    source_ctime_ns INTEGER NOT NULL,
                    mime_type TEXT, image BLOB, thumbnail_width INTEGER, thumbnail_height INTEGER,
                    metadata_json TEXT NOT NULL, preview_available INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL, source_present INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS local_gcode_firmware_key
                    ON local_gcode_previews(firmware_key);
                CREATE TABLE IF NOT EXISTS local_gcode_index_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS machine_state_time ON machine_state_events(observed_at);
                CREATE INDEX IF NOT EXISTS telemetry_session_time
                    ON telemetry(session_id, observed_at);
                """
                )
                self._prune(db)
        except sqlite3.Error as exc:
            self.storage_error = f"Timeline database initialization failed: {exc}"

    def storage_diagnostics(self) -> dict[str, Any]:
        """Return current persistent-history storage facts without background polling."""
        path = Path(self.path)
        persistent_root = Path("/data")
        try:
            parent = path.parent.resolve()
            data_root = persistent_root.resolve()
            persistent = parent == data_root or data_root in parent.parents
            usage = shutil.disk_usage(parent)
            database_bytes = path.stat().st_size if path.exists() else 0
        except OSError as exc:
            return {
                "status": "unavailable",
                "persistent": False,
                "database_bytes": None,
                "free_bytes": None,
                "error": self.storage_error or str(exc),
            }

        free_bytes = usage.free
        status = "healthy"
        if self.storage_error:
            status = "unavailable"
        elif free_bytes < 100 * 1024 * 1024:
            status = "low_space"
        elif not persistent:
            status = "non_persistent"
        return {
            "status": status,
            "persistent": persistent,
            "database_bytes": database_bytes,
            "free_bytes": free_bytes,
            "minimum_free_bytes": 100 * 1024 * 1024,
            "error": self.storage_error,
        }

    @staticmethod
    def offline_demo() -> dict[str, Any]:
        """Return a fresh, simulated path interpreted by the shared G-code parser."""
        analysis = interpret_gcode(OFFLINE_DEMO_GCODE, "offline-motion-demo.gcode")
        duration = float(analysis["estimated_duration_seconds"])
        end = datetime.now(UTC)
        start = end - timedelta(seconds=duration)
        feedrate_history: list[dict[str, Any]] = []
        planned_history: list[dict[str, Any]] = []
        for segment in analysis["segments"]:
            for point in segment["feedrate_profile"]:
                when = start + timedelta(
                    seconds=float(segment["start_seconds"]) + float(point["offset_seconds"])
                )
                feedrate_history.append(
                    {
                        "observed_at": when.isoformat(),
                        "offset_seconds": float(segment["start_seconds"])
                        + float(point["offset_seconds"]),
                        "feedrate_mm_min": round(float(point["feedrate_mm_min"]), 3),
                        "axis_feedrates_mm_min": _axis_feedrates(
                            segment, float(point["feedrate_mm_min"])
                        ),
                        "source": "offline_gcode_plan",
                        "data_class": "simulated",
                        "confidence": segment["confidence"],
                        "line": segment["line"],
                    }
                )
            planned_history.append(
                {
                    "observed_at": (
                        start + timedelta(seconds=segment["start_seconds"])
                    ).isoformat(),
                    "offset_seconds": float(segment["start_seconds"]),
                    "axes": {
                        axis: {"status": "planned", "value_mm": value}
                        for axis, value in segment["from"].items()
                    },
                    "interval": {
                        "status": "planned",
                        "distance_mm": segment["distance_mm"],
                        "duration_seconds": segment["duration_seconds"],
                        "velocity_mm_s": segment["velocity_mm_s"],
                        "axis_deltas_mm": segment["axis_deltas_mm"],
                        "movement_axes": [
                            axis for axis in "xyz" if abs(segment["axis_deltas_mm"][axis]) > 1e-8
                        ],
                    },
                    "source": "offline_gcode_demo",
                    "confidence": segment["confidence"],
                    "line": segment["line"],
                    "kind": segment["kind"],
                }
            )
        for event in analysis["events"]:
            offset = float(event.get("time_seconds", event.get("start_seconds", 0.0)))
            feedrate_history.append(
                {
                    "observed_at": (start + timedelta(seconds=offset)).isoformat(),
                    "offset_seconds": offset,
                    "feedrate_mm_min": 0.0,
                    "axis_feedrates_mm_min": dict.fromkeys("xyze", 0.0),
                    "source": "offline_gcode_plan",
                    "data_class": "simulated_idle",
                    "confidence": event.get("confidence", "gcode_dwell_command"),
                    "line": event["line"],
                }
            )
            if event["kind"] == "planned_idle_dwell":
                offset = float(event["end_seconds"])
                feedrate_history.append(
                    {
                        "observed_at": (start + timedelta(seconds=offset)).isoformat(),
                        "offset_seconds": offset,
                        "feedrate_mm_min": 0.0,
                        "axis_feedrates_mm_min": dict.fromkeys("xyze", 0.0),
                        "source": "offline_gcode_plan",
                        "data_class": "simulated_idle",
                        "confidence": event.get("confidence", "gcode_dwell_command"),
                        "line": event["line"],
                    }
                )
        feedrate_history.sort(key=lambda row: row["offset_seconds"])
        events = [
            {
                **event,
                "observed_at": (
                    start
                    + timedelta(
                        seconds=float(event.get("time_seconds", event.get("start_seconds", 0.0)))
                    )
                ).isoformat(),
                "source": "offline_gcode_demo",
                "data_class": "simulated",
            }
            for event in analysis["events"]
        ]
        return {
            "filename": analysis["filename"],
            "gcode": OFFLINE_DEMO_GCODE,
            "segments": analysis["segments"],
            "events": events,
            "feedrate_history": feedrate_history,
            "planned_history": planned_history,
            "duration_seconds": duration,
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
            "source": "simulated_offline_fixture",
        }

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=3.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def save_sd_thumbnail(self, filename: str, mime_type: str, image_data: bytes) -> bool:
        """Persist a bounded G-code preview so SD listings can show its thumbnail."""
        if (
            mime_type not in {"image/png", "image/jpeg"}
            or not image_data
            or len(image_data) > 512 * 1024
        ):
            return False
        with self.lock:
            try:
                with self._db() as db:
                    db.execute(
                        "INSERT INTO sd_previews(filename,mime_type,image,updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(filename) DO UPDATE SET "
                        "mime_type=excluded.mime_type,image=excluded.image,updated_at=excluded.updated_at",
                        (
                            filename,
                            mime_type,
                            sqlite3.Binary(image_data),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    db.execute(
                        "DELETE FROM sd_previews WHERE filename NOT IN "
                        "(SELECT filename FROM sd_previews ORDER BY updated_at DESC LIMIT 100)"
                    )
                return True
            except sqlite3.Error as exc:
                self.storage_error = f"SD preview storage failed: {exc}"
                return False

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def save_local_gcode_record(self, record: dict[str, Any]) -> bool:
        """Persist local file metadata and its embedded thumbnail, if available."""
        image = record.get("image")
        mime_type = record.get("mime_type")
        if image is not None and (
            mime_type not in {"image/png", "image/jpeg"} or len(image) > 512 * 1024
        ):
            return False
        with self.lock:
            try:
                with self._db() as db:
                    db.execute(
                        "INSERT INTO local_gcode_previews("
                        "relative_path,filename,firmware_key,source_fingerprint,source_size_bytes,"
                        "source_mtime_ns,source_ctime_ns,mime_type,image,thumbnail_width,thumbnail_height,"
                        "metadata_json,preview_available,indexed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(relative_path) DO UPDATE SET filename=excluded.filename,"
                        "firmware_key=excluded.firmware_key,source_fingerprint=excluded.source_fingerprint,"
                        "source_size_bytes=excluded.source_size_bytes,source_mtime_ns=excluded.source_mtime_ns,"
                        "source_ctime_ns=excluded.source_ctime_ns,"
                        "mime_type=excluded.mime_type,image=excluded.image,"
                        "thumbnail_width=excluded.thumbnail_width,thumbnail_height=excluded.thumbnail_height,"
                        "metadata_json=excluded.metadata_json,preview_available=excluded.preview_available,"
                        "indexed_at=excluded.indexed_at,source_present=1",
                        (
                            record["relative_path"],
                            record["filename"],
                            record.get("firmware_key"),
                            record["source_fingerprint"],
                            record["source_size_bytes"],
                            record["source_mtime_ns"],
                            record["source_ctime_ns"],
                            mime_type,
                            sqlite3.Binary(image) if image is not None else None,
                            record.get("thumbnail_width"),
                            record.get("thumbnail_height"),
                            json.dumps(record.get("metadata", {}), separators=(",", ":")),
                            int(bool(record.get("preview_available"))),
                            self.now_iso(),
                        ),
                    )
                    db.execute(
                        "DELETE FROM local_gcode_previews WHERE relative_path NOT IN "
                        "(SELECT relative_path FROM local_gcode_previews "
                        "ORDER BY preview_available DESC,indexed_at DESC LIMIT 1000)"
                    )
                    db.execute(
                        "UPDATE local_gcode_previews SET image=NULL,mime_type=NULL "
                        "WHERE relative_path NOT IN "
                        "(SELECT relative_path FROM local_gcode_previews "
                        "WHERE preview_available=1 ORDER BY indexed_at DESC LIMIT 200)"
                    )
                return True
            except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
                self.storage_error = f"Local G-code index storage failed: {exc}"
                return False

    @staticmethod
    def _local_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "relative_path": row["relative_path"],
            "filename": row["filename"],
            "firmware_key": row["firmware_key"],
            "source_fingerprint": row["source_fingerprint"],
            "source_size_bytes": row["source_size_bytes"],
            "source_mtime_ns": row["source_mtime_ns"],
            "source_ctime_ns": row["source_ctime_ns"],
            "mime_type": row["mime_type"],
            "image": bytes(row["image"]) if row["image"] is not None else None,
            "thumbnail_width": row["thumbnail_width"],
            "thumbnail_height": row["thumbnail_height"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "preview_available": bool(row["preview_available"]) and row["image"] is not None,
            "indexed_at": row["indexed_at"],
            "source_present": bool(row["source_present"]),
        }

    def get_local_gcode_record(self, relative_path: str) -> dict[str, Any] | None:
        with self.lock:
            try:
                with self._db() as db:
                    row = db.execute(
                        "SELECT * FROM local_gcode_previews WHERE relative_path=?", (relative_path,)
                    ).fetchone()
                return self._local_record(row) if row else None
            except (sqlite3.Error, json.JSONDecodeError):
                return None

    def list_local_gcode_records(self) -> list[dict[str, Any]]:
        with self.lock:
            try:
                with self._db() as db:
                    rows = db.execute(
                        "SELECT * FROM local_gcode_previews "
                        "ORDER BY filename COLLATE NOCASE,relative_path COLLATE NOCASE"
                    ).fetchall()
                return [self._local_record(row) for row in rows]
            except (sqlite3.Error, json.JSONDecodeError):
                return []

    def get_unique_local_gcode_preview(self, filename: str) -> tuple[str, bytes] | None:
        """Serve a thumbnail only when its firmware-compatible basename is unique."""
        key = filename.casefold()
        with self.lock:
            try:
                with self._db() as db:
                    rows = db.execute(
                        "SELECT firmware_key,mime_type,image,preview_available "
                        "FROM local_gcode_previews WHERE firmware_key=? COLLATE NOCASE",
                        (key,),
                    ).fetchall()
                if len(rows) != 1 or not rows[0]["preview_available"] or rows[0]["image"] is None:
                    return None
                return rows[0]["mime_type"], bytes(rows[0]["image"])
            except sqlite3.Error:
                return None

    def get_local_gcode_preview_by_path(self, relative_path: str) -> tuple[str, bytes] | None:
        with self.lock:
            try:
                with self._db() as db:
                    row = db.execute(
                        "SELECT mime_type,image,preview_available FROM local_gcode_previews "
                        "WHERE relative_path=?",
                        (relative_path,),
                    ).fetchone()
                if row is None or not row["preview_available"] or row["image"] is None:
                    return None
                return row["mime_type"], bytes(row["image"])
            except sqlite3.Error:
                return None

    def local_index_updated_at(self) -> str | None:
        with self.lock:
            try:
                with self._db() as db:
                    row = db.execute(
                        "SELECT MAX(indexed_at) AS value FROM local_gcode_previews"
                    ).fetchone()
                return row["value"] if row else None
            except sqlite3.Error:
                return None

    def save_local_index_state(self, data: dict[str, Any]) -> None:
        with self.lock:
            try:
                with self._db() as db:
                    db.execute(
                        "INSERT INTO local_gcode_index_state(singleton,data_json) VALUES(1,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET data_json=excluded.data_json",
                        (json.dumps(data, separators=(",", ":")),),
                    )
            except (sqlite3.Error, TypeError, ValueError) as exc:
                self.storage_error = f"Local G-code scan status storage failed: {exc}"

    def get_local_index_state(self) -> dict[str, Any]:
        with self.lock:
            try:
                with self._db() as db:
                    row = db.execute(
                        "SELECT data_json FROM local_gcode_index_state WHERE singleton=1"
                    ).fetchone()
                return json.loads(row["data_json"]) if row else {}
            except (sqlite3.Error, json.JSONDecodeError):
                return {}

    def mark_local_gcode_sources(self, relative_paths: set[str]) -> None:
        """Mark the complete mounted-volume snapshot without deleting cached previews."""
        with self.lock:
            try:
                with self._db() as db:
                    db.execute("UPDATE local_gcode_previews SET source_present=0")
                    db.executemany(
                        "UPDATE local_gcode_previews SET source_present=1 WHERE relative_path=?",
                        [(path,) for path in relative_paths],
                    )
            except sqlite3.Error as exc:
                self.storage_error = f"Local G-code index presence update failed: {exc}"

    def has_sd_thumbnail(self, filename: str) -> bool:
        with self.lock:
            try:
                with self._db() as db:
                    return (
                        db.execute(
                            "SELECT 1 FROM sd_previews WHERE filename=? COLLATE NOCASE", (filename,)
                        ).fetchone()
                        is not None
                    )
            except sqlite3.Error:
                return False

    def get_sd_thumbnail(self, filename: str) -> tuple[str, bytes] | None:
        with self.lock:
            try:
                with self._db() as db:
                    row = db.execute(
                        "SELECT mime_type,image FROM sd_previews WHERE filename=? COLLATE NOCASE",
                        (filename,),
                    ).fetchone()
            except sqlite3.Error:
                return None
        return (str(row["mime_type"]), bytes(row["image"])) if row else None

    def _segments(self, db: sqlite3.Connection, file_id: str) -> list[dict[str, Any]]:
        cached = self._segment_cache.get(file_id)
        if cached is None:
            cached = [
                json.loads(row["data"])
                for row in db.execute(
                    "SELECT data FROM segments WHERE file_id=? ORDER BY seq", (file_id,)
                )
            ]
            self._segment_cache[file_id] = cached
        return cached

    @staticmethod
    def _prune(db: sqlite3.Connection) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).isoformat()
        history_cutoff = (
            datetime.now(UTC) - timedelta(days=PRINT_HISTORY_RETENTION_DAYS)
        ).isoformat()
        db.execute(
            "DELETE FROM print_sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (history_cutoff,),
        )
        db.execute(
            "DELETE FROM telemetry WHERE id NOT IN "
            "(SELECT id FROM telemetry ORDER BY id DESC LIMIT ?)",
            (MAX_TELEMETRY_ROWS,),
        )
        db.execute(
            "DELETE FROM gcode_files WHERE created_at < ? AND id NOT IN "
            "(SELECT file_id FROM print_sessions WHERE file_id IS NOT NULL)",
            (cutoff,),
        )
        db.execute(
            "DELETE FROM operation_events WHERE id NOT IN "
            "(SELECT id FROM operation_events ORDER BY updated_at DESC LIMIT ?)",
            (MAX_OPERATION_EVENTS,),
        )
        db.execute("DELETE FROM machine_state_events WHERE observed_at < ?", (cutoff,))
        db.execute(
            "DELETE FROM machine_state_events WHERE id NOT IN "
            "(SELECT id FROM machine_state_events ORDER BY observed_at DESC LIMIT 2000)"
        )
        files = db.execute("SELECT id FROM gcode_files ORDER BY created_at DESC").fetchall()
        total_segments = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        remaining_files = len(files)
        for row in reversed(files):
            if remaining_files <= MAX_STORED_GCODE_FILES and total_segments <= MAX_STORED_SEGMENTS:
                break
            segment_count = db.execute(
                "SELECT COUNT(*) FROM segments WHERE file_id=?", (row["id"],)
            ).fetchone()[0]
            db.execute("UPDATE print_sessions SET file_id=NULL WHERE file_id=?", (row["id"],))
            db.execute("DELETE FROM gcode_files WHERE id=?", (row["id"],))
            total_segments -= segment_count
            remaining_files -= 1

    def store_gcode(self, text: str, filename: str) -> dict[str, Any]:
        result = interpret_gcode(text, filename)
        file_id = str(uuid.uuid4())
        with self.lock, self._db() as db:
            db.execute(
                "INSERT INTO gcode_files VALUES(?,?,?,?,?,?,?)",
                (
                    file_id,
                    result["filename"],
                    result["sha256"],
                    result["size_bytes"],
                    result["estimated_duration_seconds"],
                    json.dumps(result["warnings"]),
                    now_iso(),
                ),
            )
            db.executemany(
                "INSERT INTO segments VALUES(?,?,?)",
                [
                    (file_id, idx, json.dumps(row, separators=(",", ":")))
                    for idx, row in enumerate(result["segments"])
                ],
            )
            db.executemany(
                "INSERT INTO gcode_events VALUES(?,?,?)",
                [
                    (file_id, idx, json.dumps(row, separators=(",", ":")))
                    for idx, row in enumerate(result["events"])
                ],
            )
            self._prune(db)
        return {
            **{k: v for k, v in result.items() if k != "segments"},
            "id": file_id,
            "segment_count": len(result["segments"]),
            "segments": result["segments"][:3000],
        }

    def record_snapshot(
        self, snapshot: dict[str, Any], operations: list[dict[str, Any]] | None = None
    ) -> None:
        connection = snapshot.get("connection")
        if snapshot.get("stale") is True or (
            isinstance(connection, dict) and connection.get("connected") is False
        ):
            # A failed poll carries the last good telemetry for display. It is
            # not a new observation and must not extend a print session or
            # create fabricated idle/feedrate history.
            return
        telemetry = snapshot.get("telemetry") or {}

        def val(key: str) -> Any:
            return (telemetry.get(key) or {}).get("value")

        active = val("print_active")
        filename = val("current_file") or val("filename") or val("sd_filename")
        progress = val("progress_percent")
        state = val("printer_state") or "unknown"
        source = (
            "OctoPrint_API_telemetry"
            if (connection or {}).get("transport") == "OctoPrint API"
            else "MKS_read_only_telemetry"
        )
        observed = snapshot.get("observed_at") or now_iso()
        latency = snapshot.get("poll_latency_ms")
        with self.lock, self._db() as db:
            previous = db.execute(
                "SELECT observed_at,state,filename,nozzle_target,bed_target FROM telemetry "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = dict(previous) if previous else None
            current = db.execute(
                "SELECT * FROM print_sessions WHERE ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if active is True:
                if current is None or current["source"] != source or (
                    filename
                    and current["filename"]
                    and filename.casefold() != current["filename"].casefold()
                ):
                    if current is not None:
                        db.execute(
                            "UPDATE print_sessions SET ended_at=?, state='interrupted' WHERE id=?",
                            (observed, current["id"]),
                        )
                    match = db.execute(
                        "SELECT id FROM gcode_files WHERE lower(filename)=lower(?) "
                        "ORDER BY created_at DESC LIMIT 1",
                        (Path(str(filename)).name if filename else "",),
                    ).fetchone()
                    session_id = str(uuid.uuid4())
                    db.execute(
                        "INSERT INTO print_sessions VALUES(?,?,?,?,?,?,?,?)",
                        (
                            session_id,
                            filename,
                            observed,
                            None,
                            state,
                            progress,
                            match["id"] if match else None,
                            source,
                        ),
                    )
                else:
                    session_id = current["id"]
                    if not current["file_id"] and filename:
                        match = db.execute(
                            "SELECT id FROM gcode_files WHERE lower(filename)=lower(?) "
                            "ORDER BY created_at DESC LIMIT 1",
                            (Path(str(filename)).name,),
                        ).fetchone()
                        if match:
                            db.execute(
                                "UPDATE print_sessions SET file_id=? WHERE id=?",
                                (match["id"], session_id),
                            )
                    db.execute(
                        "UPDATE print_sessions SET state=?, progress=?, "
                        "filename=COALESCE(?,filename) WHERE id=?",
                        (state, progress, filename, session_id),
                    )
                db.execute(
                    "INSERT INTO telemetry(session_id,observed_at,state,filename,progress,"
                    "nozzle,nozzle_target,bed,bed_target,latency_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        observed,
                        state,
                        filename,
                        progress,
                        val("nozzle_temperature"),
                        (telemetry.get("nozzle_temperature") or {}).get("target_c"),
                        val("bed_temperature"),
                        (telemetry.get("bed_temperature") or {}).get("target_c"),
                        None if latency is None else float(latency),
                    ),
                )
            elif active is False:
                session_id = current["id"] if current else None
                if current:
                    db.execute(
                        "INSERT INTO telemetry(session_id,observed_at,state,filename,progress,"
                        "nozzle,nozzle_target,bed,bed_target,latency_ms) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            current["id"],
                            observed,
                            state,
                            filename,
                            progress,
                            val("nozzle_temperature"),
                            (telemetry.get("nozzle_temperature") or {}).get("target_c"),
                            val("bed_temperature"),
                            (telemetry.get("bed_temperature") or {}).get("target_c"),
                            None if latency is None else float(latency),
                        ),
                    )
                    db.execute(
                        "UPDATE print_sessions SET ended_at=?, state=? WHERE id=?",
                        (observed, state, current["id"]),
                    )
                db.execute(
                    "INSERT INTO telemetry(session_id,observed_at,state,filename,progress,"
                    "nozzle,nozzle_target,bed,bed_target,latency_ms) "
                    "VALUES(NULL,?,?,?,?,?,?,?,?,?)",
                    (
                        observed,
                        state,
                        filename,
                        progress,
                        val("nozzle_temperature"),
                        (telemetry.get("nozzle_temperature") or {}).get("target_c"),
                        val("bed_temperature"),
                        (telemetry.get("bed_temperature") or {}).get("target_c"),
                        None if latency is None else float(latency),
                    ),
                )
            else:
                # Missing/unsupported active state is unknown, not evidence
                # that a current print stopped. Keep the session open and
                # associate the telemetry sample with it when one exists.
                session_id = current["id"] if current else None
                db.execute(
                    "INSERT INTO telemetry(session_id,observed_at,state,filename,progress,"
                    "nozzle,nozzle_target,bed,bed_target,latency_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        observed,
                        state,
                        filename,
                        progress,
                        val("nozzle_temperature"),
                        (telemetry.get("nozzle_temperature") or {}).get("target_c"),
                        val("bed_temperature"),
                        (telemetry.get("bed_temperature") or {}).get("target_c"),
                        None if latency is None else float(latency),
                    ),
                )
            if previous:
                observed_values = {
                    "nozzle_target": (telemetry.get("nozzle_temperature") or {}).get("target_c"),
                    "bed_target": (telemetry.get("bed_temperature") or {}).get("target_c"),
                    "state": state,
                    "filename": filename,
                }
                for field, value in observed_values.items():
                    old_value = previous.get(field)
                    if value is None or old_value is None or value == old_value:
                        continue
                    if field in {"nozzle_target", "bed_target"}:
                        try:
                            if abs(float(value) - float(old_value)) < 0.5:
                                continue
                        except (TypeError, ValueError):
                            continue
                        device = "nozzle" if field == "nozzle_target" else "bed"
                        codes = ("M104", "M109") if device == "nozzle" else ("M140", "M190")
                        correlated = None
                        for operation in reversed(operations or []):
                            for result in operation.get("command_results", []):
                                command = str(result.get("command") or "").upper()
                                dispatched = result.get("dispatched_at")
                                if not dispatched or not any(
                                    re.search(rf"\b{code}\b", command) for code in codes
                                ):
                                    continue
                                try:
                                    age = abs(
                                        (
                                            datetime.fromisoformat(observed)
                                            - datetime.fromisoformat(dispatched)
                                        ).total_seconds()
                                    )
                                except (TypeError, ValueError):
                                    continue
                                if age <= 90:
                                    correlated = {
                                        "operation_id": operation.get("id"),
                                        "command": command,
                                    }
                                    break
                            if correlated:
                                break
                        kind = f"{device}_target_changed"
                        classification = (
                            "firmware_state_observed"
                            if correlated
                            else "external_state_change_inferred"
                        )
                        source = (
                            "command_executor_correlated"
                            if correlated
                            else "unmatched_telemetry_change"
                        )
                        confidence = (
                            "telemetry_target_observed" if correlated else "source_not_identified"
                        )
                        details = {
                            "field": field,
                            "previous": old_value,
                            "value": value,
                            "correlation": correlated,
                        }
                    else:
                        kind = (
                            "printer_state_changed" if field == "state" else "active_file_changed"
                        )
                        classification = "firmware_state_observed"
                        source = "MKS_read_only_telemetry"
                        confidence = "reported_state_transition"
                        details = {"field": field, "previous": old_value, "value": value}
                    event_id = hashlib.sha256(
                        f"{observed}|{kind}|{json.dumps(details, sort_keys=True)}".encode()
                    ).hexdigest()
                    db.execute(
                        "INSERT OR IGNORE INTO machine_state_events VALUES(?,?,?,?,?,?,?)",
                        (
                            event_id,
                            observed,
                            kind,
                            source,
                            classification,
                            confidence,
                            json.dumps(details, separators=(",", ":")),
                        ),
                    )
            if operations:
                for item in operations:
                    event_id = item.get("id")
                    if not event_id:
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO operation_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            event_id,
                            item.get("created_at"),
                            item.get("updated_at"),
                            item.get("macro_id"),
                            item.get("state"),
                            json.dumps(item.get("commands", [])),
                            json.dumps(item.get("command_results", [])),
                            int(bool(item.get("transport_acknowledged"))),
                            int(bool(item.get("physical_completion_confirmed"))),
                            item.get("operator_observation"),
                            item.get("error"),
                            json.dumps(item, separators=(",", ":")),
                        ),
                    )
            self._prune(db)

    def list_print_history(self, limit: int = 200) -> dict[str, Any]:
        """Read persisted observed sessions without implying successful print completion."""
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Print history limit must be between 1 and 500")
        with self.lock, self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM print_sessions").fetchone()[0]
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT id,filename,started_at,ended_at,state,progress,source "
                    "FROM print_sessions ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                )
            ]
        for row in rows:
            state = str(row["state"] or "unknown").lower()
            ended = row["ended_at"] is not None
            if not ended:
                outcome = "active_or_unfinished"
            elif state in {"completed", "complete", "success"}:
                outcome = "reported_completed"
            elif state in {"cancelled", "canceled"}:
                outcome = "reported_cancelled"
            elif state in {"failed", "error"}:
                outcome = "reported_failed"
            elif state == "interrupted":
                outcome = "inferred_interrupted"
            else:
                outcome = "ended_unverified"
            elapsed = None
            if ended:
                try:
                    start = datetime.fromisoformat(row["started_at"])
                    finish = datetime.fromisoformat(row["ended_at"])
                    elapsed = max(0, int((finish - start).total_seconds()))
                except (TypeError, ValueError):
                    pass
            row["outcome"] = outcome
            row["duration_seconds"] = elapsed
            row["outcome_verified"] = False
        return {
            "sessions": rows,
            "total": total,
            "returned": len(rows),
            "limit": limit,
            "source": "Observatory SQLite recording",
            "retention_days": PRINT_HISTORY_RETENTION_DAYS,
            "note": (
                "These are Observatory-observed sessions. An idle transition or "
                "100% progress alone does not prove print completion. A session "
                "recorded as ended_unverified has no verified final outcome."
            ),
        }

    def snapshot(
        self, limit: int = 500, live_snapshot: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self.lock, self._db() as db:
            sessions = [
                dict(row)
                for row in db.execute(
                    "SELECT id,filename,started_at,ended_at,state,progress,file_id,source "
                    "FROM print_sessions ORDER BY started_at DESC LIMIT 50"
                )
            ]
            current = next((s for s in sessions if s["ended_at"] is None), None)
            idle_now = datetime.now(UTC)
            idle_cutoff = (idle_now - timedelta(hours=24)).isoformat()
            idle_detail_cutoff = (idle_now - timedelta(minutes=5)).isoformat()
            idle_bucket_seconds = math.ceil((24 * 60 * 60 - 5 * 60) / (MAX_FEEDRATE_SAMPLES - 150))
            idle_rows = [
                dict(row)
                for row in db.execute(
                    "SELECT observed_at,state FROM telemetry WHERE session_id IS NULL "
                    "AND observed_at>=? AND lower(coalesce(state,'')) IN "
                    "('idle','paused','complete','completed') UNION ALL "
                    "SELECT t.observed_at,t.state FROM telemetry t JOIN ("
                    "SELECT MAX(id) AS id FROM telemetry WHERE session_id IS NULL "
                    "AND observed_at>=? AND observed_at<? AND lower(coalesce(state,'')) IN "
                    "('idle','paused','complete','completed') GROUP BY "
                    "CAST((julianday(observed_at)-julianday(?))*86400/? AS INTEGER)"
                    ") buckets ON t.id=buckets.id ORDER BY observed_at",
                    (
                        idle_detail_cutoff,
                        idle_cutoff,
                        idle_detail_cutoff,
                        idle_cutoff,
                        idle_bucket_seconds,
                    ),
                )
            ]
            files = [
                dict(row)
                for row in db.execute(
                    "SELECT id,filename,sha256,size_bytes,duration,warnings,created_at "
                    "FROM gcode_files ORDER BY created_at DESC LIMIT 50"
                )
            ]
            telemetry = []
            if current:
                telemetry = [
                    dict(row)
                    for row in db.execute(
                        "SELECT observed_at,state,filename,progress,nozzle,nozzle_target,"
                        "bed,bed_target,latency_ms FROM telemetry "
                        "WHERE session_id=? ORDER BY id DESC LIMIT ?",
                        (current["id"], min(limit, 6000)),
                    )
                ]
                telemetry.reverse()
            operations = [
                json.loads(row["data"])
                for row in db.execute(
                    "SELECT data FROM operation_events ORDER BY updated_at DESC LIMIT 100"
                )
            ]
            operation_path: list[dict[str, Any]] = []
            for operation in reversed(operations):
                command_results = operation.get("command_results", [])
                acknowledged = bool(operation.get("transport_acknowledged")) or any(
                    result.get("state") == "transport_acknowledged" for result in command_results
                )
                if not acknowledged:
                    # A dry-run, queued request, or failed-before-send record is
                    # an event, not evidence of a dispatched motion path.
                    continue
                macro_id = str(operation.get("macro_id", ""))
                parts = macro_id.split("_")
                axis = operation.get("planned_axis") or (parts[1] if len(parts) > 1 else None)
                if axis not in {"x", "y", "z"} or not macro_id.startswith("jog_"):
                    continue
                target = operation.get(
                    "planned_end_position_mm", operation.get("proposed_position_mm")
                )
                commands = operation.get("commands") or []
                move_delta = 0.0
                feed = None
                for command in commands:
                    match = re.search(rf"\b{axis.upper()}([-+]?(?:\d+(?:\.\d*)?|\.\d+))", command)
                    if match:
                        move_delta += float(match.group(1))
                    feed_match = re.search(r"\bF(\d+(?:\.\d+)?)\b", command)
                    if feed_match:
                        feed = float(feed_match.group(1))
                start = operation.get("planned_start_position_mm")
                if start is None and isinstance(target, (int, float)):
                    start = float(target) - move_delta
                if not isinstance(start, (int, float)) or not isinstance(target, (int, float)):
                    continue
                distance = abs(float(target) - float(start))
                duration_seconds = operation.get("estimated_duration_seconds")
                if duration_seconds is None and feed and feed > 0:
                    duration_seconds = distance / (feed / 60.0)
                dispatched = next(
                    (
                        result.get("dispatched_at")
                        for result in command_results
                        if result.get("dispatched_at")
                    ),
                    operation.get("created_at"),
                )
                if not dispatched:
                    continue
                try:
                    started = datetime.fromisoformat(dispatched)
                except (TypeError, ValueError):
                    continue
                if duration_seconds is None:
                    duration_seconds = 0.0
                source = "commanded_operation"
                confidence = (
                    "operator_observed_commanded_move"
                    if operation.get("physical_completion_confirmed")
                    else "acknowledged_target_not_physical"
                )
                for point, offset in (
                    (float(start), 0.0),
                    (float(target), float(duration_seconds)),
                ):
                    point_time = started + timedelta(seconds=offset)
                    operation_path.append(
                        {
                            "observed_at": point_time.isoformat(),
                            "axes": {axis: {"status": "planned", "value_mm": point}},
                            "interval": {
                                "status": "planned",
                                "axis_deltas_mm": {axis: float(target) - float(start)},
                                "axis_velocities_mm_s": {
                                    axis: (float(target) - float(start))
                                    / max(float(duration_seconds), 0.001)
                                },
                                "distance_mm": distance,
                                "duration_seconds": duration_seconds,
                                "movement_axes": [axis],
                            }
                            if offset
                            else None,
                            "source": source,
                            "confidence": confidence,
                            "operation_id": operation.get("id"),
                            "timestamp_precision": "dispatch_time"
                            if any(result.get("dispatched_at") for result in command_results)
                            else "operation_time_approximate",
                            "kind": "commanded_jog_target",
                        }
                    )
            reconstruction = None
            if current and current["file_id"]:
                record = db.execute(
                    "SELECT duration FROM gcode_files WHERE id=?", (current["file_id"],)
                ).fetchone()
                if record:
                    pct = current.get("progress")
                    if isinstance(pct, (int, float)):
                        elapsed = record["duration"] * max(0.0, min(100.0, float(pct))) / 100.0
                        segrow = db.execute(
                            "SELECT data FROM segments WHERE file_id=? "
                            "AND json_extract(data,'$.end_seconds')>=? "
                            "ORDER BY seq LIMIT 1",
                            (current["file_id"], elapsed),
                        ).fetchone()
                        if segrow:
                            seg = json.loads(segrow["data"])
                            fraction = (
                                0
                                if seg["duration_seconds"] <= 0
                                else max(
                                    0.0,
                                    min(
                                        1.0,
                                        (elapsed - seg["start_seconds"]) / seg["duration_seconds"],
                                    ),
                                )
                            )
                            point = {
                                axis: seg["from"][axis]
                                + (seg["to"][axis] - seg["from"][axis]) * fraction
                                for axis in "xyze"
                            }
                            reconstruction = {
                                "position": point,
                                "velocity_mm_s": seg["velocity_mm_s"],
                                "kind": seg["kind"],
                                "line": seg["line"],
                                "elapsed_seconds": elapsed,
                                "progress_percent": pct,
                                "confidence": "progress_mapped_estimate",
                                "source": "MKS_progress_mapped_to_uploaded_G-code",
                                "physical_position_confirmed": False,
                                "motion_timing": "linear_feed_estimate_no_acceleration",
                            }
            planned_history = []
            matched_file = (current or {}).get("file_id")
            planned_start = (current or {}).get("started_at")
            if not current and files:
                matched_file = files[0]["id"]
                # Place offline path samples into a relative, current replay window.
                planned_start = (
                    datetime.now(UTC) - timedelta(seconds=float(files[0]["duration"]))
                ).isoformat()
            if matched_file:
                row_count = db.execute(
                    "SELECT COUNT(*) FROM segments WHERE file_id=?", (matched_file,)
                ).fetchone()[0]
                stride = max(1, math.ceil(row_count / limit))
                rows = db.execute(
                    "SELECT seq,data FROM segments WHERE file_id=? "
                    "AND (seq % ? = 0 OR seq = (SELECT MAX(seq) FROM segments WHERE file_id=?)) "
                    "ORDER BY seq",
                    (matched_file, stride, matched_file),
                ).fetchall()
                started = datetime.fromisoformat(planned_start)
                for row in rows:
                    seg = json.loads(row["data"])
                    when = started + timedelta(seconds=seg["start_seconds"])
                    planned_history.append(
                        {
                            "observed_at": when.isoformat(),
                            "axes": {
                                a: {"status": "planned", "value_mm": seg["from"][a]} for a in "xyze"
                            },
                            "interval": {
                                "status": "planned",
                                "distance_mm": seg["distance_mm"],
                                "duration_seconds": seg["duration_seconds"],
                                "velocity_mm_s": seg["velocity_mm_s"],
                                "axis_deltas_mm": seg["axis_deltas_mm"],
                                "axis_velocities_mm_s": seg["axis_velocities_mm_s"],
                                "movement_axes": [
                                    a for a in "xyz" if abs(seg["to"][a] - seg["from"][a]) > 1e-8
                                ],
                            },
                            "source": "uploaded_gcode_plan",
                            "confidence": seg["confidence"],
                            "line": seg["line"],
                            "kind": seg["kind"],
                        }
                    )
            feedrate_history: list[dict[str, Any]] = []
            offline_feedrate_history: list[dict[str, Any]] = []
            matched_segments: list[dict[str, Any]] = []
            matched_duration = 0.0
            if matched_file:
                file_row = db.execute(
                    "SELECT duration FROM gcode_files WHERE id=?", (matched_file,)
                ).fetchone()
                if file_row:
                    matched_duration = float(file_row["duration"])
                    matched_segments = self._segments(db, matched_file)
            if current and matched_segments:
                feedrate_history = _feedrate_for_samples(
                    telemetry, matched_segments, matched_duration
                )
            elif not current and matched_segments:
                start = datetime.fromisoformat(planned_start)
                for segment in matched_segments:
                    for point in segment.get("feedrate_profile", []):
                        offline_feedrate_history.append(
                            {
                                "observed_at": (
                                    start
                                    + timedelta(
                                        seconds=float(segment["start_seconds"])
                                        + float(point["offset_seconds"])
                                    )
                                ).isoformat(),
                                "feedrate_mm_min": round(float(point["feedrate_mm_min"]), 3),
                                "axis_feedrates_mm_min": _axis_feedrates(
                                    segment, float(point["feedrate_mm_min"])
                                ),
                                "source": "offline_gcode_plan",
                                "data_class": "planned_feedrate",
                                "confidence": segment["confidence"],
                                "line": segment["line"],
                            }
                        )
                for event in db.execute(
                    "SELECT data FROM gcode_events WHERE file_id=? ORDER BY seq", (matched_file,)
                ):
                    item = json.loads(event["data"])
                    if item.get("kind") == "planned_idle_dwell":
                        for offset in (item["start_seconds"], item["end_seconds"]):
                            offline_feedrate_history.append(
                                {
                                    "observed_at": (
                                        start + timedelta(seconds=float(offset))
                                    ).isoformat(),
                                    "feedrate_mm_min": 0.0,
                                    "axis_feedrates_mm_min": dict.fromkeys("xyze", 0.0),
                                    "source": "offline_gcode_plan",
                                    "data_class": "planned_idle_interval",
                                    "confidence": item["confidence"],
                                    "line": item["line"],
                                }
                            )
            if live_snapshot is not None and not current:
                telemetry_state = live_snapshot.get("telemetry") or {}
                state = (telemetry_state.get("printer_state") or {}).get("value")
                connection = live_snapshot.get("connection") or {}
                if (
                    state == "idle"
                    and connection.get("connected")
                    and not live_snapshot.get("stale")
                    and not any(item.get("state") in {"queued", "running"} for item in operations)
                ):
                    feedrate_history = [
                        {
                            "observed_at": row["observed_at"],
                            "feedrate_mm_min": 0.0,
                            "axis_feedrates_mm_min": dict.fromkeys("xyze", 0.0),
                            "source": "firmware_reported_state",
                            "data_class": "firmware_reported_state_not_motor_feedback",
                            "confidence": "no_print_motion_reported",
                        }
                        for row in idle_rows
                    ]
            for operation in operations:
                if not str(operation.get("macro_id", "")).startswith("jog_"):
                    continue
                results = operation.get("command_results", [])
                dispatched = next(
                    (item.get("dispatched_at") for item in results if item.get("dispatched_at")),
                    None,
                )
                if not dispatched:
                    continue
                axis = str(operation.get("macro_id", "")).split("_")[1]
                distance = 0.0
                feed = 0.0
                for command in operation.get("commands", []):
                    move = re.search(rf"\b{axis.upper()}([-+]?(?:\d+(?:\.\d*)?|\.\d+))", command)
                    rate = re.search(r"\bF(\d+(?:\.\d*)?)", command)
                    if move:
                        distance += float(move.group(1))
                    if rate:
                        feed = float(rate.group(1))
                if not distance or not feed:
                    continue
                duration, profile, _ = _motion_timing(abs(distance), feed, None)
                try:
                    started = datetime.fromisoformat(dispatched)
                except ValueError:
                    continue
                for point in profile:
                    feedrate_history.append(
                        {
                            "observed_at": (
                                started + timedelta(seconds=point["offset_seconds"])
                            ).isoformat(),
                            "feedrate_mm_min": round(point["feedrate_mm_min"], 3),
                            "axis_feedrates_mm_min": {
                                key: round(point["feedrate_mm_min"], 3) if key == axis else 0.0
                                for key in "xyze"
                            },
                            "source": "commanded_operation_estimate",
                            "data_class": "commanded_estimate",
                            "confidence": "planned_duration_from_feedrate_ack_not_completion",
                            "operation_id": operation.get("id"),
                            "estimated_duration_seconds": duration,
                        }
                    )
            feedrate_history.sort(key=lambda row: row["observed_at"])
            offline_feedrate_history.sort(key=lambda row: row["observed_at"])
            if len(feedrate_history) > MAX_FEEDRATE_SAMPLES:
                final_sample = feedrate_history[-1]
                stride = math.ceil(len(feedrate_history) / MAX_FEEDRATE_SAMPLES)
                feedrate_history = feedrate_history[::stride]
                if feedrate_history[-1] != final_sample:
                    feedrate_history.append(final_sample)
            timeline_events: list[dict[str, Any]] = []
            if matched_file:
                timeline_events.extend(
                    {
                        **(event_data := json.loads(row["data"])),
                        "observed_at": (
                            datetime.fromisoformat(planned_start)
                            + timedelta(
                                seconds=float(
                                    event_data.get(
                                        "time_seconds", event_data.get("start_seconds", 0.0)
                                    )
                                )
                            )
                        ).isoformat(),
                        "source": "offline_gcode_plan",
                        "data_class": "simulated_event",
                    }
                    for row in db.execute(
                        "SELECT data FROM gcode_events WHERE file_id=? ORDER BY seq",
                        (matched_file,),
                    )
                )
            timeline_events.extend(
                {
                    "observed_at": row.get("observed_at"),
                    "kind": row.get("kind"),
                    "label": row.get("label"),
                    "source": row.get("source"),
                    "data_class": row.get("data_class"),
                    "confidence": row.get("confidence"),
                    "operation_id": row.get("operation_id"),
                }
                for row in operation_path
            )
            timeline_events.extend(
                {
                    "observed_at": next(
                        (
                            result.get("dispatched_at")
                            for result in operation.get("command_results", [])
                            if result.get("dispatched_at")
                        ),
                        operation.get("updated_at") or operation.get("created_at"),
                    ),
                    "kind": "machine_operation",
                    "label": str(operation.get("macro_id", "machine action")).replace("_", " "),
                    "source": "command_executor",
                    "data_class": "operator_observed"
                    if operation.get("physical_completion_confirmed")
                    else "command_acknowledged"
                    if operation.get("transport_acknowledged")
                    else "command_event",
                    "confidence": operation.get("verification_confidence", "not_acknowledged"),
                    "operation_id": operation.get("id"),
                }
                for operation in reversed(operations)
                if operation.get("created_at") or operation.get("updated_at")
            )
            state_events = [
                {
                    **dict(row),
                    "details": json.loads(row["data"]),
                }
                for row in db.execute(
                    "SELECT * FROM machine_state_events ORDER BY observed_at DESC LIMIT 200"
                )
            ]
            timeline_events.extend(
                {
                    "observed_at": event["observed_at"],
                    "kind": event["kind"],
                    "label": event["kind"].replace("_", " "),
                    "source": event["source"],
                    "data_class": event["data_class"],
                    "confidence": event["confidence"],
                    "details": event["details"],
                }
                for event in state_events
            )
            timeline_events.sort(key=lambda item: item.get("observed_at") or "")
            planned_history.extend(operation_path)
            planned_history.sort(key=lambda row: row["observed_at"])
            return {
                "recording": "backend_poller",
                "retention_days": RETENTION_DAYS,
                "active_session": current,
                "sessions": sessions,
                "gcode_files": files,
                "telemetry": telemetry,
                "operations": list(reversed(operations)),
                "reconstruction": reconstruction,
                "planned_history": planned_history,
                "feedrate_history": feedrate_history,
                "offline_feedrate_history": offline_feedrate_history,
                "timeline_events": timeline_events[-MAX_OPERATION_EVENTS:],
                "machine_state_events": state_events,
                "note": (
                    "MKS telemetry and uploaded G-code analysis are persisted by the server. "
                    "G-code analysis does not upload to the printer. Planned and "
                    "progress-mapped positions are estimates, never firmware-reported coordinates."
                ),
            }

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        with self.lock, self._db() as db:
            session = db.execute(
                "SELECT * FROM print_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                return None
            result = dict(session)
            result["telemetry"] = [
                dict(row)
                for row in db.execute(
                    "SELECT observed_at,state,filename,progress,nozzle,nozzle_target,"
                    "bed,bed_target,latency_ms FROM telemetry "
                    "WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ]
            if result["file_id"]:
                file_row = db.execute(
                    "SELECT duration FROM gcode_files WHERE id=?", (result["file_id"],)
                ).fetchone()
                result["segments"] = [
                    json.loads(row["data"])
                    for row in db.execute(
                        "SELECT data FROM segments WHERE file_id=? ORDER BY seq",
                        (result["file_id"],),
                    )
                ]
                if file_row:
                    result["feedrate_history"] = _feedrate_for_samples(
                        result["telemetry"],
                        result["segments"],
                        float(file_row["duration"]),
                    )
                else:
                    result["feedrate_history"] = []
                result["events"] = [
                    json.loads(row["data"])
                    for row in db.execute(
                        "SELECT data FROM gcode_events WHERE file_id=? ORDER BY seq",
                        (result["file_id"],),
                    )
                ]
            else:
                result["feedrate_history"] = _feedrate_for_samples(result["telemetry"], [], 0.0)
                result["events"] = []
            return result
