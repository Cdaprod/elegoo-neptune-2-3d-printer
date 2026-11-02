# Neptune Observatory machine controls

## Normal operation

The Machine page is the normal control surface. Sign in with the dashboard token,
then arm Operator Mode once while at the printer. Arming requires a fresh
connected state plus confirmation that you are present, can reach the power
switch, and have disconnected other MKS clients. Operator Mode stays armed until
you disarm it or the service restarts; it has no inactivity timer and requires
no separate credential.

Choose a fixed action card, set its parameters if needed, review the exact
command preview, and confirm once. Current telemetry, machine state, parameter
bounds, operation conflicts, and action-specific safety conditions are checked
on the server. The result, command reply, and error appear on the same action
card. There is no required post-action observation form and no per-operation
commissioning gate. Optional commissioning evidence is retained for diagnostics
only.

The executor uses fixed validated command templates through the shared
serialized MKS transport. It has bounded timeouts, records each dispatch and
reply, and never automatically retries a command after an ambiguous result. A
successful MKS acknowledgment is not reported as physical completion. State
labels distinguish acknowledged, failed, outcome unknown, and operator-observed
results.

## Neptune 3-Axis Orbital Dance

The Macro Library includes **Neptune 3-Axis Orbital Dance**. Its existing macro
card opens the normal parameter dialog and exact-command preview. Orbit size is
limited to 3–10% of the nominal XY build envelope (default 7%); commanded
feedrate is limited to 3000–9000 mm/min (50–150 mm/s), defaulting to 6000 mm/min
(100 mm/s). The initial Z lift requests 10 mm/s. Firmware maximum feedrate and
acceleration remain authoritative and may reduce these requested speeds; the
dashboard has no measured motor-speed signal. The generated path uses a two-turn
helix, figure eight, and phase-shifted XYZ wave, followed by an elevated-center
park and final `G28`. It is 120 commands at the default settings. Every XYZ
point is checked against the Observatory nominal 220 × 220 × 250 mm printable
volume before the preview is returned. It emits no E movement, heater, or print
commands. `G90` establishes absolute positioning after the initial home.

The choreography uses an initial `M400` compatibility preflight, then `M400`
barriers after homing and each motion phase. Marlin documents `M400` as waiting
until planner moves finish. `M400` returned `ok` in the user's run, but that
acknowledgment does not prove the planner barrier completed on this exact
Elegoo build. A rejected preflight is first and sends no movement. A later
error/cancel stops the sequence before its final `G28`; no automatic home is
injected. The existing single-client MKS connection sends one line at a time and
waits for each response. The path uses the nominal printable envelope rather
than live EEPROM travel settings, which are not exposed by MKS telemetry. The
run timed out on its 13th movement and is recorded as outcome unknown in the
commissioning log. No physical command was sent by the software change or its
tests; mocked execution tests do not establish real motion completion.

## Motion and coordinate confidence

`G28` operations are available in Operator Mode and do not depend on M114. After
a G28 sequence is acknowledged, the service establishes a **command-acknowledged
coordinate estimate** at the origin for bounded relative jogging during that
service run. The dashboard labels this estimate as such; it is not firmware
position feedback or independent proof that a switch was reached. An ambiguous
motion result or service restart clears the reference. Jogging is blocked until
a new in-session home reference exists, and software travel bounds do not
replace inspecting the physical travel path.

Home X/Y/Z and Home All normally send their standard fixed `G28` command. The
Home dialog also offers a visible 30 mm retreat when the operator confirms that
the relevant axis is already resting at its home switch. That option previews
and sends the fixed relative move followed by G28; it is not required for normal
homing and its retreat sequence has mock-only verification.

M114 returned only `ok` in the prior live query, without coordinates. Position
cards and measured-position charts remain empty until a parseable firmware
sample is available. Planned G-code and command-derived coordinates are shown as
estimates, never measured telemetry.

## Supported dashboard groups

- **Control:** Home X/Y/Z/All; bounded X/Y/Z jog; hotend and bed targets; PLA and
  PETG presets; cooldown; fan output/off; stepper enable/release; endstop report;
  bounded extrusion/retraction; SD print actions.
- **Macro Library:** fixed purge, nozzle preparation, load/unload and guided
  filament workflows. Extrusion requires fresh nozzle telemetry at or above the
  configured minimum (180 °C by default); the cold-extrusion protection is not
  bypassed.
- **Calibration:** practical OrcaSlicer preparation guidance. The dashboard does
  not claim to create calibration G-code or start those prints automatically.
- **Diagnostics:** firmware capability notes, transport responses, command
  history, and read-only telemetry details.

All operations still require an armed session and must satisfy live runtime
checks. Heating, moving, extrusion, motor release, and print control do not run
automatically on startup, page load, deployment, or test execution.

## Confirmed and unconfirmed protocol behavior

- **Home X:** a prior operator observed the carriage reach its reference switch and stop normally. This does not establish current homed state after restart or external motion.
  as an acknowledged `G28 X` with the operator observing the X carriage reach
  its endstop and stop normally.
- **Cooldown:** `M104 S0` and `M140 S0` received acknowledgments while both
  targets were already zero; a target transition was not demonstrated.
- **M114:** one read-only query returned only `ok`; no usable coordinates.
- **Other command families:** fixed controls are implemented and can be used
  directly in Operator Mode, but their physical behavior through this installed
  WiFi bridge has not been confirmed by this record. A real printer response
  or error is shown when the operator uses them.
- **SD upload:** remains separately disabled. Makerbase source documents an
  upload handler, but this printer's firmware endpoint and transfer integrity
  have not been confirmed.

Generic Marlin documentation describes command behavior but is not proof of
this installed Elegoo firmware's exact behavior. The full software and
hardware-dependent status is in [`feature-status.md`](feature-status.md).
