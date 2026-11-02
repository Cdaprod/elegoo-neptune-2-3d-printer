from __future__ import annotations

from datetime import timedelta

from mkswifi import parse_status

from mks_wifi_bridge.protocol import (
    FIRMWARE_COMMANDS,
    READ_ONLY_COMMANDS,
    STATUS_COMMANDS,
    telemetry_payload,
)


def test_read_only_command_allowlist_has_no_mutating_operations() -> None:
    assert STATUS_COMMANDS == ("M105", "M997", "M27", "M994", "M992")
    assert FIRMWARE_COMMANDS == ("M115",)
    assert set(READ_ONLY_COMMANDS) == {"M105", "M997", "M27", "M994", "M992", "M115"}
    assert not {"M23", "M24", "M25", "M28", "M29", "M104", "M140", "G0", "G28"} & set(
        READ_ONLY_COMMANDS
    )


def test_known_response_parsing_reports_telemetry() -> None:
    status = parse_status(
        "ok\nT:201.5 /205 B:60 /60\nM997 PRINTING\nM27 42\n"
        "M994 1:/sample.gcode;12345\nM992 00:12:34\n"
    )
    payload = telemetry_payload(status, "TFT24")
    assert payload["nozzle_temperature"] == {
        "status": "available",
        "value": 201.5,
        "target_c": 205.0,
        "unit": "°C",
    }
    assert payload["bed_temperature"]["value"] == 60.0
    assert payload["printer_state"]["value"] == "printing"
    assert payload["print_active"]["value"] is True
    assert payload["firmware_identification"]["value"] == "TFT24"
    assert payload["progress_percent"]["value"] == 42
    assert payload["current_file"]["value"] == "sample.gcode"
    assert payload["elapsed_seconds"]["value"] == int(
        timedelta(minutes=12, seconds=34).total_seconds()
    )


def test_missing_and_malformed_replies_are_explicitly_unavailable() -> None:
    status = parse_status("noise\nok\nM997 BUSY\nM27 ??\n")
    payload = telemetry_payload(status, None)
    assert payload["nozzle_temperature"]["status"] == "unsupported_or_not_reported"
    assert payload["bed_temperature"]["status"] == "unsupported_or_not_reported"
    assert payload["printer_state"]["status"] == "unsupported_or_not_reported"
    assert payload["print_active"]["value"] is None
    assert payload["firmware_identification"]["status"] == "unsupported_or_not_reported"
