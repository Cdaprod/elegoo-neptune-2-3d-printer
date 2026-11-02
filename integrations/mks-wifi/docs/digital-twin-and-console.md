# Digital Twin, Motion Preview, and Console

## Machine reference and confidence

The reusable Three.js renderer models a Cartesian Neptune 2 bed-slinger: X
moves the extruder carriage, Y moves the build plate under the stationary
nozzle, and Z raises the X gantry. The published 220 × 220 × 250 mm build
volume defines the displayed printable envelope.

That envelope comes from the [Elegoo Neptune 2 user guide](https://elegoo-downloads.oss-us-west-1.aliyuncs.com/download.elegoo.com/06%20FDM%20Printer/02%20ELEGOO%20Neptune%20Series%20Files/Neptune%202-2S-2D/4.%20User%27s%20Guide/1.User%20Manual/Neptune%202%20%26%20Neptune%202S%20User%20Guide%20%28EN%29.pdf). Generic [Marlin homing documentation](https://marlinfw.org/docs/features/homing.html) describes switch-based homing, but does not verify this printer's compiled firmware settings.

Elegoo's [firmware notes](https://download.elegoo.com/06%20FDM%20Printer/02%20ELEGOO%20Neptune%20Series%20Files/Neptune%202-2S-2D/2.%20Firmware/1.%20Firmware%20update%20instructions%20%28required%20reading%29/Neptune2%262D%262S%20Firmware%20Update%20Notes.pdf) list separate V1.2 releases, and the [maintained firmware repository](https://github.com/NARUTOfzr/ZNP-Robin-Nano-V1.2-V1.3) contains the board family. The current MKS identifier `TFT28/32` is not a firmware release number, so those package defaults cannot establish the settings active on this printer.

The factory Robin Nano V1.2 V1.0.4 [`elegoo.txt`](https://raw.githubusercontent.com/NARUTOfzr/ZNP-Robin-Nano-V1.2-V1.3/master/Configuration%20files-V1.0.4/Neptune%202/elegoo.txt) sets X/Y/Z minima to 0, maxima to 235/235/260 mm, and homes each axis toward its minimum switch; its default order is X then Y. The renderer shows those package defaults as an amber travel envelope and the 220 × 220 × 250 mm printable envelope in blue. The dashboard's `TFT28/32` identifier does not prove that V1.0.4 or its values are active: installed firmware version and EEPROM overrides remain unverified. The simulator uses those published defaults as a labelled nominal reference and zero initial offsets. G-code `G92` offsets are tracked within the parsed program. The frame and switch markers are schematic, not dimension-certified measurements.

`G28` in a file creates a **simulated** coordinate reference for the selected
axes (all XYZ when no axis is named). It adds no invented travel segment and
never establishes that the physical machine homed. Motion following a parsed
home is shown as planned file motion in that virtual coordinate system. Axis
switches are reference points, not continuous position feedback.

## Offline OrcaSlicer simulation

In Digital Twin → Simulation, choose an OrcaSlicer-exported `.gcode` file from
the current device. It is parsed locally for 3D rendering and submitted to the
authenticated Observatory API for analysis-only indexing. Parsed motion data
and a SHA-256 reference are stored in the server database; file content is
never sent to the printer. The parser
handles linear `G0`/`G1`, `G28`, absolute/relative XYZ (`G90`/`G91`), `G92`
coordinate offsets, absolute/relative extrusion (`M82`/`M83`), and units
(`G20`/`G21`). It uses OrcaSlicer feature comments and filament/line-width
metadata when present to render batched, cross-section filament beads. Outer
perimeters, infill, support, and future paths have separate colors. Toolpath
geometry is progressively revealed during playback; the X carriage, Z gantry,
and Y-moving bed follow the planned path.

Replay time is an estimate based on straight-line distance and feed rate. It
does not model acceleration, firmware planner look-ahead, homing travel
sequence, or unsupported arc commands (`G2`/`G3`); omitted arcs are called out.
Parsing is capped at 80,000 moves to bound browser memory; a longer file shows
a clear truncation warning. Coordinates outside the published printable envelope are flagged but not
silently clipped. The Three.js renderer is lazy-loaded and stops drawing while
offscreen or while the browser tab is hidden.

## Live progress synchronization

The existing MKS poll already reads `M27` progress, `M994` current filename,
`M997` printer state, and `M992` elapsed time. Live Sync follows a local G-code
path only when telemetry is fresh, the printer reports an active/paused job,
progress is in range, and the selected local basename matches the reported
file. The MKS percentage is mapped to the G-code's estimated timeline. The UI
labels the resulting position as a **progress-based estimate**, not an exact
executing line or a firmware-reported carriage position. Temperatures and
printer state continue to come from the shared cached MKS polling worker.

This synchronization uses no M114 request and creates no additional printer
connection or polling worker. If progress, filename, state, freshness, or file
matching is missing, the toolhead location remains unavailable. M114 remains
disabled after the last one-shot query returned only `ok` and no coordinates.

## Historical Replay

Historical capture is performed by the FastAPI service's existing shared
poller, independent of open browser tabs. It stores bounded timestamps, the
MKS-reported filename, print state, progress percent, elapsed time, temperatures,
poll latency, and operation audit events in SQLite on the persistent `/data`
volume. Retention is 30 days, 100,000 telemetry samples, 100 operation records,
8 analyzed G-code files, and 200,000 motion segments. G-code content is not
stored, and no credential or CAD data is included.

To replay, select a server-recorded session and choose the matching G-code in
Simulation. Recorded MKS progress is interpolated by timestamp and mapped onto
the estimated G-code timeline. Play/pause, reset, playback speed, and timeline
scrubbing use those recorded samples. This cannot reconstruct an exact firmware
line or physical position. Without matching G-code, the recorded session
remains available as telemetry history but no toolpath is rendered.

## Existing SD-card and OrcaSlicer workflows

The SD Card view remains a read-only root listing through the existing M20
request. It does not download file contents or modify the card. For simulation
or live matching, select the corresponding OrcaSlicer export from the current
device. OrcaSlicer profile files and printer settings are not modified.

## Read-only console

The Home preview and Console page provide a fixed query allowlist for cached
status, temperature, motion, firmware, diagnostics, health, and command audit
data. This is a safe query prompt, not a shell, code REPL, arbitrary API client,
or G-code console. It cannot issue M114, change settings, or control the
printer.

## Validation boundaries

Automated fixtures cover G28 axis selection, G92 coordinate offsets without
movement, G90/G91, M82/M83, units, feature comments, envelope warnings, and
bounded local history sample handling. No real OrcaSlicer export was inspected
or sliced for this work, and no CAD models were accessed. No physical homing,
movement, heating, extrusion, G-code transmission, or print operation was
performed. The MKS M27 progress value is available through the existing parser;
live path alignment remains a percentage-based estimate, not an exact
position/progress synchronization signal.
