# Network discovery record

## User-provided observations (not repeated here)

| Probe | User-reported result | Notes |
| --- | --- | --- |
| TCP 80 | Open | MKS WiFi web management interface |
| TCP 8080 | Open | Native MKS printer communication interface |
| HTTP GET `/` on port 80 | 200 | Management HTML includes firmware/filesystem/WiFi configuration forms |
| HTTP HEAD on port 8080 | 200 | Not evidence that port 8080 is an HTTP API |
| HTTP GET on port 8080 | No useful response | Consistent with raw TCP G-code service |
| GET `/api/files/sdcard` on port 80 | 404 | Source contains a file-list handler, but its HTTP route registration is disabled |
| Plain-text `M105` on port 8080 | No visible response | Does not establish that the native line protocol is unsupported; response framing/connection behavior still requires live probe |

## Source findings

- Makerbase source binds raw TCP 8080 and HTTP management on port 80.
- HTTP `/update_sketch`, `/update_spiffs`, and `/update_cfg` are management
  functions. Never call them during this read-only task. Do not submit forms or
  query routes with configuration parameters.
- The OctoPrint-style HTTP handlers include control methods and a file-list
  handler, but registration is under `#if 0` in the inspected source revision.
- The active raw TCP G-code service and HTTP management server are different
  interfaces. An HTTP success response on port 8080 is not required for raw
  TCP telemetry to work.

## Live testing policy and results

The probe CLI is gated by `--confirm-single-client-risk`. This reflects the
Makerbase firmware's single-client behavior: connecting can disconnect another
currently connected host. The tested printer address is intentionally omitted
from this public note. Automated tests use mocked endpoints.

## Live read-only results (2026-10-31)

- The configured printer TCP endpoint on port `8080` connected and returned a
  parseable MKS status.
- Printer state: idle; print-active: false; progress: 0%; reported elapsed: 0 s.
- Nozzle: 24 °C, target 0 °C on the first probe; 23 °C, target 0 °C on the
  combined telemetry/listing probe. These are ambient, read-only observations.
- Bed: 24 °C, target 0 °C on the first probe; 23 °C, target 0 °C on the second.
- M115 identification: `TFT28/32`. This is the MKS/board identifier returned
  by the WiFi module, not a complete Marlin build string.
- M20 returned a complete root listing (start and end markers present), with 18
  entries and no `elegoo.txt` in that listing. This does not establish whether
  a similarly named file is in another directory or whether the card changes.
- The result does not prove that arbitrary file contents can be downloaded. No
  such read interface was invoked or identified.
- The live probe opened sequential, short-lived TCP sessions and sent only the
  fixed read-only telemetry queries plus one `M20`. No temperatures, settings,
  files, firmware, or print state were changed.

## Neptune Observatory live check (2026-10-31)

- FastAPI started on `127.0.0.1:8000`; `/health`, status, temperature,
  firmware, and OpenAPI/Swagger routes returned successfully.
- The dashboard WebSocket displayed the actual printer state, temperature
  readings, and firmware identifier while the shared two-second poller ran.
- A manual `/api/printer/sdcard?refresh=true` request returned an available
  18-entry root listing. The UI displayed it as directory/file rows with unknown
  sizes. It did not expose file-content or transfer actions.
- Responsive layout was inspected at desktop `1440×900` and phone-sized
  `390×844` browser viewports. At 390 px, document width was 375 px with no
  horizontal overflow; a physical iPhone Safari session was not available for
  direct verification.
- The server was stopped after the live check. Automated application tests use
  fake clients and a fake SD reader only; they never contact the printer.

## Neptune Observatory live check (2026-11-05)

- The deployed FastAPI health endpoint returned `ok`; the shared poller reported
  a fresh MKS TCP connection. Telemetry reported idle, no active print, and
  ambient nozzle/bed temperatures (25 °C, both targets 0 °C). These are
  read-only observations; no printer state was changed.
- A fresh `/api/printer/sdcard?refresh=true` request returned a complete
  firmware `M20` root listing with 17 entries. The listing exposes filenames,
  not file bytes or thumbnails.
- A read-only HTTP `GET /api/files/sdcard` on port 80 returned `404`. A TCP
  connect-only probe to port `11188` was refused. Together with the inspected
  Makerbase source (where the OctoPrint-style file routes are disabled and no
  listener is registered on the download-port constant), these checks found no
  usable remote G-code read path on this device. This does not establish the
  behavior of other MKS firmware builds.
- The configured local G-code index reported its source unavailable; all 17
  live SD entries were unmatched to cached local metadata and had no preview.
  No upload, file selection, print, motion, heat, extrusion, or other
  state-changing command was issued.
