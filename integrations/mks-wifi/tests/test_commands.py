from __future__ import annotations

import re

import pytest

from mks_wifi_bridge.commands import DryRunCommandService, PreviewError, orbital_dance_commands
from mks_wifi_bridge.executor import _COMMAND_PATTERN


def test_catalogue_is_typed_and_execution_is_disabled() -> None:
    service = DryRunCommandService()
    catalogue = service.list_macros()
    ids = {macro["id"] for macro in catalogue}
    assert {"home_axes", "preheat_pla", "preheat_petg", "cooldown", "purge_nozzle"} <= ids
    assert {"filament_change_guided", "filament_load", "filament_unload", "park_nozzle"} <= ids
    assert {"set_fan_speed", "fan_off", "endstop_status", "cancel_print"} <= ids
    assert service.execution_enabled is False
    assert all(macro["enabled_for_execution"] is False for macro in catalogue)
    assert all(macro["requirements"] for macro in catalogue)
    assert all(macro["state_machine"] for macro in catalogue)
    assert all(macro["cancellation"] for macro in catalogue)


def test_orbital_dance_is_registered_as_a_bounded_preview_macro() -> None:
    service = DryRunCommandService()
    macro = next(
        item for item in service.list_macros() if item["id"] == "neptune_3_axis_orbital_dance"
    )
    assert macro["name"] == "Neptune 3-Axis Orbital Dance"
    assert macro["category"] == "macro"
    assert macro["command_families"] == ["G1", "G28", "M400"]
    assert macro["execution_block_reason"] is None
    assert [item["name"] for item in macro["parameters"]] == [
        "amplitude_percent",
        "feed_mm_min",
    ]
    assert macro["parameters"][1]["default"] == 6000
    assert macro["parameters"][1]["minimum"] == 3000
    assert macro["parameters"][1]["maximum"] == 9000
    assert macro["workflow"][0].startswith("Preflight:")
    assert macro["workflow"][-1].startswith("Home Again:")


def test_orbital_dance_preview_is_non_extruding_ordered_and_within_nominal_bounds() -> None:
    service = DryRunCommandService()
    preview = service.preview(
        "neptune_3_axis_orbital_dance", {"amplitude_percent": 7, "feed_mm_min": 6000}
    )
    commands = preview["commands"]
    assert preview["dry_run"] is True
    assert preview["execution_enabled"] is False
    assert preview["motion_classification"] == "planned_gcode_preview_only"
    assert len(commands) == 120
    assert commands[0] == "M400"
    assert commands[1] == "G28"
    assert commands[3] == "G90"
    assert commands[4] == "G1 Z30 F600"
    assert commands[5] == "G1 X110 Y110 F6000"
    assert commands[-2] == "G28"
    assert commands[-1] == "M400"
    assert commands[-3] == "M400"
    first_helix = [
        float(re.search(rf"\b{axis}(-?\d+(?:\.\d+)?)", commands[7]).group(1)) for axis in "XYZ"
    ]
    assert first_helix == [125.4, 110.0, 30.0]
    assert not any(" E" in command for command in commands)
    assert {command for command in commands if command.startswith("M")} == {"M400"}
    assert all(_COMMAND_PATTERN.fullmatch(command) for command in commands)
    for command in commands:
        if not command.startswith("G1 "):
            continue
        for axis in "XYZ":
            match = re.search(rf"\b{axis}(-?\d+(?:\.\d+)?)", command)
            if match:
                value = float(match.group(1))
                limit = 250 if axis == "Z" else 220
                assert 0 <= value <= limit


@pytest.mark.parametrize("amplitude,feed", [(2, 6000), (11, 6000), (7, 2999), (7, 9001)])
def test_orbital_dance_rejects_unsafe_parameters(amplitude: int, feed: int) -> None:
    with pytest.raises(PreviewError):
        orbital_dance_commands(amplitude, feed)


