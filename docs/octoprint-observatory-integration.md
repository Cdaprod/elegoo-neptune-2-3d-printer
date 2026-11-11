# Neptune Observatory ↔ OctoPrint integration plan

Status: **design / implementation contract, not commissioned**.

## Goal

Preserve the existing Neptune Observatory FastAPI dashboard, operator controls, history, and Three.js Digital Twin. Add an opt-in OctoPrint backend for printer monitoring, job management, and guarded controls. OctoPrint must be the **sole authority** for printer I/O in OctoPrint mode. Do **not** start a second native MKS WiFi socket from Observatory.

Current implementation anchors:
- `integrations/mks-wifi/src/mks_wifi_bridge/observatory.py`: `PrinterService` owns status polling, cache, history, SD listing, and websocket updates, currently hard-wired to `MksWifiClient`.
- `integrations/mks-wifi/src/mks_wifi_bridge/executor.py`: `CommandTransport` and `MksTcpCommandTransport`, with guarded fixed-command executor and outcome accounting.
- `integrations/mks-wifi/src/mks_wifi_bridge/static/`: UI and Digital Twin. Avoid replacing it.
- `integrations/octoprint/compose.yaml`: optional OctoPrint service, currently behind Compose profile.
- `integrations/octoprint/README.md`: MKS raw TCP Remote Connection is experimental and unverified against the printer.
- `docs/development.md`: identifies the domain-level backend adapter seam.

## Architecture and mode isolation

```text
Browser / Observatory UI
       │ authenticated app API + WebSocket
       ▼
FastAPI + normalized domain state + existing history/guardrails
       │
       ├── driver=mks        → serialized MKS native TCP (current behavior)
       │
       └── driver=octoprint  → OctoPrint REST / push event adapter
                                      │
                                  OctoPrint
                                      │
                            one physical transport
                            (USB OR verified MKS TCP)
```

- Add `NEPTUNE_PRINTER_BACKEND=mks|octoprint` with `mks` default to preserve current deployments.
- Add `OCTOPRINT_BASE_URL` and an authenticated `OCTOPRINT_API_KEY` (passed via local secret file or runtime environment; never committed, logged, rendered to UI, or embedded in browser JS).
- In `octoprint` mode, exclude all MKS direct polling, command dispatch, SD listing, HTTP uploads, and M114 queries. Do not silently fall back to direct MKS on OctoPrint failure.
- Keep physical connection uniqueness operationally enforced: even when OctoPrint uses USB, don't leave a separate native MKS command controller active.
- Add clear backend/source/connection state to the Observatory UI and API; distinguish “OctoPrint reachable” from “printer connected”.
- Keep source-aware statuses: firmware-reported, OctoPrint-reported, commanded, acknowledged by HTTP, physically confirmed, estimated, stale, and unavailable.

## Normalized API and adapter

Define an interface beneath `PrinterService`, with async methods:
- `read_state()` → normalized connection, state, nozzle/bed temperatures, active file, print progress, elapsed/remaining time, observed timestamp and confidence.
- `list_files()` → normalized local/SD file entries, provenance and availability. OctoPrint file storage and native printer SD listings are different sources.
- `execute_operation(operation)` → guarded typed operation with structured response including dispatch/acceptance vs physical completion.
- `close()` → release HTTP session/resources.

Implement `MksBackend` wrapping current behavior (minimize code movement) and `OctoPrintBackend` with `httpx.AsyncClient` using bounded deadlines and scoped API key. Reuse existing `PrinterService`'s cache, data history, event broadcasting, and guarded action model. No new raw-G-code terminal or broad proxy endpoint.

Suggested OctoPrint mappings (check official OctoPrint API docs and permission requirements for version installed):

