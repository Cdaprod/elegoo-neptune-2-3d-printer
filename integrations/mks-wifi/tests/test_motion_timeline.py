from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from mks_wifi_bridge.motion_timeline import (
    MotionTimeline,
    _axis_feedrates,
    _downsample_feedrate,
    interpret_gcode,
)


def test_interpreter_tracks_modal_modes_units_resets_and_moves() -> None:
    result = interpret_gcode(
        "G21\nG90\nM82\nG28 X\nG1 X10 F600\nG91\nM83\nG1 X2 E1 F300\nG92 X0 E0\nG1 X1 E2\n",
        "modes.gcode",
    )
    assert len(result["segments"]) == 3
    assert result["segments"][0]["to"]["x"] == 10
    assert result["segments"][0]["reference_kind"] == "simulated_gcode_home"
    assert result["segments"][1]["to"]["x"] == 12
    assert result["segments"][1]["to"]["e"] == 1
    assert result["segments"][2]["to"]["x"] == 13
    assert result["segments"][2]["to"]["e"] == 3
    assert result["segments"][0]["axis_velocities_mm_s"]["x"] == 10
    assert result["events"][0]["physical_homing_confirmed"] is False


def test_home_event_does_not_create_a_fake_motion_path() -> None:
    result = interpret_gcode("G28 X\nG28 Y\n", "home-only.gcode")
    assert result["segments"] == []
    assert len(result["events"]) == 2
    assert result["events"][0]["kind"] == "simulated_home_reference"


def test_storage_diagnostics_reports_database_and_persistent_location(tmp_path) -> None:
    db_path = tmp_path / "timeline.sqlite3"
    timeline = MotionTimeline(db_path)

    result = timeline.storage_diagnostics()

    assert result["database_bytes"] == db_path.stat().st_size
    assert result["persistent"] is False
    assert result["free_bytes"] > 0
    assert result["status"] == "non_persistent"