def test_purge_preview_renders_only_fixed_validated_commands() -> None:
    service = DryRunCommandService()
    preview = service.preview("purge_nozzle", {"length_mm": 12, "feed_mm_min": 180})
    assert preview["commands"] == ["M83", "G1 E12 F180", "M82"]
    assert preview["dry_run"] is True
    assert preview["execution_enabled"] is False
    assert preview["result"] == "preview_only"
    assert len(service.audit) == 1


def test_bounded_jog_previews_offer_requested_steps_and_validate_feedrates() -> None:
    service = DryRunCommandService()
    assert service.preview("jog_x_positive", {"distance_mm": "1", "feed_mm_min": 300})[
        "commands"
    ] == [
        "G91",
        "G1 X1 F300",
        "G90",
    ]
    assert service.preview("jog_z_negative", {"distance_mm": "-0.1", "feed_mm_min": 120})[
        "commands"
    ] == [
        "G91",
        "G1 Z-0.1 F120",
        "G90",
    ]
    macro = next(item for item in service.list_macros() if item["id"] == "jog_x_positive")
    assert macro["parameters"][0]["options"] == ["0.1", "1", "5", "10"]
    with pytest.raises(PreviewError, match="at most 300"):
        service.preview("jog_x_positive", {"distance_mm": "10", "feed_mm_min": 301})


def test_sd_print_preview_accepts_only_cached_m20_files() -> None:
    service = DryRunCommandService()
    with pytest.raises(PreviewError, match="listed SD-card file"):
        service.preview("start_sd_print", {"filename": "sample.gcode"})
    service.allowed_sd_files = {"sample.gcode"}
    assert service.preview("start_sd_print", {"filename": "sample.gcode"})["commands"] == [
        "M23 sample.gcode",
        "M24",
    ]
    with pytest.raises(PreviewError):
        service.preview("start_sd_print", {"filename": "../sample.gcode"})


@pytest.mark.parametrize(
    ("macro_id", "parameters"),
    [
        ("preheat_pla", {"temperature_c": 999, "bed_temperature_c": 60}),
        ("purge_nozzle", {"temperature_c": 200, "length_mm": -1, "feed_mm_min": 120}),
        ("filament_load", {"length_mm": 10, "feed_mm_min": 120, "gcode": "M112"}),
        ("not-a-macro", {}),
    ],
)
def test_invalid_or_unlisted_input_is_rejected(
    macro_id: str, parameters: dict[str, object]
) -> None:
    with pytest.raises(PreviewError):
        DryRunCommandService().preview(macro_id, parameters)


def test_guided_filament_change_contains_no_gcode() -> None:
    preview = DryRunCommandService().preview("filament_change_guided")
    assert preview["commands"] == []
    assert preview["workflow"]
    assert preview["notice"] == "Preview only. Nothing was sent to the printer."


def test_non_finite_numeric_values_are_rejected() -> None:
    with pytest.raises(PreviewError, match="finite"):
        DryRunCommandService().preview(
            "purge_nozzle", {"length_mm": float("nan"), "feed_mm_min": 120}
        )


def test_calibration_workflows_are_typed_and_never_generate_or_start_prints() -> None:
    calibrations = DryRunCommandService().list_calibrations()
    assert len(calibrations) == 5
    assert all(item["steps"] and item["prerequisites"] for item in calibrations)
    assert all(item["generates_gcode"] is False for item in calibrations)
    assert all(item["starts_print"] is False for item in calibrations)


def test_execution_attempt_is_blocked_and_never_dispatches() -> None:
    service = DryRunCommandService()
    result = service.reject_execution("cooldown")
    assert result["result"] == "blocked_execution_disabled"
    assert result["command_count"] == 0
    assert service.execution_enabled is False


def test_fan_and_diagnostic_previews_are_fixed_and_bounded() -> None:
    service = DryRunCommandService()
    assert service.preview("set_fan_speed", {"pwm": 180})["commands"] == ["M106 S180"]
    assert service.preview("fan_off")["commands"] == ["M107"]
    assert service.preview("endstop_status")["commands"] == ["M119"]
    assert service.preview("cancel_print")["commands"] == ["M524"]
    with pytest.raises(PreviewError):
        service.preview("set_fan_speed", {"pwm": 256})
