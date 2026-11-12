"""Source-aware summary of the live printer job and persisted print history."""

from __future__ import annotations

import math
from typing import Any


def _reported_value(telemetry: dict[str, Any], key: str, *, fresh: bool) -> dict[str, Any]:
    field = telemetry.get(key) or {}
    value = field.get("value")
    status = field.get("status", "unsupported_or_not_reported")
    if status != "available" or value is None:
        return {
            "status": "unavailable",
            "value": None,
            "source_command": field.get("source_command"),
        }
    if not fresh:
        return {"status": "stale", "value": value, "source_command": field.get("source_command")}
    return {"status": "available", "value": value, "source_command": field.get("source_command")}


def _valid_percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def print_job_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Summarize MKS-reported job state without promoting stale data to live state."""
    telemetry = snapshot.get("telemetry") or {}
    connection = snapshot.get("connection") or {}
    fresh = bool(connection.get("connected")) and not bool(snapshot.get("stale"))
    active_field = telemetry.get("print_active") or {}
    active_value = active_field.get("value") if active_field.get("status") == "available" else None
    host_state = str((telemetry.get("printer_state") or {}).get("value") or "unknown")
    external = telemetry.get("external_sd") or {}
    octoprint_mode = connection.get("transport") == "OctoPrint API"
    external_available = external.get("status") == "available"
    host_running = active_value is True
    external_selected = external_available and not host_running
    if not fresh:
        status = "stale" if snapshot.get("observed_at") else "unavailable"
        active = None
    elif octoprint_mode and external_selected:
        status = str(external.get("state") or "unknown")
        active = external.get("active")
    elif octoprint_mode and active_value is False:
        # OctoPrint idle describes its host job, not a separate touchscreen SD job.
        status = "unknown"
        active = None
    elif active_value is True:
        state = str((telemetry.get("printer_state") or {}).get("value") or "printing").lower()
        status = "paused" if state == "paused" else "printing"
        active = True
    elif active_value is False:
        status = "idle"
        active = False
    else:
        status = "unknown"
        active = None

    filename = _reported_value(telemetry, "current_file", fresh=fresh)
    if filename["status"] == "available" and not str(filename["value"]).strip():
        filename = {**filename, "status": "unavailable", "value": None}
    progress = _reported_value(telemetry, "progress_percent", fresh=fresh)
    progress["value"] = _valid_percent(progress["value"])
    if progress["value"] is None:
        progress["status"] = "unavailable"
    elapsed = _reported_value(telemetry, "elapsed_seconds", fresh=fresh)
    if fresh and octoprint_mode and external_selected:
        filename = {
            "status": "available" if external.get("filename") else "unavailable",
            "value": external.get("filename"),
            "source_command": "MKS_HTTP_/api/job",
        }
        progress = {
            "status": "available"
            if external.get("progress_percent") is not None
            else "unavailable",
            "value": _valid_percent(external.get("progress_percent")),
            "source_command": "MKS_HTTP_/api/job",
        }
        elapsed = {
            "status": "available" if external.get("elapsed_seconds") is not None else "unavailable",
            "value": external.get("elapsed_seconds"),
            "source_command": "MKS_HTTP_/api/job",
        }
    try:
        elapsed_value = float(elapsed["value"])
    except (TypeError, ValueError):
        elapsed_value = math.nan
    if not math.isfinite(elapsed_value) or elapsed_value < 0:
        elapsed = {**elapsed, "status": "unavailable", "value": None}
    else:
        elapsed["value"] = int(elapsed_value)

    timeline = snapshot.get("timeline") or {}
    sessions = timeline.get("sessions") or []
    active_session = timeline.get("active_session")
    active_id = active_session.get("id") if isinstance(active_session, dict) else None
    return {
        "status": status,
        "active": active,
        "source": (
            "MKS WiFi HTTP · printer-reported SD activity"
            if fresh and octoprint_mode and external_selected
            else "OctoPrint API telemetry"
            if fresh and octoprint_mode
            else "MKS read-only telemetry"
            if fresh
            else "Last telemetry sample · stale"
        ),
        "observed_at": snapshot.get("observed_at"),
        "host_job_state": host_state if octoprint_mode else None,
        "physical_activity": {
            "status": (
                "conflict"
                if octoprint_mode
                and host_running
                and external_available
                and external.get("active") is False
                else external.get("status", "unavailable")
                if octoprint_mode
                else "available"
            ),
            "state": external.get("state", "unknown") if octoprint_mode else status,
            "source": external.get("source", "mks_wifi_http")
            if octoprint_mode
            else "mks_read_only_telemetry",
            "observed_at": external.get("observed_at")
            if octoprint_mode
            else snapshot.get("observed_at"),
            "confidence": external.get("confidence", "not_configured")
            if octoprint_mode
            else "firmware_reported",
            "error": external.get("error") if octoprint_mode else None,
        },
        "filename": filename,
        "progress_percent": progress,
        "elapsed_seconds": elapsed,
        "active_session": active_session,
        "recent_sessions": [session for session in sessions if session.get("id") != active_id],
        "history_source": "FastAPI server recording",
    }
