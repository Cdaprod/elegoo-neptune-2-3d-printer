import { NEPTUNE_2_MACHINE, printableBoundsStatus } from "./neptune_machine.js";

const MAX_GCODE_BYTES = 50 * 1024 * 1024;
const MAX_SEGMENTS = 80_000;
const NUMBER = "[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[Ee][-+]?\\d+)?";
const WORD = new RegExp(`([A-Z])\\s*(${NUMBER})`, "g");
const EPSILON = 1e-8;

function numberWord(words, letter) {
  const value = words.get(letter);
  return value === undefined || !Number.isFinite(value) ? null : value;
}

function parseWords(code) {
  const words = new Map();
  for (const match of code.matchAll(WORD)) words.set(match[1], Number(match[2]));
  return words;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function clonePoint(point) {
  return { x: point.X, y: point.Y, z: point.Z };
}

function commandFrom(code) {
  return code.match(/^(?:N\d+\s*)?([GMT]\d+(?:\.\d+)?)/)?.[1] || null;
}

function g28Axes(code) {
  const suffix = code.replace(/^(?:N\d+\s*)?G28\b/i, "");
  const requested = [...suffix.matchAll(/(?:^|\s)([XYZ])(?=\s|[-+]?\d|$)/g)]
    .map((match) => match[1].toLowerCase());
  return requested.length ? [...new Set(requested)] : ["x", "y", "z"];
}

function extractHeaderValue(comment, name) {
  const match = comment.match(new RegExp(`(?:^|\\b)${name}\\s*=\\s*(${NUMBER})`, "i"));
  return match ? Number(match[1]) : null;
}

function featureFromComment(comment, previous) {
  const match = comment.match(/(?:^|\b)TYPE\s*:\s*([^;]+)/i);
  return match ? match[1].trim() : previous;
}

/**
 * Decode the supported linear OrcaSlicer G-code subset into a local, planned
 * path. G28 creates a simulated switch reference only; it never asserts that
 * the physical printer has homed.
 */
export function parseToolpath(text, filename = "local.gcode", machine = NEPTUNE_2_MACHINE) {
  if (typeof text !== "string") throw new TypeError("G-code must be text.");
  if (new TextEncoder().encode(text).byteLength > MAX_GCODE_BYTES) {
    throw new RangeError("G-code file exceeds the 50 MB local preview limit.");
  }

  const logical = { X: 0, Y: 0, Z: 0, E: 0 };
  const machinePosition = { X: 0, Y: 0, Z: 0, E: 0 };
  const offsets = {
    X: machine.initialHomeOffsetsMm.x,
    Y: machine.initialHomeOffsetsMm.y,
    Z: machine.initialHomeOffsetsMm.z,
    E: machine.initialHomeOffsetsMm.e,
  };
  const homed = { X: false, Y: false, Z: false };
  const segments = [];
  const events = [];
  const warnings = new Set();
  const featureNames = new Set();
  let absoluteXYZ = true;
  let absoluteE = true;
  let unitScale = 1;
  let feedMmMin = 1200;
  let elapsedSeconds = 0;
  let currentLayer = 0;
  let lastLayerZ = 0;
  let layerHeightMm = 0.2;
  let filamentDiameterMm = 1.75;
  let lineWidthMm = 0.42;
  let feature = "unknown";
  let unsupportedMotionCount = 0;
  let moveCount = 0;
  let hitSegmentLimit = false;
  const lines = text.split(/\r?\n/);

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const raw = lines[lineIndex];
    const semicolon = raw.indexOf(";");
    const code = (semicolon < 0 ? raw : raw.slice(0, semicolon)).trim().toUpperCase();
    const comment = semicolon < 0 ? "" : raw.slice(semicolon + 1).trim();
    const layerMatch = comment.match(/(?:LAYER_CHANGE|LAYER)\s*[:= ]\s*(-?\d+)/i);
    if (layerMatch) currentLayer = Math.max(0, Number(layerMatch[1]));
    const zMatch = comment.match(new RegExp(`^Z\\s*:\\s*(${NUMBER})`, "i"));
    if (zMatch) lastLayerZ = Number(zMatch[1]);
    layerHeightMm = extractHeaderValue(comment, "layer_height") ?? layerHeightMm;
    filamentDiameterMm = extractHeaderValue(comment, "filament_diameter") ?? filamentDiameterMm;
    lineWidthMm = extractHeaderValue(comment, "(?:outer_wall_line_width|line_width)") ?? lineWidthMm;
    feature = featureFromComment(comment, feature);
    if (feature !== "unknown") featureNames.add(feature);
    if (!code) continue;

    const command = commandFrom(code);
    const words = parseWords(code);
    if (command === "G20") { unitScale = 25.4; continue; }
    if (command === "G21") { unitScale = 1; continue; }
    if (command === "G90") { absoluteXYZ = true; continue; }
    if (command === "G91") { absoluteXYZ = false; continue; }
    if (command === "M82") { absoluteE = true; continue; }
    if (command === "M83") { absoluteE = false; continue; }

    if (command === "G28") {
      const axes = g28Axes(code);
      const before = clonePoint(machinePosition);
      for (const axis of axes) {
        const key = axis.toUpperCase();
        const axisConfig = machine.nominalAxisLimitsMm[axis];
        const logicalHome = axisConfig.homeCoordinate;
        const homeOffset = machine.initialHomeOffsetsMm[axis] || 0;
        const machineHome = logicalHome + homeOffset;
        machinePosition[key] = machineHome;
        logical[key] = logicalHome;
        offsets[key] = machineHome - logical[key];
        homed[key] = true;
      }
      events.push({
        type: "simulated_home",
        command: "G28",
        line: lineIndex + 1,
        time_seconds: elapsedSeconds,
        axes,
        from: before,
        to: clonePoint(machinePosition),
        homed_axes: Object.keys(homed).filter((axis) => homed[axis]).map((axis) => axis.toLowerCase()),
        reference_kind: "simulated_endstop_reference",
        physical_homing_confirmed: false,
        confidence: machine.homeConfigurationConfidence,
      });
      continue;
    }

    if (command === "G92") {
      const reset = {};
      for (const axis of ["X", "Y", "Z", "E"]) {
        const value = numberWord(words, axis);
        if (value === null) continue;
        logical[axis] = value * unitScale;
        offsets[axis] = machinePosition[axis] - logical[axis];
        reset[axis.toLowerCase()] = logical[axis];
      }
      events.push({
        type: "coordinate_reset",
        command: "G92",
        line: lineIndex + 1,
        time_seconds: elapsedSeconds,
        values: reset,
        machine_position: clonePoint(machinePosition),
        offsets: { x: offsets.X, y: offsets.Y, z: offsets.Z, e: offsets.E },
      });
      continue;
    }

    if (command !== "G0" && command !== "G1") {
      if (command === "G2" || command === "G3") {
        unsupportedMotionCount += 1;
        warnings.add("G2/G3 arc motion is not included in this linear replay.");
      }
      continue;
    }

    moveCount += 1;
    const startMachine = { ...machinePosition };
    const startLogical = { ...logical };
    const previousE = logical.E;
    for (const axis of ["X", "Y", "Z", "E"]) {
      const value = numberWord(words, axis);
      if (value === null) continue;
      const isAbsolute = axis === "E" ? absoluteE : absoluteXYZ;
      const scaled = value * unitScale;
      if (isAbsolute) {
        logical[axis] = scaled;
        machinePosition[axis] = scaled + offsets[axis];
      } else {
        logical[axis] += scaled;
        machinePosition[axis] += scaled;
      }
    }

    const nextFeed = numberWord(words, "F");
    if (nextFeed !== null && nextFeed > 0) feedMmMin = nextFeed * unitScale;
    const dx = machinePosition.X - startMachine.X;
    const dy = machinePosition.Y - startMachine.Y;
    const dz = machinePosition.Z - startMachine.Z;
    const distance = Math.hypot(dx, dy, dz);
    const extrusionDelta = logical.E - previousE;
    if (machinePosition.Z > lastLayerZ + 0.01) {
      currentLayer = Math.max(currentLayer + 1, currentLayer);
      lastLayerZ = machinePosition.Z;
      layerHeightMm = Math.max(0.05, Math.min(1.2, machinePosition.Z - startMachine.Z || layerHeightMm));
    }
    if (distance < EPSILON && Math.abs(extrusionDelta) < EPSILON) continue;

    const kind = distance < EPSILON
      ? (extrusionDelta > EPSILON ? "stationary_extrusion" : "stationary_retract")
      : (extrusionDelta > EPSILON ? "extrusion" : extrusionDelta < -EPSILON ? "retract" : "travel");
    const timedDistance = distance > EPSILON ? distance : Math.abs(extrusionDelta);
    const duration = timedDistance / Math.max(feedMmMin / 60, 0.1);
    const printableBounds = printableBoundsStatus(clonePoint(machinePosition), machine);
    const segment = {
      kind,
      feature,
      layer: currentLayer,
      line: lineIndex + 1,
      from: clonePoint(startMachine),
      to: clonePoint(machinePosition),
      logical_from: clonePoint(startLogical),
      logical_to: clonePoint(logical),
      homed_axes: Object.keys(homed).filter((axis) => homed[axis]).map((axis) => axis.toLowerCase()),
      reference_status: Object.values(homed).every(Boolean) ? "simulated_homed" : "unreferenced_planned_coordinates",
      feed_mm_min: feedMmMin,
      extrusion_delta_mm: extrusionDelta,
      extrusion_volume_mm3: Math.max(0, extrusionDelta) * Math.PI * (filamentDiameterMm / 2) ** 2,
      filament_diameter_mm: filamentDiameterMm,
      bead_height_mm: layerHeightMm,
      bead_width_mm: clamp(lineWidthMm, 0.2, 2.0),
      start_seconds: elapsedSeconds,
      end_seconds: elapsedSeconds + duration,
      distance_mm: distance,
      printable_bounds: printableBounds,
      outside_printable_envelope: Object.values(printableBounds).some((inside) => !inside),
    };
    segments.push(segment);
    elapsedSeconds += duration;
    if (segments.length >= MAX_SEGMENTS) { hitSegmentLimit = true; break; }
  }

  if (unsupportedMotionCount) warnings.add(`${unsupportedMotionCount} arc move(s) were omitted.`);
  if (hitSegmentLimit) warnings.add(`Replay stopped at the ${MAX_SEGMENTS.toLocaleString()} segment safety limit.`);
  const layers = [...new Set(segments.map((segment) => segment.layer))].sort((a, b) => a - b);
  const homingEvents = events.filter((event) => event.type === "simulated_home");
  return {
    filename,
    source: "local_orcaslicer_gcode",
    machine: {
      id: machine.id,
      name: machine.name,
      build_volume_mm: machine.buildVolumeMm,
      nominal_axis_limits_mm: machine.nominalAxisLimitsMm,
      travel_limit_confidence: machine.travelLimitConfidence,
      home_configuration_confidence: machine.homeConfigurationConfidence,
      kinematics: machine.kinematics,
    },
    segment_count: segments.length,
    move_count: moveCount,
    line_count: lines.length,
    estimated_seconds: elapsedSeconds,
    estimated_only: true,
    estimated_time_basis: "linear distance and G0/G1 feed rate; no acceleration or planner model",
    homing_simulated: homingEvents.length > 0,
    simulated_homed_axes: homingEvents.at(-1)?.homed_axes || [],
    physical_homing_confirmed: false,
    offsets_mm: { x: offsets.X, y: offsets.Y, z: offsets.Z, e: offsets.E },
    header: { layer_height_mm: layerHeightMm, filament_diameter_mm: filamentDiameterMm, line_width_mm: lineWidthMm },
    layers,
    features: [...featureNames],
    warnings: [...warnings],
    events,
    home_events: homingEvents,
    segments,
  };
}
