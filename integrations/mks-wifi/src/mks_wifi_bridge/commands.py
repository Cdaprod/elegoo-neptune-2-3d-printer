"""Typed command catalogue, validation, and local previews.

Transport execution is isolated in ``executor`` and remains disabled by default.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class PreviewError(ValueError):
    """A macro identifier or parameter is invalid for preview."""


@dataclass(frozen=True)
class Parameter:
    name: str
    label: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    default: float | str | None = None
    options: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "default": self.default,
            "options": list(self.options),
        }


@dataclass(frozen=True)
class Macro:
    id: str
    name: str
    description: str
    category: str
    icon: str
    risk: str
    compatibility: str
    requirements: tuple[str, ...]
    parameters: tuple[Parameter, ...] = ()
    templates: tuple[str, ...] = ()
    workflow: tuple[str, ...] = ()
    enabled_for_execution: bool = False
    state_machine: tuple[str, ...] = (
        "draft",
        "validated",
        "awaiting_operator_confirmation",
        "queued",
        "running",
        "transport_acknowledged | failed | outcome_unknown | cancelled_before_next_command",
    )
    cancellation: str = (
        "Cancellation prevents commands not yet sent. It cannot interrupt an in-flight command "
        "and is not an emergency stop."
    )
    execution_block_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "icon": self.icon,
            "risk": self.risk,
            "compatibility": self.compatibility,
            "requirements": list(self.requirements),
            "parameters": [parameter.as_dict() for parameter in self.parameters],
            "command_families": sorted(
                {template.split(maxsplit=1)[0] for template in self.templates}
            ),
            "workflow": list(self.workflow),
            "enabled_for_execution": self.enabled_for_execution,
            "state_machine": list(self.state_machine),
            "cancellation": self.cancellation,
            "execution_block_reason": self.execution_block_reason,
        }


TEMP = Parameter("temperature_c", "Nozzle target", "number", 0, 260, 200)
BED = Parameter("bed_temperature_c", "Bed target", "number", 0, 100, 60)
LENGTH = Parameter("length_mm", "Extrusion length", "number", 1, 50, 10)
FEED = Parameter("feed_mm_min", "Feed rate", "number", 60, 600, 120)
SD_FILE = Parameter("filename", "SD card file", "file")
FAN_PWM = Parameter("pwm", "Fan output (0–255)", "number", 0, 255, 128)
ORBITAL_AMPLITUDE = Parameter(
    "amplitude_percent", "Orbit size (% of nominal XY travel)", "number", 3, 10, 7
)
ORBITAL_FEED = Parameter(
    "feed_mm_min", "Motion speed (mm/min; 6000 = 100 mm/s)", "number", 3000, 9000, 6000
)


def _format_coordinate(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def orbital_dance_commands(amplitude_percent: float, feed_mm_min: float) -> list[str]:
    """Create the fixed, non-extruding dance within the nominal build envelope.

    The envelope matches Neptune Observatory's published 220 x 220 x 250 mm
    printable volume. It is deliberately treated as nominal: firmware offsets
    and EEPROM travel settings are not available through the installed MKS
    telemetry path, so this path is suitable for review and simulation only.
    """
    if not math.isfinite(amplitude_percent) or not 3 <= amplitude_percent <= 10:
        raise PreviewError("Orbit size must be between 3% and 10% of nominal XY travel.")
    if not math.isfinite(feed_mm_min) or not 3000 <= feed_mm_min <= 9000:
        raise PreviewError("Motion speed must be between 3000 and 9000 mm/min (50–150 mm/s).")

    x_min, x_max = 0.0, 220.0
    y_min, y_max = 0.0, 220.0
    z_min, z_max = 0.0, 250.0
    cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
    radius = min(x_max - x_min, y_max - y_min) * amplitude_percent / 100
    z_base = z_min + 30.0
    helix_height = 12.0
    vertical_wave = min(5.0, (z_max - z_min) * 0.025)
    speed = _format_coordinate(feed_mm_min)
    helix_points: list[tuple[float, float, float]] = []

    # Two-turn helix, 24 line segments per turn.
    for index in range(49):
        angle = 4 * math.pi * index / 48
        helix_points.append(
            (
                cx + radius * math.cos(angle),
                cy + radius * math.sin(angle),
                z_base + helix_height * index / 48,
            )
        )

    # Figure eight, with a modest vertical oscillation.
    figure_z = z_base + helix_height
    figure_points: list[tuple[float, float, float]] = []
    for index in range(1, 33):
        angle = 2 * math.pi * index / 32
        figure_points.append(
            (
                cx + radius * math.sin(angle),
                cy + radius * math.sin(2 * angle),
                figure_z + vertical_wave * math.sin(angle),
            )
        )

    # Phase-shifted XYZ wave; the first line enters this phase from the end
    # of the figure eight and remains within the same validated bounds.
    wave_z = figure_z
    wave_points: list[tuple[float, float, float]] = []
    for index in range(25):
        angle = 2 * math.pi * index / 24
        wave_points.append(
            (
                cx + radius * math.sin(angle),
                cy + radius * math.sin(angle + 2 * math.pi / 3),
                wave_z + vertical_wave * math.sin(angle + 4 * math.pi / 3),
            )
        )

    # Validate every point before any command list is exposed to the executor.
    points = [*helix_points, *figure_points, *wave_points]
    for x, y, z in points:
        if not (x_min <= x <= x_max and y_min <= y <= y_max and z_min <= z <= z_max):
            raise PreviewError("Generated dance path exceeds the nominal machine envelope.")

    commands = [
        # Empty planner barrier is a harmless compatibility preflight: if the
        # installed firmware rejects M400, no homing or travel has begun.
        "M400",
        "G28",
        "M400",
        "G90",
        # Lift at 10 mm/s; the printer's own configured Z limit remains authoritative.
        f"G1 Z{_format_coordinate(z_base)} F600",
        f"G1 X{_format_coordinate(cx)} Y{_format_coordinate(cy)} F{speed}",
        "M400",
    ]

    def move(x: float, y: float, z: float) -> str:
        return (
            f"G1 X{_format_coordinate(x)} Y{_format_coordinate(y)} "
            f"Z{_format_coordinate(z)} F{speed}"
        )

    for phase_points in (helix_points, figure_points, wave_points):
        commands.extend(move(x, y, z) for x, y, z in phase_points)
        commands.append("M400")
    commands.extend(
        (
            f"G1 X{_format_coordinate(cx)} Y{_format_coordinate(cy)} "
            f"Z{_format_coordinate(z_base)} F{speed}",
            "M400",
            "G28",
            "M400",
        )
    )
    if len(commands) > 120:
        raise PreviewError("Generated dance exceeds the bounded 120-command sequence limit.")
    return commands


def _jog_macro(axis: str, direction: str, feed: int) -> Macro:
    steps = ("0.1", "1", "5", "10")
    options = steps if direction == "positive" else tuple("-" + step for step in steps)
    return Macro(
        f"jog_{axis}_{direction}",
        f"Jog {axis.upper()} {direction.title()}",
        f"Move {axis.upper()} by a bounded relative step; requires a confirmed home.",
        "control",
        "↔" if axis in {"x", "y"} else "↕",
        "high",
        "documented_unverified",
        (
            f"{axis.upper()} must be homed and its last motion confirmed at the printer.",
            "The dashboard brackets the step with G91/G90; remain at the machine.",
        ),
        (
            Parameter("distance_mm", "Jog distance and direction", "select", options=options),
            Parameter(
                "feed_mm_min", "Conservative feedrate (mm/min)", "number", 60, feed, min(feed, 300)
            ),
        ),
        ("G91", f"G1 {axis.upper()}{{distance_mm}} F{{feed_mm_min}}", "G90"),
    )


MACROS: tuple[Macro, ...] = (
    Macro(
        "home_x",
        "Home X",
        "Home the X axis; optionally use a visible 30 mm retreat when it is already at the switch.",
        "control",
        "↔",
        "high",
        "documented_unverified",
        (
            "Only select the visible retreat when the X carriage is resting at its home switch.",
            "Confirm the full X travel path is clear and watch the carriage return to its switch.",
        ),
        templates=("G28 X",),
    ),
    Macro(
        "home_y",
        "Home Y",
        "Home the Y axis; optionally use a visible 30 mm retreat when it is already at the switch.",
        "control",
        "↕",
        "high",
        "documented_unverified",
        (
            "Only select the visible retreat when the bed is resting at its home switch.",
            "Keep the full bed travel path clear and watch it return to its switch.",
        ),
        templates=("G28 Y",),
    ),
    Macro(
        "home_z",
        "Home Z",
        (
            "Home Z; optionally use a visible 30 mm retreat when it is already at the switch. "
            "Firmware safe-homing may also move X/Y.",
        ),
        "control",
        "↟",
        "high",
        "documented_unverified",
        (
            "Select the visible retreat only when Z is at home and the raise is clear.",
            "Clear the bed; firmware safe-homing may also move X/Y.",
        ),
        templates=("G28 Z",),
    ),
    Macro(
        "home_axes",
        "Home All",
        (
            "Home all axes; optionally use visible 30 mm retreats when all axes are at their "
            "switches."
        ),
        "control",
        "⌂",
        "high",
        "documented_unverified",
        (
            "Only select visible retreats when all three axes are at their home switches.",
            "Keep carriage, bed, gantry, cable, and nozzle paths clear while watching the return.",
        ),
        templates=("G28",),
    ),
    Macro(
        "set_nozzle_temperature",
        "Set nozzle target",
        "Set a bounded nozzle temperature target.",
        "control",
        "♨",
        "high",
        "documented_unverified",
        (
            "Stay with the printer and verify the requested target in Neptune Observatory's "
            "fresh temperature telemetry.",
        ),
        (Parameter("temperature_c", "Nozzle target", "number", 0, 260, 200),),
        ("M104 S{temperature_c}",),
    ),
    Macro(
        "set_bed_temperature",
        "Set bed target",
        "Set a bounded bed temperature target.",
        "control",
        "♨",
        "high",
        "documented_unverified",
        (
            "Stay with the printer and verify the requested target in Neptune Observatory's "
            "fresh temperature telemetry.",
        ),
        (Parameter("temperature_c", "Bed target", "number", 0, 100, 60),),
        ("M140 S{temperature_c}",),
    ),
    Macro(
        "preheat_pla",
        "Preheat PLA",
        "Set nozzle and bed targets to common PLA preview defaults.",
        "control",
        "♨",
        "high",
        "documented_unverified",
        ("Confirm material and machine limits before any future activation.",),
        (TEMP, BED),
        ("M104 S{temperature_c}", "M140 S{bed_temperature_c}"),
    ),
    Macro(
        "preheat_petg",
        "Preheat PETG",
        "Set nozzle and bed targets to common PETG preview defaults.",
        "control",
        "♨",
        "high",
        "documented_unverified",
        ("Targets are editable preview defaults, not a printer profile.",),
        (
            Parameter("temperature_c", "Nozzle target", "number", 0, 260, 235),
            Parameter("bed_temperature_c", "Bed target", "number", 0, 100, 80),
        ),
        ("M104 S{temperature_c}", "M140 S{bed_temperature_c}"),
    ),
    Macro(
        "cooldown",
        "Cooldown",
        "Set nozzle and bed targets to zero.",
        "control",
        "◌",
        "medium",
        "documented_unverified",
        ("Cooling is a state-changing operation; actual execution remains disabled.",),
        templates=("M104 S0", "M140 S0"),
    ),
    Macro(
        "motors_enable",
        "Enable motors",
        "Enable stepper drivers. This does not move axes.",
        "control",
        "◉",
        "medium",
        "documented_unverified",
        ("Keep clear of the printer; enabling drivers may make motors hold position.",),
        templates=("M17",),
    ),
    Macro(
        "motors_disable",
        "Disable motors",
        "Disable stepper drivers. The Z gantry may drop if its motors release.",
        "control",
        "◯",
        "high",
        "documented_unverified",
        ("Support the gantry and bed; disabling drivers may release gravity-loaded axes.",),
        templates=("M84",),
    ),
    Macro(
        "set_fan_speed",
        "Set part cooling fan",
        "Set a bounded part cooling fan output using the firmware PWM scale.",
        "control",
        "✣",
        "medium",
        "documented_unverified",
        ("Confirm the correct fan is connected and remain beside the printer.",),
        (FAN_PWM,),
        ("M106 S{pwm}",),
    ),
    Macro(
        "fan_off",
        "Turn fan off",
        "Turn off the part cooling fan.",
        "control",
        "✣",
        "low",
        "documented_unverified",
        ("Confirm the requested fan is the part cooling fan.",),
        templates=("M107",),
    ),
    Macro(
        "endstop_status",
        "Read endstop status",
        "Query firmware endstop states; the response is shown in the operation log.",
        "control",
        "⌖",
        "low",
        "documented_unverified",
        ("Keep the printer idle; readings describe switch logic, not axis position.",),
        templates=("M119",),
    ),
    Macro(
        "neptune_3_axis_orbital_dance",
        "Neptune 3-Axis Orbital Dance",
        (
            "Coordinated XYZ helix, figure-eight, and wave choreography with safe park "
            "and final homing."
        ),
        "macro",
        "✧",
        "high",
        "documented_unverified",
        (
            "Preview uses the nominal 220 × 220 × 250 mm printable volume; installed firmware "
            "travel limits and offsets are not readable from current telemetry.",
            "All axes must home correctly; keep the entire bed, carriage, gantry, cables, and "
            "nozzle path clear for the full choreography.",
            "The installed firmware must accept Marlin M400 planner-finish commands. The first "
            "M400 is a no-motion compatibility check; if rejected, execution stops before homing.",
            "Nozzle and bed targets must already be off and temperatures safe. This macro "
            "does not heat, extrude, or print.",
            "Motion feed is 50–150 mm/s (100 mm/s default). Firmware maximum feedrate and "
            "acceleration may limit actual speed; telemetry does not report measured speed.",
            "A final G28 is included only at the end of normal completion. Cancel or transport "
            "failure stops before that command; it will not automatically re-home.",
        ),
        (ORBITAL_AMPLITUDE, ORBITAL_FEED),
        ("M400", "G28", "G1 X110 Y110 Z30 F6000"),
        workflow=(
            "Preflight: M400 checks the motion queue without moving; then home with G28 and "
            "wait at M400.",
            "Set absolute positioning with G90.",
            "Lift: raise Z to 30 mm and travel to the center of the nominal build area.",
            "M400 waits for the lift and center positioning to finish.",
            "Helix: two XY turns while Z rises 12 mm, followed by an M400 completion barrier.",
            "Figure Eight: coordinated X/Y figure eight with a small Z oscillation, then M400.",
            "XYZ Wave: phase-shift X, Y, and Z through one cycle, then M400.",
            "Park: return to the elevated center point and wait at M400.",
            "Home Again: final G28 is sent only after every prior phase's M400 is acknowledged; "
            "the final M400 waits for homing to finish.",
        ),
    ),
    Macro(
        "purge_nozzle",
        "Purge nozzle",
        "Extrude a measured length only at an already-hot, verified nozzle temperature.",
        "macro",
        "⇣",
        "high",
        "documented_unverified",
        (
            "Nozzle must be hot; extrusion mode and firmware limits must be verified "
            "before activation.",
        ),
        (LENGTH, FEED),
        ("M83", "G1 E{length_mm} F{feed_mm_min}", "M82"),
    ),
    Macro(
        "nozzle_preparation",
        "Pre-print nozzle preparation",
        "Wait for the nozzle to reach the selected target.",
        "macro",
        "⌁",
        "high",
        "documented_unverified",
        ("M109 may block command processing while waiting.",),
        (TEMP,),
        ("M109 S{temperature_c}",),
    ),
    Macro(
        "filament_change_guided",
        "Guided filament change",
        "A manual, operator-led workflow with no printer commands.",
        "macro",
        "⟳",
        "medium",
        "workflow_only",
        (
            "Printer state must be freshly observed as idle; this workflow cannot "
            "pause an active print.",
            "Use Operator Mode temperature and filament controls; fresh nozzle "
            "telemetry gates extrusion.",
            "Confirm filament seating and nozzle clearance before finishing the checklist.",
        ),
        workflow=(
            "Confirm Observatory reports idle; stop if a print is active or state is unknown.",
            "Select the material and set a suitable target using the guarded Observatory controls.",
            "Wait in Observatory until live nozzle telemetry reaches and stabilizes "
            "near the target. Do not extrude while cold.",
            "Perform the physical filament handoff while watching the printer, then use "
            "the guarded Filament Load or Unload action after its live temperature check passes.",
            "Inspect the extruder path and nozzle locally, then confirm the result "
            "in Observatory. This workflow never depends on a TFT prompt and does "
            "not start or resume a print.",
        ),
        state_machine=(
            "ready",
            "idle_confirmed",
            "dashboard_preheat_and_telemetry_gate",
            "manual_filament_change",
            "operator_verified_filament_path",
            "completed | cancelled | blocked",
        ),
        cancellation=(
            "Stop the checklist and leave the printer in its current state. "
            "Use a guarded Observatory control or the physical "
            "power switch for recovery; this workflow does not resume a print."
        ),
    ),
    Macro(
        "filament_load",
        "Filament load",
        "Move bounded filament using fixed relative extrusion, then restore absolute mode.",
        "macro",
        "⇧",
        "high",
        "documented_unverified",
        ("Requires fresh nozzle telemetry at or above the configured extrusion minimum.",),
        (LENGTH, FEED),
        ("M83", "G1 E{length_mm} F{feed_mm_min}", "M82"),
    ),
    Macro(
        "filament_unload",
        "Filament unload",
        "Retract bounded filament using fixed relative extrusion, then restore absolute mode.",
        "macro",
        "⇩",
        "high",
        "documented_unverified",
        ("Requires fresh nozzle telemetry at or above the configured extrusion minimum.",),
        (LENGTH, FEED),
        ("M83", "G1 E-{length_mm} F{feed_mm_min}", "M82"),
    ),
    Macro(
        "park_nozzle",
        "Park nozzle",
        "A park location cannot be previewed safely without verified machine bounds "
        "and park configuration.",
        "macro",
        "⌖",
        "high",
        "unknown",
        ("Machine coordinate bounds and safe park position are unknown.",),
    ),
    *(
        _jog_macro(axis, direction, feed)
        for axis, feed in (("x", 300), ("y", 300), ("z", 120))
        for direction in ("positive", "negative")
    ),
    Macro(
        "start_sd_print",
        "Start SD card print",
        "Select a listed SD-card file and start it.",
        "control",
        "▶",
        "high",
        "documented_unverified",
        ("Review the exact filename and ensure the bed and toolhead are clear.",),
        (SD_FILE,),
        ("M23 {filename}", "M24"),
    ),
    Macro(
        "print_first_layer_sd",
        "Print first-layer-only SD file",
        "Start a dedicated G-code file containing only the first layer. "
        "A normal multi-layer file is not stopped automatically.",
        "macro",
        "▤",
        "high",
        "documented_unverified",
        (
            "Choose the prepared one-layer G-code file. The filename and M20 listing do not "
            "prove its contents; explicitly confirm this exact file contains only layer one.",
            "Confirm the bed is clear, the correct material is loaded, and remain at the printer.",
        ),
        (SD_FILE,),
        ("M23 {filename}", "M24"),
    ),
    Macro(
        "pause_print",
        "Pause print",
        "Request a firmware pause for the active SD-card print.",
        "control",
        "Ⅱ",
        "medium",
        "documented_unverified",
        ("Use only while the printer reports an active print; remain nearby.",),
        templates=("M25",),
    ),
    Macro(
        "resume_print",
        "Resume print",
        "Request resume of the firmware-paused SD-card print.",
        "control",
        "▶",
        "high",
        "documented_unverified",
        ("Check the nozzle, bed, and print before resuming.",),
        templates=("M24",),
    ),
    Macro(
        "cancel_print",
        "Cancel active print",
        "Request Marlin's SD print abort command; exact support is not yet verified.",
        "control",
        "■",
        "high",
        "documented_unverified",
        ("Only use for a confirmed active SD print; remain at the printer.",),
        templates=("M524",),
    ),
)


@dataclass(frozen=True)
class CalibrationWorkflow:
    id: str
    name: str
    description: str
    steps: tuple[str, ...]
    prerequisites: tuple[str, ...]
    state_machine: tuple[str, ...] = ("ready", "operator_review", "prepared", "cancelled")
    cancellation: str = "Stop the checklist at any time; it sends no printer commands."

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "steps": list(self.steps),
            "prerequisites": list(self.prerequisites),
            "state_machine": list(self.state_machine),
            "cancellation": self.cancellation,
            "generates_gcode": False,
            "starts_print": False,
        }


CALIBRATIONS: tuple[CalibrationWorkflow, ...] = (
    CalibrationWorkflow(
        "first_layer",
        "First-layer test",
        "Prepare a first-layer test in OrcaSlicer; this dashboard creates no model or G-code.",
        (
            "Review bed cleanliness, build-plate installation, and current "
            "temperatures at the printer.",
            "In OrcaSlicer, select the Neptune 2 profile and a known material preset.",
            "Choose a small first-layer calibration model from OrcaSlicer's own workflow.",
            "Review the preview, start G-code, target temperatures, and estimated duration.",
            "After review, transfer the export, refresh the SD picker, "
            "then use the guarded print action.",
        ),
        (
            "Printer is idle and telemetry is fresh.",
            "Material and plate are known.",
            "OrcaSlicer profile is reviewed.",
        ),
    ),
    CalibrationWorkflow(
        "bed_level",
        "Bed-level test pattern",
        "A bed-level pattern must be prepared and sliced in OrcaSlicer.",
        (
            "Confirm the printer is idle and the bed is clear.",
            "Choose a reviewed pattern in OrcaSlicer and select the Neptune 2 profile.",
            "Inspect the complete travel path and start G-code in Preview.",
            "After review, transfer the export, refresh the SD picker, "
            "then use the guarded print action.",
        ),
        ("Printer is idle and telemetry is fresh.", "Travel and start G-code are reviewed."),
    ),
    CalibrationWorkflow(
        "temperature_tower",
        "Temperature tower",
        "Use OrcaSlicer's calibration workflow and review its generated temperature changes.",
        (
            "Select the correct filament and material range in OrcaSlicer.",
            "Generate the tower using OrcaSlicer's calibration workflow.",
            "Inspect every temperature-change command and confirm the range suits the filament.",
            "Preview the complete file, transfer it, refresh the SD "
            "picker, and use the guarded print action.",
        ),
        ("Printer is idle and telemetry is fresh.", "Material temperature range is known."),
    ),
    CalibrationWorkflow(
        "retraction",
        "Retraction test",
        "Prepare the test in OrcaSlicer with the reviewed Neptune 2 material profile.",
        (
            "Verify the selected material and nozzle are correct.",
            "Use OrcaSlicer's calibration workflow with bounded retraction values.",
            "Inspect the retraction towers and generated settings in Preview.",
            "Transfer the reviewed G-code, refresh the SD picker, and start it through the guarded "
            "print action.",
        ),
        (
            "Printer is idle and telemetry is fresh.",
            "Extrusion mode and material profile are reviewed.",
        ),
    ),
    CalibrationWorkflow(
        "flow",
        "Flow calibration",
        "Prepare the calibration in OrcaSlicer; this dashboard does not fabricate a print file.",
        (
            "Confirm material, nozzle size, and the current OrcaSlicer profile.",
            "Select an OrcaSlicer flow-calibration method appropriate to the profile.",
            "Review generated walls, flow modifiers, and estimated material use.",
            "Transfer the reviewed G-code, refresh the SD picker, and start it through the guarded "
            "print action.",
        ),
        ("Printer is idle and telemetry is fresh.", "Nozzle size and material are known."),
    ),
)

CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "command": "G28 X",
        "purpose": "Home X",
        "status": "operator_verified_on_this_printer",
        "basis": (
            "The operator previously observed the X carriage reach its reference switch and "
            "stop normally after dashboard G28 X. Runtime home state is cleared on restart."
        ),
    },
    {
        "command": "G28 Y/Z/all",
        "purpose": "Home other axes",
        "status": "documented_unverified",
        "basis": (
            "Fixed G28 variants are exposed through Operator Mode. Runtime responses are recorded; "
            "an acknowledgment alone is not physical homing evidence."
        ),
    },
    {
        "command": "M104 / M109",
        "purpose": "Set / wait for hotend target",
        "status": "limited_transport_acknowledgment",
        "basis": (
            "M104 S0 received an MKS TCP 'ok'; the target was already 0 °C. "
            "No target transition was demonstrated; M104 above zero and M109 remain unverified."
        ),
    },
    {
        "command": "M140 / M190",
        "purpose": "Set / wait for bed target",
        "status": "limited_transport_acknowledgment",
        "basis": (
            "M140 S0 received an MKS TCP 'ok'; the target was already 0 °C. "
            "No target transition was demonstrated; M140 above zero and M190 remain unverified."
        ),
    },
    {
        "command": "G1 X",
        "purpose": "Bounded X jog",
        "status": "awaiting_home_x_observation",
        "basis": (
            "Bounded jogs are available in Operator Mode after a current-session home reference "
            "and fresh idle telemetry."
        ),
    },
    {
        "command": "G1 E",
        "purpose": "Controlled extrusion / retract",
        "status": "documented_unverified",
        "basis": "Marlin documents linear moves; extrusion mode and MKS forwarding are unknown.",
    },
    {
        "command": "M400",
        "purpose": "Wait for queued movement to finish",
        "status": "documented_unverified",
        "basis": (
            "Marlin documents M400 as a planner-finish barrier. The TCP bridge forwards generic "
            "G-code, but M400 behavior on this installed Elegoo build has not been tested."
        ),
    },
    {
        "command": "M600",
        "purpose": "Filament change",
        "status": "requires_firmware_configuration",
        "basis": "Marlin requires Advanced Pause configuration; Elegoo build flags are unknown.",
    },
    {
        "command": "M701 / M702",
        "purpose": "Load / unload",
        "status": "requires_firmware_configuration",
        "basis": "Marlin documents these behind filament load/unload configuration; "
        "Elegoo build flags are unknown.",
    },
    {
        "command": "G12",
        "purpose": "Nozzle cleaning motion",
        "status": "requires_firmware_configuration",
        "basis": "Marlin requires nozzle-clean feature and configured physical wipe "
        "area; neither is verified.",
    },
    {
        "command": "M20",
        "purpose": "List SD card",
        "status": "supported_live_read_only",
        "basis": "Existing read-only MKS root-listing integration is implemented; "
        "no file contents are exposed.",
    },
    {
        "command": "M17 / M18 / M84",
        "purpose": "Enable / release stepper drivers",
        "status": "documented_unverified",
        "basis": (
            "Fixed templates are available for separate supervised commissioning; their "
            "physical effects remain unverified."
        ),
    },
    {
        "command": "M106 / M107",
        "purpose": "Set / disable part cooling fan",
        "status": "documented_unverified",
        "basis": (
            "Fixed macros use bounded PWM values, but fan wiring and MKS execution are "
            "not physically verified."
        ),
    },
    {
        "command": "M119",
        "purpose": "Read endstop states",
        "status": "documented_unverified",
        "basis": (
            "Read-only diagnostic template exists; response format on this Elegoo firmware "
            "is not yet tested."
        ),
    },
    {
        "command": "M23 / M24 / M25",
        "purpose": "Select/start/resume/pause SD print",
        "status": "documented_unverified",
        "basis": (
            "Only a file present in the latest M20 listing is selectable; print-control "
            "behavior is not verified on this MKS connection."
        ),
    },
    {
        "command": "M524",
        "purpose": "Abort active SD print",
        "status": "documented_unverified",
        "basis": (
            "Marlin documents M524, but the installed Elegoo build's exact cancel behavior "
            "is not verified."
        ),
    },
)


class DryRunCommandService:
    """Validates fixed macro parameters and records local-only previews."""

    execution_enabled = False

    def __init__(self) -> None:
        self.audit: deque[dict[str, Any]] = deque(maxlen=200)
        self.allowed_sd_files: set[str] = set()
        self.ambiguous_sd_files: set[str] = set()

    def update_sd_listing(self, entries: list[dict[str, Any]]) -> None:
        """Keep exact firmware names while excluding case-insensitive duplicates."""
        names = [str(row.get("name", "")) for row in entries if row.get("kind") == "file"]
        counts: dict[str, int] = {}
        for name in names:
            counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
        self.ambiguous_sd_files = {name.casefold() for name in names if counts[name.casefold()] > 1}
        self.allowed_sd_files = {name for name in names if counts[name.casefold()] == 1}

    def is_sd_file_ambiguous(self, filename: str) -> bool:
        return filename.casefold() in self.ambiguous_sd_files

    def list_macros(self) -> list[dict[str, Any]]:
        return [macro.as_dict() for macro in MACROS]

    def list_calibrations(self) -> list[dict[str, Any]]:
        return [workflow.as_dict() for workflow in CALIBRATIONS]

    def preview(
        self,
        macro_id: str,
        parameters: dict[str, Any] | None = None,
        *,
        retreat_from_home: bool = False,
    ) -> dict[str, Any]:
        macro = next((item for item in MACROS if item.id == macro_id), None)
        if macro is None:
            raise PreviewError("Unknown macro. Choose an item from the fixed macro library.")
        values = parameters or {}
        allowed = {parameter.name for parameter in macro.parameters}
        extra = set(values) - allowed
        missing = allowed - set(values)
        if extra:
            raise PreviewError("Unexpected parameter(s): " + ", ".join(sorted(extra)))
        if missing:
            raise PreviewError("Missing parameter(s): " + ", ".join(sorted(missing)))
        normalized: dict[str, int | float | str] = {}
        for parameter in macro.parameters:
            value = values[parameter.name]
            if parameter.kind == "number":
                if isinstance(value, bool) or not isinstance(value, (float, int)):
                    raise PreviewError(f"{parameter.label} must be a number.")
                number = float(value)
                if not math.isfinite(number):
                    raise PreviewError(f"{parameter.label} must be finite.")
                if parameter.minimum is not None and number < parameter.minimum:
                    raise PreviewError(f"{parameter.label} must be at least {parameter.minimum:g}.")
                if parameter.maximum is not None and number > parameter.maximum:
                    raise PreviewError(f"{parameter.label} must be at most {parameter.maximum:g}.")
                normalized[parameter.name] = int(number) if number.is_integer() else number
            elif parameter.kind == "file":
                if isinstance(value, str) and self.is_sd_file_ambiguous(value):
                    raise PreviewError(
                        "The current M20 listing contains duplicate names for this file; "
                        "selection is blocked because M23 would be ambiguous."
                    )
                if (
                    not isinstance(value, str)
                    or value not in self.allowed_sd_files
                    or not value
                    or any(
                        character
                        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/ "
                        for character in value
                    )
                    or ".." in value
                ):
                    raise PreviewError("Choose a listed SD-card file using a safe filename.")
                normalized[parameter.name] = value
            else:
                if not isinstance(value, str) or value not in parameter.options:
                    raise PreviewError(f"{parameter.label} is not an allowed option.")
                normalized[parameter.name] = value
        if macro_id == "neptune_3_axis_orbital_dance":
            commands = orbital_dance_commands(
                float(normalized["amplitude_percent"]), float(normalized["feed_mm_min"])
            )
        else:
            commands = [template.format(**normalized) for template in macro.templates]
        if retreat_from_home:
            retreat_sequences = {
                "home_x": ["G91", "G1 X30 F600", "G90", "G28 X"],
                "home_y": ["G91", "G1 Y30 F600", "G90", "G28 Y"],
                "home_z": ["G91", "G1 Z30 F180", "G90", "G28 Z"],
                "home_axes": [
                    "G91",
                    "G1 X30 F600",
                    "G1 Y30 F600",
                    "G1 Z30 F180",
                    "G90",
                    "G28",
                ],
            }
            commands = retreat_sequences.get(macro_id, [])
            if not commands:
                raise PreviewError("Visible retreat is available only with a fixed homing action.")
        entry = {
            "created_at": datetime.now(UTC).isoformat(),
            "macro_id": macro.id,
            "result": "preview_only",
            "command_count": len(commands),
            "retreat_from_home": retreat_from_home,
        }
        self.audit.append(entry)
        return {
            **entry,
            "name": macro.name,
            "commands": commands,
            "motion_classification": "planned_gcode_preview_only"
            if macro_id == "neptune_3_axis_orbital_dance"
            else None,
            "execution_block_reason": macro.execution_block_reason,
            "retreat_from_home": retreat_from_home,
            "workflow": list(macro.workflow),
            "requirements": list(macro.requirements),
            "risk": macro.risk,
            "compatibility": macro.compatibility,
            "dry_run": True,
            "execution_enabled": False,
            "notice": "Preview only. Nothing was sent to the printer.",
        }

    def reject_execution(self, macro_id: str) -> dict[str, Any]:
        """Record a blocked attempt; this method has no transport or dispatch path."""
        entry = {
            "created_at": datetime.now(UTC).isoformat(),
            "macro_id": macro_id,
            "result": "blocked_execution_disabled",
            "command_count": 0,
        }
        self.audit.append(entry)
        return entry

    def record_event(self, macro_id: str, result: str, error: str | None = None) -> dict[str, Any]:
        """Record a command lifecycle event without storing operator parameters or tokens."""
        entry: dict[str, Any] = {
            "created_at": datetime.now(UTC).isoformat(),
            "macro_id": macro_id,
            "result": result,
            "command_count": 0,
        }
        if error:
            entry["error"] = error[:500]
        self.audit.append(entry)
        return entry

    def record_error(self, macro_id: str, message: str) -> dict[str, Any]:
        """Record a rejected preview input without storing parameter contents."""
        entry = {
            "created_at": datetime.now(UTC).isoformat(),
            "macro_id": macro_id,
            "result": "preview_rejected",
            "command_count": 0,
            "error": message,
        }
        self.audit.append(entry)
        return entry
