"""Read-only M114 parsing and conservative coordinate interval estimates."""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime
from typing import Any

from .executor import CommandTransportError, MksTcpCommandTransport

AXES = ("x", "y", "z", "e")
_COORDINATE = re.compile(r"\b([XYZE])\s*:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", re.I)
_MAX_SPEED_MM_S = {"x": 300.0, "y": 300.0, "z": 30.0, "e": 100.0}


def parse_m114(lines: list[str]) -> dict[str, Any]:
    """Parse the first reported position tuple; absent/invalid axes stay unavailable."""
    axes: dict[str, dict[str, Any]] = {
        axis: {"status": "unsupported_or_not_reported", "value_mm": None} for axis in AXES
    }
    for line in lines:
        coordinate_text = re.split(r"\bCount\s+[XYZE]\s*:", line, maxsplit=1, flags=re.I)[0]
        found: set[str] = set()
        for match in _COORDINATE.finditer(coordinate_text):
            axis = match.group(1).lower()
            if axis in found:
                continue
            found.add(axis)
            try:
                value = float(match.group(2))
            except ValueError:
                continue
            if math.isfinite(value):
                axes[axis] = {"status": "available", "value_mm": value}
            else:
                axes[axis] = {"status": "malformed", "value_mm": None}
        if found:
            return {"status": "available", "axes": axes, "raw_lines": lines[-20:]}
    rejected = any(line.lower().startswith(("error:", "!!", "unknown command")) for line in lines)
    return {
        "status": "unsupported_or_malformed" if rejected or lines else "empty_response",
        "axes": axes,
        "raw_lines": lines[-20:],
    }


async def query_m114(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """Issue exactly one read-only M114 using the existing bounded MKS transport."""
    started = time.monotonic()
    transport = MksTcpCommandTransport(host, port, timeout)
    try:
        await transport.connect()
        response = await transport.send("M114", read_only=True)
        parsed = parse_m114(response.get("response_lines", []))
        return {
            **parsed,
            "acknowledgment": response.get("acknowledgment"),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except (CommandTransportError, OSError, TimeoutError) as exc:
        return {
            "status": "unsupported_or_unavailable",
            "axes": {axis: {"status": "unavailable", "value_mm": None} for axis in AXES},
            "raw_lines": [],
            "error": str(exc) or type(exc).__name__,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    finally:
        await transport.close()


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_motion_sample(
    result: dict[str, Any],
    *,
    observed_at: str,
    printer_state: str | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create a timestamped firmware-coordinate sample and guarded estimates."""
    axes = result.get("axes") or {
        axis: {"status": "unavailable", "value_mm": None} for axis in AXES
    }
    interval: dict[str, Any] = {
        "status": "unavailable",
        "elapsed_seconds": None,
        "deltas_mm": {axis: None for axis in AXES},
        "velocities_mm_s": {axis: None for axis in AXES},
        "movement_axes": [],
        "confidence": "not_estimated",
    }
    if result.get("status") == "available":
        previous_axes = previous.get("axes", {}) if previous else {}
        previous_time = previous.get("poll_timestamp") if previous else None
        elapsed = None
        if previous_time:
            try:
                elapsed = (
                    datetime.fromisoformat(observed_at) - datetime.fromisoformat(previous_time)
                ).total_seconds()
            except ValueError:
                elapsed = None
        if elapsed is not None and 0 < elapsed <= 120:
            deltas: dict[str, float | None] = {}
            speeds: dict[str, float | None] = {}
            changed: list[str] = []
            reset_candidate = False
            malformed = False
            for axis in AXES:
                current = axes.get(axis, {}).get("value_mm")
                prior = previous_axes.get(axis, {}).get("value_mm")
                if not isinstance(current, (int, float)) or not isinstance(prior, (int, float)):
                    deltas[axis] = None
                    speeds[axis] = None
                    malformed = True
                    continue
                delta = float(current) - float(prior)
                velocity = delta / elapsed
                deltas[axis] = round(delta, 4)
                if abs(delta) >= 0.01:
                    changed.append(axis)
                if (abs(float(current)) < 0.001 and abs(float(prior)) > 1) or abs(
                    velocity
                ) > _MAX_SPEED_MM_S[axis]:
                    reset_candidate = True
                speeds[axis] = round(velocity, 4)
            interval_status = (
                "coordinate_discontinuity"
                if reset_candidate
                else "unavailable"
                if malformed
                else "estimated"
            )
            interval = {
                "status": interval_status,
                "elapsed_seconds": round(elapsed, 3),
                "deltas_mm": (
                    {axis: None for axis in AXES} if reset_candidate or malformed else deltas
                ),
                "velocities_mm_s": (
                    {axis: None for axis in AXES} if reset_candidate or malformed else speeds
                ),
                "movement_axes": [] if reset_candidate or malformed else changed,
                "confidence": (
                    "not_estimated"
                    if reset_candidate or malformed
                    else "approximate_coordinate_delta"
                ),
                "note": (
                    "Possible homing/origin reset or implausible jump; interval suppressed."
                    if reset_candidate
                    else "Missing axis data; interval suppressed."
                    if malformed
                    else (
                        "Coordinate deltas are estimates, not physical measurements; M114 cannot "
                        "identify every external G92/origin change."
                    )
                ),
            }
    return {
        "observed_at": observed_at,
        "poll_timestamp": observed_at,
        "age_seconds": 0.0,
        "fresh": True,
        "latency_ms": result.get("latency_ms"),
        "source_command": "M114",
        "position_kind": "planned_position_reported_by_firmware",
        "measurement_confidence": "firmware_reported_planned_coordinates_not_physical_measurement",
        "printer_state": printer_state,
        "status": result.get("status", "unsupported_or_unavailable"),
        "axes": axes,
        "interval": interval,
        "error": result.get("error"),
        "acknowledgment": result.get("acknowledgment"),
        "raw_lines": result.get("raw_lines", []),
    }
