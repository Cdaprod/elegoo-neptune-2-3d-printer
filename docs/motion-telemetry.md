# Motion telemetry

## Axis feedrate and activity timeline

The Motion and Overview charts use the same backend timeline. G-code feedrate is
projected onto each participating X/Y/Z/E axis by its share of the planned path
distance and labeled as planned or estimated. Idle zeroes are shown only when
fresh MKS state reports idle (or when a G-code dwell / motion-profile endpoint
explicitly provides a zero). Missing axis values remain gaps. No curve is motor
speed feedback: this firmware does not currently report stepper velocity.

Machine-state changes detected from the shared poll are stored in SQLite for up
to 30 days (bounded to 2,000 events). A nozzle or bed target change that matches
a recent Dashboard command is labeled as a correlated firmware-state
observation; an unmatched target change is labeled as an external state change
with unknown source. Print-state and current-file transitions are recorded from
MKS telemetry. These records do not reveal which TFT button or G-code caused a
change.

The TFT actions shown in the supplied photos (target temperatures and the
extrusion screen) are therefore only partially visible: a changed target may be
detected on the next M105 poll, but no TFT command transcript is available.
The current response has no position, extruder coordinate, fan state, or
stepper-activity field. The prior WiFi M114 query returned `ok` without axis
coordinates. As a result, the Z+10 mm movement and extrusion/retraction cannot
be reconstructed or added to the timeline from this connection. The dashboard
keeps them unavailable rather than turning the operator's example into a false
measurement.

The embedded Digital Twin and full view share the same Three.js machine and
camera code. Orbit/zoom focuses on the current rendered nozzle position and
smoothly follows simulated/live-estimated toolhead changes. Manual camera input
temporarily suspends following so touch or mouse orbit remains usable.

Current dashboard implementation details, shared G-code timeline behavior,
server-side SQLite recording, API endpoints, retention limits, and automated
verification are maintained in
[`integrations/mks-wifi/docs/motion-telemetry.md`](../integrations/mks-wifi/docs/motion-telemetry.md).

## Capability findings

The Makerbase MKS-WIFI source accepts line-based TCP G-code and forwards
unhandled commands to the controller UART, then relays controller responses to
the TCP client. The source does not establish that this Neptune 2 firmware
build implements `M114`; it only establishes the transport path for generic
commands. The module accepts one TCP client at a time, so a new client may
replace an existing host.

Marlin documents `M114` as “Get Current Position.” Its ordinary report is the
last planned firmware position and does not wait for queued moves to finish.
Real-time stepper position requires optional `M114_REALTIME` support; this
integration does not request that option. Marlin documentation is not evidence
that the installed Elegoo firmware supports `M114`, or that the TFT28/32 MKS
module relays a usable reply from this particular printer.

A single authenticated, explicitly authorized live `M114` query was sent to the
printer on 2026-11-01. The MKS TCP transport returned the line `ok` in 23.21 ms,
but no coordinate report followed. The result is therefore recorded as
**transport acknowledged; position reporting unavailable/unverified**. X/Y/Z/E
remain unavailable (null); they are not zero. The one-shot gate was consumed
before transport dispatch, periodic motion polling remained off, and the saved
configuration was returned to disabled. `TFT28/32` remains the MKS WiFi/display
identifier, not a verified Marlin build version.

## Implementation and safeguards

- `GET /api/printer/motion` returns cached motion state and bounded history.
- `POST /api/printer/motion/query` provides one dashboard-triggered `M114`
  request when `NEPTUNE_MOTION_QUERY_ENABLED=true`. It requires authentication,
  the exact confirmation `M114`, fresh idle telemetry, no active operation, the
  shared serialized I/O lock, and a 30-second rate limit. It consumes the
  one-shot runtime gate immediately before its single transport attempt, including
  on timeout or an ambiguous response. It is disabled by default and does not
  enable periodic polling.
- `/api/printer/status` and `/ws/printer` include the same cached motion block.
- `NEPTUNE_MOTION_POLLING_ENABLED` defaults to `false`.
- When separately enabled, the existing telemetry worker runs one `M114`
  query no more often than `NEPTUNE_MOTION_POLL_INTERVAL_SECONDS` (default 10,
  minimum 5). It only queries after fresh printer telemetry reports idle and no
  active print. The query holds the same serialized `io_lock` as every other
  printer request. It does not create a second worker or concurrent connection.
- The M114 command is a fixed literal in a read-only query path. It is not in
  the machine-control command or macro allowlist.
- A missing, malformed, unsupported, or timed-out report leaves affected axes
  unavailable; it never substitutes zero.
- Coordinate samples are explicitly identified as planned positions reported
  by firmware, never as measured physical positions. Query latency, poll time,
  freshness, source command, and printer state accompany the sample.
- Velocity and activity intervals require complete, consecutive samples while
  idle. Intervals are suppressed after a skipped/non-idle poll, missing axis,
  suspected re-zero, or implausibly fast coordinate jump. Estimates are
  approximate coordinate deltas. `M114` alone cannot reveal every origin change
  such as an external `G92`; the dashboard states this limitation.
- History is bounded in memory (up to one hour at current poll rates). No
  database or simulated live data is used.

## One-shot query procedure and result

The dashboard remains safe with the capability disabled. Before changing the
local `.env`, obtain explicit authorization for a read-only `M114` query and
confirm the printer is idle, no print is active, and competing printer clients
are disconnected. For exactly one query, set:

```dotenv
NEPTUNE_MOTION_QUERY_ENABLED=true
```

Rebuild/restart the service, open **Motion**, and use **Request one read-only
M114 sample**. The dashboard requires typing `M114`; the server revalidates
fresh idle state immediately before sending. It sends no homing, heating,
extrusion, or print command. If the response is empty, malformed, or
unsupported, the page records that result and does not retry automatically. The
server consumes the runtime gate before the attempt; set the persisted one-shot
opt-in back to `false` after the check. Periodic polling is a separate opt-in
and remains off.

The live attempt returned only `ok` and no coordinate line. This does not verify
that the controller implements `M114` position reporting. A supervised follow-up
should inspect the printer firmware/controller documentation or test another
read-only channel before considering any periodic position polling.

## Sources

- [Makerbase MKS-WIFI source](https://github.com/makerbase-mks/MKS-WIFI/blob/master/firmware_source/MksWifi/MksWifi.ino)
- [Marlin M114 documentation](https://marlinfw.org/docs/gcode/M114.html)
- [Marlin reporting options](https://marlinfw.org/docs/settings/reporting.html)
- [Marlin M154 position auto-report notes](https://marlinfw.org/docs/gcode/M154.html)
# Current dashboard implementation

The shared G-code interpreter, backend SQLite print recorder, API routes,
retention limits, source labels, and test coverage are documented in
[`integrations/mks-wifi/docs/motion-telemetry.md`](../integrations/mks-wifi/docs/motion-telemetry.md).
