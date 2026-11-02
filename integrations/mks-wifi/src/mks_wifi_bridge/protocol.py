"""Verified read-only command and response presentation helpers."""

from __future__ import annotations

from typing import Any

from mkswifi import PrinterStatus

STATUS_COMMANDS = ("M105", "M997", "M27", "M994", "M992")
FIRMWARE_COMMANDS = ("M115",)
READ_ONLY_COMMANDS = STATUS_COMMANDS + FIRMWARE_COMMANDS
SD_LIST_COMMANDS = ("M20",)


def _field(value: Any, **extra: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "unsupported_or_not_reported", "value": None, **extra}
    return {"status": "available", "value": value, **extra}


def telemetry_payload(status: PrinterStatus, firmware: str | None) -> dict[str, Any]:
    """Convert the upstream typed response to stable, explicit JSON fields."""
    nozzle = status.nozzle
    bed = status.bed
    state = getattr(status.state, "value", str(status.state)).lower()
    known_state = state in {"idle", "printing", "paused"}
    active = state in {"printing", "paused"} if known_state else None

    return {
        "nozzle_temperature": _field(
            None if nozzle is None else nozzle.current,
            target_c=None if nozzle is None else nozzle.target,
            unit="°C",
        ),
        "bed_temperature": _field(
            None if bed is None else bed.current,
            target_c=None if bed is None else bed.target,
            unit="°C",
        ),
        "printer_state": {
            "status": "available" if known_state else "unsupported_or_not_reported",
            "value": state if known_state else None,
            "raw_value": state,
        },
        "print_active": {
            "status": "available" if known_state else "unsupported_or_not_reported",
            "value": active,
            "note": "A paused job is reported as active; unknown state remains null.",
        },
        "firmware_identification": _field(
            firmware,
            note=(
                "M115 identifies the MKS WiFi/board type in the inspected firmware; "
                "it may not identify the full Marlin build."
            ),
        ),
        "progress_percent": _field(status.progress, unit="%", source_command="M27"),
        "current_file": _field(status.file_name, source_command="M994"),
        "file_size_bytes": _field(status.file_size),
        "elapsed_seconds": _field(
            None if status.elapsed is None else int(status.elapsed.total_seconds()),
            source_command="M992",
        ),
    }
