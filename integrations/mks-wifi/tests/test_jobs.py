from mks_wifi_bridge.jobs import print_job_summary


def test_print_job_summary_separates_live_firmware_state_from_recorded_history() -> None:
    result = print_job_summary(
        {
            "connection": {"connected": True},
            "stale": False,
            "observed_at": "2026-11-05T12:00:00+00:00",
            "telemetry": {
                "print_active": {"status": "available", "value": True},
                "printer_state": {"status": "available", "value": "printing"},
                "current_file": {
                    "status": "available",
                    "value": "part.gcode",
                    "source_command": "M994",
                },
                "progress_percent": {"status": "available", "value": 38.5, "source_command": "M27"},
                "elapsed_seconds": {"status": "available", "value": 120, "source_command": "M992"},
            },
            "timeline": {
                "active_session": {"id": "active", "filename": "part.gcode"},
                "sessions": [
                    {"id": "active", "filename": "part.gcode"},
                    {"id": "previous", "filename": "older.gcode"},
                ],
            },
        }
    )

    assert result["status"] == "printing"
    assert result["active"] is True
    assert result["filename"] == {
        "status": "available",
        "value": "part.gcode",
        "source_command": "M994",
    }
    assert result["progress_percent"]["value"] == 38.5
    assert result["elapsed_seconds"]["value"] == 120
    assert result["recent_sessions"] == [{"id": "previous", "filename": "older.gcode"}]


def test_print_job_summary_never_calls_stale_telemetry_live() -> None:
    result = print_job_summary(
        {
            "connection": {"connected": False},
            "stale": True,
            "observed_at": "2026-11-05T12:00:00+00:00",
            "telemetry": {
                "print_active": {"status": "available", "value": True},
                "printer_state": {"status": "available", "value": "printing"},
                "current_file": {"status": "available", "value": "old.gcode"},
                "progress_percent": {"status": "available", "value": 45},
                "elapsed_seconds": {"status": "available", "value": 90},
            },
            "timeline": {"active_session": None, "sessions": []},
        }
    )

    assert result["status"] == "stale"
    assert result["active"] is None
    assert result["filename"]["status"] == "stale"
    assert result["progress_percent"]["status"] == "stale"
    assert result["elapsed_seconds"]["status"] == "stale"


def test_print_job_summary_rejects_impossible_progress_values() -> None:
    result = print_job_summary(
        {
            "connection": {"connected": True},
            "stale": False,
            "telemetry": {"progress_percent": {"status": "available", "value": 140}},
        }
    )

    assert result["progress_percent"] == {
        "status": "unavailable",
        "value": None,
        "source_command": None,
    }
