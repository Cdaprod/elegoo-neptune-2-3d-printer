# SD-card print state reconciliation (MKS HTTP + OctoPrint)

## Observed evidence — 12 November 2026

During a touchscreen/SD-started Neptune 2 print, the operator observed OctoPrint Terminal replies `M27 7` and subsequently `M27 8`, while the OctoPrint job stayed Operational/0%.

[Official Makerbase MKS-WIFI source](https://github.com/makerbase-mks/MKS-WIFI/blob/master/firmware_source/MksWifi/MksWifi.ino) returns `M27 %d` from its cached `print_rate`. The separate MKS HTTP server exposes `GET /api/printer` (including `state.flags.printing` and `state.flags.paused`) and `GET /api/job`. The latter represents completion as `print_rate / 100`, e.g. `0.08` means **8%**. On idle, upstream `handleApiPrintInf` sends **no HTTP response**, so Observatory does not query /api/job unless /api/printer reports printing/paused.

## Runtime architecture

Root Compose retains **OctoPrint as the sole raw MKS TCP port-8080 client**. When `OCTOPRINT_MKS_WIFI_HOST` is a private literal IP, Observatory can separately query MKS **HTTP port 80**, using fixed read-only GET paths, bounded response size and timeout, and redirects disabled. It does not send G-code or establish another raw TCP connection.

In the existing 2-second shared poller, an MKS HTTP status is attached as `telemetry.external_sd`. OctoPrint's own `printer_state` and `print_active` fields remain unchanged for machine-control interlocks. The print-job summary and digital twin use the reconciled result. An OctoPrint host job always wins over a contradictory MKS cached idle state, with the contradiction identified. When OctoPrint reports idle and MKS state is missing, **physical printer activity is unknown**, not confirmed idle.

The persisted SQLite recording uses the MKS state and progress when the host job is idle; it records a distinct `MKS_HTTP_SD_telemetry` source. Missing or stale MKS evidence does not close an existing print session. A subsequent MKS-reported idle state closes the session with the outcome **ended/unverified**, not physical success. G-code path mapping and the digital twin remain estimates: this source contains no independent motion or extrusion sensor values.

## Configuration

The root `compose.yaml` includes `integrations/mks-wifi/compose.octoprint.yaml`, which passes `OCTOPRINT_MKS_WIFI_HOST` and these optional overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEPTUNE_MKS_HTTP_MONITOR_ENABLED` | `true` | Enable separate read-only MKS HTTP status when host configured |
| `NEPTUNE_MKS_HTTP_PORT` | `80` | MKS HTTP server port, not raw G-code port |
| `NEPTUNE_MKS_HTTP_TIMEOUT_SECONDS` | `1.5` | Per-request timeout, capped at 10 s |

When the HTTP endpoint is inaccessible or has different firmware behavior, OctoPrint temperatures and jobs remain operational, and the dashboard marks MKS physical activity unavailable. Use `NEPTUNE_MKS_HTTP_MONITOR_ENABLED=false` to explicitly turn it off. No machine calibration, SD print control, or UI action is automatically initiated.

## Verification gates

- Hardware-free tests: fractional progress, 7→8 percentage, paused, idle, malformed replies, private-IP validation, HTTP error degradation, conflicting host job, and SQLite unverified session.
- CI: Ruff, pytest, and dashboard checks must pass.
- Live acceptance (pending a running deployment): compare Observatory SD progress to touchscreen percentage, confirm OctoPrint host-job state stays separate, check idle transition creates an unverified history entry, confirm HTTP loss preserves temperatures and shows unknown.
- Source firmware behavior is documented, **but the installed MKS HTTP firmware has not been live-tested** by this change.