| Purpose | OctoPrint API |
| --- | --- |
| Server reachability | `GET /api/version` |
| Connection status | `GET /api/connection` |
| Live printer state | `GET /api/printer` |
| Current job/progress | `GET /api/job` |
| Files and metadata | `GET /api/files` |
| Pause/resume/start/cancel | `POST /api/job` with documented typed payload |
| Read-only event updates | OctoPrint push/SockJS protocol, otherwise bounded polling |
| Limited movement/thermal commands | Official printer API operations; preserve existing safety gating |
| File upload/select | Official files API, controlled/guarded and disabled pending tests |

Do not assume that generic job data exposes real-time axis position, layer index, camera stream, or printer SD read-back. Digital Twin file visualization remains a *simulation* of its actual parsed G-code. Associate job/file hash only when verifiable.

## Delivery slices

### Slice 1: Read-only working backend
1. Add adapter interface and OctoPrint client implementation.
2. Configuration validation and API key redaction.
3. Use `NEPTUNE_PRINTER_BACKEND=octoprint` to populate existing `/api/status`, `/api/temperatures`, job summary, and WebSocket data.
4. Handle printer-offline, OctoPrint-offline, invalid key, session disconnect, timeout, stale telemetry, empty job, unsupported telemetry.
5. Ensure no native MKS connections are attempted in OctoPrint mode.
6. Unit tests with mock HTTP transport and regression tests for original MKS mode.

### Slice 2: Files and lifecycle
1. Connect file browser to OctoPrint files API with correct storage provenance.
2. Map OctoPrint job lifecycle into `print_sessions` history without duplicating transitions.
3. Implement typed, guarded job actions with state conflict handling; start disabled until explicitly tested.
4. Keep native SD and OctoPrint local uploads distinct.

### Slice 3: Safe machine controls
1. Keep Operator Mode disarmed on startup, authentication, confirmation, freshness and state checks, bounds and thermal interlocks.
2. Route existing fixed commands via OctoPrint's supported printer API, not a direct MKS socket.
3. Treat accepted HTTP response as *request accepted*, not mechanical completion.
4. Explicitly mark adapter-unsupported controls unavailable.
5. Never automatically retry ambiguous motions/heating/print lifecycle actions.

### Slice 4: Deployment and UI polish
1. Share an internal Compose network between Observatory and OctoPrint. Do not hardcode localhost for inter-container requests.
2. Example container-to-container endpoint: `http://octoprint:80`, with configured runtime secret; preserve OctoPrint persistent volume.
3. Keep the original Observatory styling/components, add backend indicator, connected state, and print job context.
4. Test desktop/mobile layouts and Digital Twin rendering. Existing MKS mode remains the safe default.
5. Add integration instructions and feature matrix.

## Acceptance criteria

- Both web applications can be running, but ONLY OctoPrint opens an actual printer session in OctoPrint backend mode.
- Observatory displays fresh real OctoPrint nozzle/bed readings and job information after authenticated connection.
- Unreachable OctoPrint turns status stale/offline with actionable error, not fabricated normal printer state.
- Historical temperature points and job transitions persist without duplicate events.
- All requests from browser terminate at Observatory; OctoPrint key never reaches frontend and never appears in logs.
- All dangerous actions require deliberate enablement/confirmation, and failures have an explicit outcome state.
- MKS mode continues to behave and test as before.
- `uv run --no-sync pytest -q`, `uv run --no-sync ruff check src tests`, `uv run --no-sync ruff format --check src tests`, `node --test tests/*.mjs`, and Compose config validation pass with harmless fixture config.
- No live printer commands, firmware flashing, uploads, heating or movement during software development and CI.

## Commissioning decision

The OctoPrint Remote Connection plugin currently uses raw TCP port 8080 as an **unverified experiment**. Prefer tested OctoPrint USB serial on a Linux Raspberry Pi host if the MKS protocol proves incompatible. Network endpoint reachability does not establish correct OctoPrint G-code sequencing. Bench-test connection and read-only state first, then physically supervise one deliberate command only after explicit user authorization.

## Documentation

- https://docs.octoprint.org/en/main/api/index.html
- https://docs.octoprint.org/en/main/api/general.html
- https://docs.octoprint.org/en/dev/bundledplugins/appkeys.html
