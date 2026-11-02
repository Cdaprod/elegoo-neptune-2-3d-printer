# Neptune Observatory completion inventory

Updated 2026-11-02. The normal product flow is Operator Mode; commissioning
history is diagnostic evidence, not a prerequisite ladder. No CAD, firmware,
OrcaSlicer profile, or global Codex configuration was accessed or changed.

## Priority backlog

| Priority | Work | Software status | Remaining acceptance / evidence |
| --- | --- | --- | --- |
| P0 | Preserve Home X evidence | Done | `G28 X` was physically observed to home the X carriage. Historical operator records stay in local runtime data; this repository records only the feature-level result. |
| P0 | Remove commissioning from normal operation | Done in code | Action cards open the command review/confirm interaction; results appear on the same card. Normal operations no longer depend on per-macro registry state or post-action observation forms. |
| P0 | Simplify authorization | Done in code | Existing dashboard sign-in, one session-level attendance/power/client attestation, per-action confirmation, fresh telemetry and disarmed-on-restart. Session remains armed until explicit disarm or restart. |
| P0 | Direct guarded command path | Implemented, mocks pass | Typed macros use the shared serialized MKS path, server checks, bounded timeout, audit history, and no retries after ambiguous dispatch. No physical commands were sent for this change. |
| P1 | Axis references and jog | Implemented as an estimate | G28 acknowledgment establishes an estimated origin for in-session bounded jogging; never displayed as measured position. Unknown/ambiguous outcomes clear the estimate. Actual clearances remain the operator's responsibility. |
| P1 | Temperature and extrusion | Implemented, not physically re-tested | Setpoints, presets, cooldown, live samples, and a 180 °C minimum extrusion interlock exist. Heat response and extrusion still need ordinary supervised use if the operator wants hardware evidence. |
| P1 | Fans, motors, endstops | Implemented, not physically re-tested | Fixed command controls are wired; real fan behavior, stepper release behavior, and M119 responses remain device-specific. |
| P1 | SD print lifecycle | Implemented, not physically re-tested | Current M20 listing, selected filename checks, print state gates, fixed start/pause/resume/cancel operations. Installed MKS firmware behavior still needs actual use before claiming verified. |
| P1 | Mobile/Desktop visual check | Pending after current edits | Inspect the rebuilt app at desktop, iPhone portrait, and landscape; confirm no overflow, modal clipping, or broken in-card status. |
| P1 | Compose deployment | Pending after current edits | Rebuild and health-check the service. Startup remains disarmed. Deployment must not run a machine-control action. |
| P2 | M114 coordinates | Protocol limitation unresolved | Existing read-only query returned only `ok`; keep positions unavailable until a usable report is received. M114 is not required for ordinary homing or bounded jogs after an in-session home reference. |
| P2 | SD upload | Gated off | Validate installed firmware endpoint, transfer semantics, and end-to-end file integrity before enabling wireless writes. |
| P2 | Live Twin synchronization | Partial | M27 progress and matching G-code provide a planned-path estimate, not live position. No actual execution-line or position source is available yet. |
| P2 | OrcaSlicer integration | Partial | Offline toolpath inspection and calibration guidance work; automatic profile mutation and full slicer management are outside the current bridge. |

## Known physical evidence

- `G28 X` was acknowledged and the operator observed the X carriage reach its
  reference switch and stop normally. This is a historical operator-observed result, not continuous position feedback.
- A previous 0.1 mm X jog was inconclusive because it was too small to see; it
  remains inconclusive, not a failure.
- The M114 sample contained `ok` without coordinates. No continuous measured
  motion line can be drawn from it.
- Cooldown commands were acknowledged while both targets were already zero;
  this does not demonstrate a target transition.

## Software validation expectations

All printer transport tests use mocks. Normal dashboard actions must remain
bounded, authenticated, serialized, state-aware, and non-retrying on ambiguous
results. No automated test or deployment step may move axes, heat, extrude,
release motors, or start/stop a print.
