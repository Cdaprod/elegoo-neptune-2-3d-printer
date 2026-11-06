# Neptune Observatory feature status

Updated 2026-11-05. Routine controls use Operator Mode and do not depend on a
commissioning record. Every service start is disarmed. An MKS acknowledgment is
recorded separately from physical completion; no position or physical result is
invented.

| Area | Status | Evidence / remaining limitation |
| --- | --- | --- |
| Overview and temperatures | Working | Shared two-second telemetry poller, current/target cards, bounded history, freshness and connection diagnostics. |
| Print-job overview | Implemented | Active/paused/idle state, MKS-reported filename/progress/elapsed time, stale labeling, and recent server-recorded sessions are shown in one compact Overview panel. Stale/disconnected polls do not extend or close a session, and an unknown active-state field does not imply the print stopped. Historical sessions are polled and persisted by FastAPI; path reconstruction remains estimated when matching G-code is available. |
| Motion telemetry | Partial by protocol | M114 parser, read-only query path, and chart exist. The observed response contained only `ok`, so no firmware coordinates or measured movement trace are available. |
| Motion feedrate timeline | Working with estimates | Fixed-height time graph, zero baseline, rolling ranges, pan/zoom/scrub, compact event markers, backend print history, and offline G-code demo. Estimated/planned speed is distinct from measured motion. |
| Digital Twin simulation | Working offline | Shared G-code interpreter handles homing references, positioning modes, offsets, XYZ/E moves, and feedrate; Three.js model uses bed-slinger kinematics. G-code simulation does not report physical homing. |
| Digital Twin live/historical | Partial | MKS print progress can map a matching G-code path as an estimate. No usable live position feedback exists; historical replay requires matching G-code for path reconstruction. |
| Machine controls | Working software path | Fixed allowlisted commands for homing, bounded jogging, temperatures, fans, motors, extrusion, diagnostics, macros, and SD print lifecycle. Uses one serialized MKS lock, fresh status, bounded parameters, authentication, and no blind retry. No arbitrary G-code endpoint. |
| Neptune 3-Axis Orbital Dance | Guarded execution path implemented; behavior is firmware-dependent | Registered in the existing Macro Library with bounded XYZ choreography and exact command preview. Requested feed is configurable; controller acceleration, planner behavior, and transport pacing affect actual movement. Do not assume an acknowledgment proves physical motion. |
| Operator Mode | Working | Uses dashboard sign-in; arm requires attendance, reachable switch, disconnected competing clients, and fresh printer state. Remains armed until disarm or service restart, with no inactivity timer. |
| Command lifecycle | Working | Requested/validated/queued/running/acknowledged/failed/outcome-unknown states and durable command history. Acknowledgment is never labeled physical completion. Ambiguous motion invalidates coordinate estimates. |
| Homing and jogging | Available in Operator Mode | Home X has historical physical observation. Any acknowledged in-session G28 establishes a clearly labeled coordinate estimate used for bounded relative jogs; it is not measured position. Ambiguous motion or restart clears it. Software limits cannot replace physical observation of clearance. |
| Temperature controls | Available in Operator Mode | PLA/PETG presets, custom nozzle/bed targets, cooldown, live monitoring, and temperature bounds. Heat-up behavior over WiFi has not been physically demonstrated. |
| Extrusion and filament | Available with runtime temperature gate | Bounded load/unload/purge actions require fresh nozzle telemetry at or above the configured 180 °C minimum. No cold-extrusion override. Physical feed behavior is not verified in this session. |
| Fan, motors, endstops | Available with runtime checks | M106/M107, M17/M84, and M119 fixed templates are wired. Fan/stepper behavior and exact firmware response remain unverified over WiFi. Motor release requires gantry support confirmation. |
| SD card | Listing and print controls | M20 listing is read-only. Selection/start/pause/resume/cancel use fixed templates and state checks; exact lifecycle behavior over this printer's MKS channel remains unverified. |
| Wireless upload | Gated off | Makerbase source documents an upload route, but this printer's installed firmware and end-to-end file integrity are not validated. No upload is enabled by this change. |
| OrcaSlicer/calibration | Partial | Dashboard offers calibration workflows and offline G-code analysis. This app does not modify slicer profiles or claim to generate/start calibration files. |
| Diagnostics/console | Working | Cached API queries, firmware reference, operation history, and command responses. Console remains read-only and accepts named queries only. |
| Deployment | Healthy Compose service; MKS connection live | Authenticated `/health` returns 200, container health is healthy, and SQLite `integrity_check` passes. On 2026-11-05 fresh telemetry reported idle and a fresh read-only M20 returned 17 entries. |
| Local G-code source | Unavailable in current deployment | The optional Compose read-only source overlay exists, but no host export path is configured and the running container reports its source unavailable. The inspected OrcaSlicer executable is 2.5.0-rc; no export directory was found at the conventional volume/export locations checked. Therefore current M20 entries have no matched local thumbnail metadata. |
| Responsive UI | Responsive styles present; authenticated visual review pending | The deployed dashboard currently opens to its token sign-in screen in Chrome. No credential was entered for the screenshot pass, so authenticated layout and browser-console checks could not be completed in this run. |
| Future Klipper/Moonraker | Architecture documented only | Adapter boundary and official Moonraker API mappings are documented in `development.md`; no Klipper firmware, Moonraker service, or second runtime backend was installed. |

## Runtime states shown to the operator

- **Ready** means current server-side state and safety checks pass.
- **Disarmed** means arm Operator Mode to enable fixed actions.
- **Awaiting homing** means the current session has no usable coordinate reference
  for a bounded jog.
- **Temporarily unavailable** means connection, freshness, or printer state is
  unsuitable right now.
- **Unsupported / not implemented** is reserved for a real protocol or software
  limitation, not simply missing commissioning evidence.
- **Acknowledged** means the MKS returned a response. Physical completion is
  shown only when telemetry or a directly recorded operator observation supports it.
- **Outcome unknown** means a command may have been sent but its result was not
  received. It is never automatically retried.

## Preserved diagnostics

Optional commissioning and verification evidence remains in the local runtime
registry and command history. It can explain past behavior, but it does not
decide whether routine actions are available. Runtime arm state and coordinate
references are never restored from disk after restart.

See [`machine-control-backlog.md`](machine-control-backlog.md) for outstanding
software and hardware-dependent work. Specific operator records remain local
runtime data and are not distributed in the public repository.