def test_storage_diagnostics_reports_low_space(monkeypatch, tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    monkeypatch.setattr(
        "mks_wifi_bridge.motion_timeline.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=990, free=10),
    )

    assert timeline.storage_diagnostics()["status"] == "low_space"


def test_interpreter_tracks_acceleration_and_idle_dwell_in_feedrate_profile() -> None:
    result = interpret_gcode("M204 S300\nG1 X10 F600\nG4 P2000\nG1 X20 F600\n", "timing.gcode")
    first, second = result["segments"]
    assert first["confidence"] == "trapezoidal_estimate_from_gcode_acceleration"
    assert first["feedrate_profile"][0]["feedrate_mm_min"] == 0
    assert first["feedrate_profile"][-1]["feedrate_mm_min"] == 0
    dwell = result["events"][0]
    assert dwell["kind"] == "planned_idle_dwell"
    assert second["start_seconds"] == first["end_seconds"] + 2


def test_timeline_exposes_offline_feedrate_with_zero_idle_gaps(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.store_gcode("G1 X10 F600\nG4 P1000\nG1 X20 F600\n", "demo.gcode")
    samples = timeline.snapshot()["offline_feedrate_history"]
    assert samples
    assert any(row["feedrate_mm_min"] > 0 for row in samples)
    assert any(row["feedrate_mm_min"] == 0 for row in samples)
    assert {row["data_class"] for row in samples} <= {
        "planned_feedrate",
        "planned_idle_interval",
    }


def test_builtin_offline_demo_uses_shared_interpreter_and_has_idle_gaps() -> None:
    demo = MotionTimeline.offline_demo()
    samples = demo["feedrate_history"]
    moving = [row for row in samples if row["feedrate_mm_min"] > 0]
    idle = [row for row in samples if row["feedrate_mm_min"] == 0]
    assert demo["source"] == "simulated_offline_fixture"
    assert len(demo["segments"]) == 3
    assert demo["segments"] == interpret_gcode(demo["gcode"], demo["filename"])["segments"]
    assert moving and idle
    assert all(row["data_class"] in {"simulated", "simulated_idle"} for row in samples)
    homes = [event for event in demo["events"] if event["kind"] == "simulated_home_reference"]
    assert homes and all(event["physical_homing_confirmed"] is False for event in homes)
    assert samples[0]["feedrate_mm_min"] == 0
    assert samples[-1]["feedrate_mm_min"] == 0
    assert not any(row["source"] == "firmware_reported" for row in samples)
    assert all(set(row["axis_feedrates_mm_min"]) == set("xyze") for row in samples)
    assert any(row["axis_feedrates_mm_min"]["x"] > 0 for row in moving)
    assert all(not row["axis_feedrates_mm_min"]["x"] for row in idle)


def test_axis_feedrate_uses_component_speed_not_a_shared_total() -> None:
    segment = {
        "axis_deltas_mm": {"x": 3, "y": 4, "z": 0, "e": 0},
        "path_distance_mm": 5,
    }
    assert _axis_feedrates(segment, 600) == {"x": 360, "y": 480, "z": 0, "e": 0}


def test_live_idle_feedrate_uses_persisted_poll_samples_and_separates_demo(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.store_gcode("G1 X10 F600\n", "demo.gcode")
    first = datetime.now(UTC).replace(microsecond=0)
    second = first + timedelta(seconds=2)
    timeline.record_snapshot(
        {
            "observed_at": first.isoformat(),
            "telemetry": {"printer_state": {"value": "idle"}, "print_active": {"value": False}},
        }
    )
    timeline.record_snapshot(
        {
            "observed_at": second.isoformat(),
            "telemetry": {"printer_state": {"value": "idle"}, "print_active": {"value": False}},
        }
    )
    snapshot = timeline.snapshot(
        live_snapshot={
            "stale": False,
            "connection": {"connected": True},
            "telemetry": {"printer_state": {"value": "idle"}},
        }
    )
    assert [row["feedrate_mm_min"] for row in snapshot["feedrate_history"]] == [0.0, 0.0]
    assert snapshot["offline_feedrate_history"]
    assert all(
        row["data_class"] == "firmware_reported_state_not_motor_feedback"
        for row in snapshot["feedrate_history"]
    )
    assert [row["observed_at"] for row in snapshot["feedrate_history"]] == [
        first.isoformat(),
        second.isoformat(),
    ]


def test_external_temperature_target_change_is_persisted_once(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")

    def poll(at: str, nozzle_target: float, bed_target: float = 0) -> dict:
        return {
            "observed_at": at,
            "telemetry": {
                "printer_state": {"value": "idle"},
                "print_active": {"value": False},
                "nozzle_temperature": {"value": 24, "target_c": nozzle_target},
                "bed_temperature": {"value": 24, "target_c": bed_target},
            },
        }

    first, changed, repeat = (
        "2026-11-02T12:00:00+00:00",
        "2026-11-02T12:00:02+00:00",
        "2026-11-02T12:00:04+00:00",
    )
    timeline.record_snapshot(poll(first, 0))
    timeline.record_snapshot(poll(changed, 200))
    timeline.record_snapshot(poll(repeat, 200))
    events = timeline.snapshot()["machine_state_events"]
    nozzle_events = [item for item in events if item["kind"] == "nozzle_target_changed"]
    assert len(nozzle_events) == 1
    assert nozzle_events[0]["data_class"] == "external_state_change_inferred"
    assert nozzle_events[0]["source"] == "unmatched_telemetry_change"
    assert nozzle_events[0]["details"]["value"] == 200
    assert any(
        item["kind"] == "nozzle_target_changed" for item in timeline.snapshot()["timeline_events"]
    )


def test_dashboard_temperature_target_change_correlates_to_command_without_claiming_tft(
    tmp_path,
) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    base = "2026-11-02T12:00:00+00:00"
    observed = "2026-11-02T12:00:02+00:00"

    def snapshot(at: str, target: float) -> dict:
        return {
            "observed_at": at,
            "telemetry": {
                "printer_state": {"value": "idle"},
                "print_active": {"value": False},
                "nozzle_temperature": {"value": 24, "target_c": target},
                "bed_temperature": {"value": 24, "target_c": 0},
            },
        }

    timeline.record_snapshot(snapshot(base, 0))
    timeline.record_snapshot(
        snapshot(observed, 200),
        operations=[
            {
                "id": "preheat-1",
                "command_results": [{"command": "M104 S200", "dispatched_at": base}],
            }
        ],
    )
    event = next(
        row
        for row in timeline.snapshot()["machine_state_events"]
        if row["kind"] == "nozzle_target_changed"
    )
    assert event["data_class"] == "firmware_state_observed"
    assert event["details"]["correlation"]["operation_id"] == "preheat-1"


def test_feedrate_downsampling_preserves_zero_motion_extrema_and_endpoints() -> None:
    rows = [
        {
            "observed_at": f"2026-11-01T12:{index // 60:02d}:{index % 60:02d}+00:00",
            "feedrate_mm_min": 0 if index % 10 else 600,
        }
        for index in range(10_000)
    ]
    reduced = _downsample_feedrate(rows)
    assert len(reduced) <= 1200
    assert reduced[0] == rows[0]
    assert reduced[-1] == rows[-1]
    assert any(row["feedrate_mm_min"] == 0 for row in reduced)
    assert any(row["feedrate_mm_min"] == 600 for row in reduced)


def test_bad_and_oversize_input_are_rejected() -> None:
    import pytest

    with pytest.raises(TypeError):
        interpret_gcode(b"G1 X1", "bad.gcode")
    with pytest.raises(ValueError, match="50 MB"):
        interpret_gcode("x" * (50 * 1024 * 1024 + 1), "large.gcode")


def test_backend_print_recording_survives_timeline_reopen_and_maps_path(tmp_path) -> None:
    db_path = tmp_path / "timeline.sqlite3"
    timeline = MotionTimeline(db_path)
    indexed = timeline.store_gcode("G90\nG1 X10 F600\nG1 X20 E1 F300\n", "Cube.gcode")
    assert indexed["segment_count"] == 2
    timeline.record_snapshot(
        {
            "observed_at": "2026-11-01T12:00:00+00:00",
            "poll_latency_ms": 32,
            "telemetry": {
                "printer_state": {"value": "printing"},
                "print_active": {"value": True},
                "current_file": {"value": "Cube.gcode"},
                "progress_percent": {"value": 20},
                "nozzle_temperature": {"value": 201, "target_c": 205},
                "bed_temperature": {"value": 59, "target_c": 60},
            },
        }
    )
    reopened = MotionTimeline(db_path)
    snapshot = reopened.snapshot()
    assert snapshot["recording"] == "backend_poller"
    assert snapshot["active_session"]["filename"] == "Cube.gcode"
    assert len(snapshot["telemetry"]) == 1
    assert snapshot["telemetry"][0]["latency_ms"] == 32
    assert snapshot["reconstruction"]["source"] == "MKS_progress_mapped_to_uploaded_G-code"
    assert snapshot["reconstruction"]["physical_position_confirmed"] is False
    assert snapshot["planned_history"]
    assert snapshot["planned_history"][0]["source"] == "uploaded_gcode_plan"
    assert any(row["feedrate_mm_min"] > 0 for row in snapshot["feedrate_history"])
    details = reopened.session_detail(snapshot["active_session"]["id"])
    assert details and details["feedrate_history"]


def test_idle_sample_closes_active_print_session(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.record_snapshot(
        {
            "observed_at": "2026-11-01T12:00:00+00:00",
            "telemetry": {
                "printer_state": {"value": "printing"},
                "print_active": {"value": True},
                "current_file": {"value": "x.gcode"},
                "progress_percent": {"value": 1},
            },
        }
    )
    timeline.record_snapshot(
        {
            "observed_at": "2026-11-01T12:01:00+00:00",
            "telemetry": {"printer_state": {"value": "idle"}, "print_active": {"value": False}},
        }
    )
    snapshot = timeline.snapshot()
    assert snapshot["active_session"] is None
    assert snapshot["sessions"][0]["ended_at"] == "2026-11-01T12:01:00+00:00"


def test_unmatched_active_filename_never_uses_an_unrelated_gcode_plan(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.store_gcode("G1 X10 F600", "other.gcode")
    timeline.record_snapshot(
        {
            "observed_at": "2026-11-01T12:00:00+00:00",
            "telemetry": {
                "printer_state": {"value": "printing"},
                "print_active": {"value": True},
                "current_file": {"value": "unknown.gcode"},
                "progress_percent": {"value": 20},
            },
        }
    )
    snapshot = timeline.snapshot()
    assert snapshot["active_session"]["file_id"] is None
    assert snapshot["reconstruction"] is None
    assert snapshot["planned_history"] == []


def test_acknowledged_jog_is_a_commanded_estimate_not_observed_motion(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.record_snapshot(
        {"observed_at": "2026-11-01T12:00:00+00:00", "telemetry": {}},
        operations=[
            {
                "id": "jog-0-1",
                "macro_id": "jog_x_positive",
                "state": "transport_acknowledged",
                "created_at": "2026-11-01T11:59:00+00:00",
                "updated_at": "2026-11-01T11:59:01+00:00",
                "commands": ["G91", "G1 X0.1 F300", "G90"],
                "command_results": [
                    {
                        "command": "G1 X0.1 F300",
                        "state": "transport_acknowledged",
                        "dispatched_at": "2026-11-01T11:59:00+00:00",
                    }
                ],
                "proposed_position_mm": 0.1,
                "physical_completion_confirmed": False,
                "transport_acknowledged": True,
            }
        ],
    )
    planned = timeline.snapshot()["planned_history"]
    jog = [row for row in planned if row["source"] == "commanded_operation"]
    assert len(jog) == 2
    assert jog[0]["axes"]["x"]["value_mm"] == 0.0
    assert jog[1]["axes"]["x"]["value_mm"] == 0.1
    assert jog[1]["confidence"] == "acknowledged_target_not_physical"
    assert jog[1]["interval"]["axis_velocities_mm_s"]["x"] == 5.0


def test_acknowledged_jog_feedrate_is_a_timed_estimate_not_a_completion_event(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.record_snapshot(
        {"observed_at": "2026-11-01T12:00:00+00:00", "telemetry": {}},
        operations=[
            {
                "id": "jog-feedrate",
                "macro_id": "jog_x_positive",
                "state": "transport_acknowledged",
                "commands": ["G91", "G1 X5 F300", "G90"],
                "command_results": [
                    {
                        "command": "G1 X5 F300",
                        "state": "transport_acknowledged",
                        "dispatched_at": "2026-11-01T11:59:00+00:00",
                    }
                ],
                "transport_acknowledged": True,
                "physical_completion_confirmed": False,
            }
        ],
    )
    curve = [
        row
        for row in timeline.snapshot()["feedrate_history"]
        if row["operation_id"] == "jog-feedrate"
    ]
    assert [row["feedrate_mm_min"] for row in curve] == [0.0, 300.0, 0.0]
    assert all(
        row["confidence"] == "planned_duration_from_feedrate_ack_not_completion" for row in curve
    )


def test_dry_run_jog_does_not_create_a_commanded_path(tmp_path) -> None:
    timeline = MotionTimeline(tmp_path / "timeline.sqlite3")
    timeline.record_snapshot(
        {"observed_at": "2026-11-01T12:00:00+00:00", "telemetry": {}},
        operations=[
            {
                "id": "jog-dry-run",
                "macro_id": "jog_x_positive",
                "state": "dry_run",
                "created_at": "2026-11-01T11:59:00+00:00",
                "commands": ["G91", "G1 X1 F300", "G90"],
                "command_results": [],
                "proposed_position_mm": 1.0,
                "transport_acknowledged": False,
            }
        ],
    )
    assert timeline.snapshot()["planned_history"] == []
