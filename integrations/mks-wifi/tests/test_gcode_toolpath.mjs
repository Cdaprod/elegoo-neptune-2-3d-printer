import test from "node:test";
import assert from "node:assert/strict";
import { NEPTUNE_2_MACHINE } from "../src/mks_wifi_bridge/static/neptune_machine.js";
import { parseToolpath } from "../src/mks_wifi_bridge/static/gcode_toolpath.js";

test("parses Orca-style absolute extrusion and estimates path time", () => {
  const result = parseToolpath([
    ";LAYER_CHANGE",
    "G90",
    "M82",
    "G1 X10 Y20 Z0.2 F1200",
    "G1 X20 E1 F600",
    "G0 X30 E0.5",
  ].join("\n"), "test.gcode");
  assert.equal(result.source, "local_orcaslicer_gcode");
  assert.equal(result.estimated_only, true);
  assert.equal(result.machine.nominal_axis_limits_mm.x.max, 235);
  assert.equal(result.machine.nominal_axis_limits_mm.y.max, 235);
  assert.equal(result.machine.nominal_axis_limits_mm.z.max, 260);
  assert.equal(result.segments.length, 3);
  assert.equal(result.segments[0].kind, "travel");
  assert.equal(result.segments[1].kind, "extrusion");
  assert.equal(result.segments[2].kind, "retract");
  assert.ok(result.estimated_seconds > 0);
  assert.deepEqual(result.segments[0].to, { x: 10, y: 20, z: 0.2 });
});

test("respects relative XYZ/E, inch units, and G92 without a physical move", () => {
  const result = parseToolpath([
    "G20", "G91", "M83", "G1 X1 Y2 Z0.01 E0.1 F60", "G92 X0 Y0 E0", "G1 X1 E0.2",
  ].join("\n"));
  assert.equal(result.segments.length, 2);
  assert.equal(result.segments[0].to.x, 25.4);
  assert.equal(result.segments[0].to.y, 50.8);
  assert.equal(result.segments[0].kind, "extrusion");
  assert.equal(result.segments[1].from.x, 25.4);
  assert.equal(result.segments[1].to.x, 50.8);
  assert.equal(result.segments[1].extrusion_delta_mm, 5.08);
  assert.deepEqual(result.events[0].values, { x: 0, y: 0, e: 0 });
  assert.equal(result.events[0].machine_position.x, 25.4);
});

test("ignores non-motion commands and preserves unavailable toolpath as empty", () => {
  const result = parseToolpath("M104 S200\nM140 S60\n; no motion\nM117 hello");
  assert.equal(result.segment_count, 0);
  assert.deepEqual(result.layers, []);
});

test("G28 establishes a simulated home coordinate without inventing travel", () => {
  const result = parseToolpath("G1 X50 Y20 Z10 F600\nG28 X Y\nG1 X5 Y7 Z10 F600");
  assert.equal(result.segment_count, 2);
  assert.deepEqual(result.segments[1].from, { x: 0, y: 0, z: 10 });
  assert.deepEqual(result.segments[1].to, { x: 5, y: 7, z: 10 });
  assert.deepEqual(result.events[0].axes, ["x", "y"]);
  assert.deepEqual(result.events[0].homed_axes, ["x", "y"]);
  assert.equal(result.events[0].physical_homing_confirmed, false);
  assert.equal(result.segments[1].reference_status, "unreferenced_planned_coordinates");
  assert.equal(result.physical_homing_confirmed, false);
});

test("G28 without axis flags homes all simulated axes and marks following coordinates referenced", () => {
  const result = parseToolpath("G28\nG1 X10 Y20 Z0.2 E1 F600");
  assert.deepEqual(result.events[0].axes, ["x", "y", "z"]);
  assert.deepEqual(result.simulated_homed_axes, ["x", "y", "z"]);
  assert.equal(result.segments[0].reference_status, "simulated_homed");
  assert.deepEqual(result.segments[0].to, { x: 10, y: 20, z: 0.2 });
  assert.equal(result.physical_homing_confirmed, false);
});

test("simulated homing applies a configured virtual home offset", () => {
  const machine = {
    ...NEPTUNE_2_MACHINE,
    initialHomeOffsetsMm: { x: 4, y: -2, z: 0.5, e: 0 },
  };
  const result = parseToolpath("G28 X\nG1 X5 Y3 Z1 F600", "offset.gcode", machine);
  assert.deepEqual(result.segments[0].from, { x: 4, y: 0, z: 0 });
  assert.deepEqual(result.segments[0].to, { x: 9, y: 1, z: 1.5 });
  assert.deepEqual(result.events[0].homed_axes, ["x"]);
});

test("G90/G91 and M82/M83 are tracked independently after G92 offsets", () => {
  const result = parseToolpath([
    "G28", "G92 X20 Y10 Z2 E0", "G90", "M83", "G1 X25 Y10 Z2 E1 F600",
    "G91", "M82", "G1 X5 E2 F600",
  ].join("\n"));
  assert.deepEqual(result.segments[0].from, { x: 0, y: 0, z: 0 });
  assert.deepEqual(result.segments[0].to, { x: 5, y: 0, z: 0 });
  assert.equal(result.segments[0].extrusion_delta_mm, 1);
  assert.deepEqual(result.segments[1].to, { x: 10, y: 0, z: 0 });
  assert.equal(result.segments[1].extrusion_delta_mm, 1);
});

test("parses slicer feature metadata and flags points outside the printable envelope", () => {
  const result = parseToolpath([
    "; filament_diameter = 1.75", "; layer_height = 0.2", "; outer_wall_line_width = 0.44",
    "G28", ";TYPE:Outer wall", "G1 X240 Y0 Z0.2 E4 F1200",
  ].join("\n"));
  assert.equal(result.header.filament_diameter_mm, 1.75);
  assert.equal(result.header.line_width_mm, 0.44);
  assert.equal(result.segments[0].feature, "Outer wall");
  assert.equal(result.segments[0].outside_printable_envelope, true);
  assert.ok(result.warnings.length >= 0);
});

test("rejects non-text and over-limit files", () => {
  assert.throws(() => parseToolpath(null), /must be text/);
  assert.throws(() => parseToolpath("x".repeat(50 * 1024 * 1024 + 1)), /50 MB/);
});

test("bounds parsed moves and warns when a local replay is truncated", () => {
  const text = `G91\n${Array.from({ length: 80_001 }, () => "G1 X0.1 F600").join("\n")}`;
  const result = parseToolpath(text, "large-local-fixture.gcode");
  assert.equal(result.segment_count, 80_000);
  assert.ok(result.warnings.some((warning) => warning.includes("80,000 segment safety limit")));
});
