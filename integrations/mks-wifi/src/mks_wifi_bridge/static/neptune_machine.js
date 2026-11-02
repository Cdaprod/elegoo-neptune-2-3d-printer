/**
 * Published Neptune 2 print envelope and bed-slinger transform used by the
 * offline visualizer. Axis travel limits and the installed firmware's home
 * offsets are not exposed by the MKS telemetry channel, so those values stay
 * explicitly nominal instead of being presented as measured machine data.
 */
export const NEPTUNE_2_MACHINE = Object.freeze({
  id: "elegoo-neptune-2",
  name: "Elegoo Neptune 2",
  buildVolumeMm: Object.freeze({ x: 220, y: 220, z: 250 }),
  firmwareReference: Object.freeze({
    release: "Elegoo Robin Nano V1.2 V1.0.4 factory configuration",
    source: "https://github.com/NARUTOfzr/ZNP-Robin-Nano-V1.2-V1.3",
    runtimeVerified: false,
  }),
  nominalAxisLimitsMm: Object.freeze({
    x: Object.freeze({ min: 0, max: 235, homeDirection: "min", homeCoordinate: 0 }),
    y: Object.freeze({ min: 0, max: 235, homeDirection: "min", homeCoordinate: 0 }),
    z: Object.freeze({ min: 0, max: 260, homeDirection: "min", homeCoordinate: 0 }),
  }),
  initialHomeOffsetsMm: Object.freeze({ x: 0, y: 0, z: 0, e: 0 }),
  homeConfigurationConfidence: "factory_v1_0_4_package_uses_min_endstops_runtime_not_verified",
  travelLimitConfidence: "factory_v1_0_4_config_defaults_runtime_not_verified_or_eeprom_checked",
  kinematics: Object.freeze({
    x: "extruder carriage moves left/right on the X gantry",
    y: "build plate translates front/back; nozzle has no Y carriage motion",
    z: "X gantry and nozzle move vertically on the Z uprights",
  }),
});

export function printableBoundsStatus(point, machine = NEPTUNE_2_MACHINE) {
  return Object.fromEntries(
    Object.entries(machine.buildVolumeMm).map(([axis, limit]) => [
      axis,
      Number.isFinite(point?.[axis]) && point[axis] >= 0 && point[axis] <= limit,
    ]),
  );
}
