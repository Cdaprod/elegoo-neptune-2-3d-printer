# OctoPrint control adapter (opt-in)

The Observatory backend can now send a **small, allowlisted** set of commands to OctoPrint's REST API, which remains the exclusive owner of the verified MKS WiFi Raw TCP port 8080 connection.

## Fixed file-list error

On the SD/file catalog screen, OctoPrint API failures now take precedence over the static storage-provenance explanation. Previously cached rows are labeled as potentially outdated. No error is hidden by the OctoPrint storage message.

## Control API

**Default: disabled.** Set `NEPTUNE_OCTOPRINT_CONTROLS_ENABLED=true` only after the application key has printer-control permissions, the machine is attended, and the emergency power switch is accessible. Configure `NEPTUNE_PRINTER_BACKEND=octoprint`. The key stays server-side.

`POST /api/octoprint/control` requires Observatory authentication and the existing control token, same-origin validation, and JSON:

```json
{
  "operation": "home_x",
  "confirmation": "CONFIRM HOME_X",
  "attended": true,
  "power_switch_reachable": true
}
```

Allowlisted operations: `home_x`, `home_y`, `home_z` (only idle); `pause` (printing); `resume` (paused); `cancel` (printing or paused). Every command requires fresh (<=5s) connected telemetry and the exact confirmation text. The backend serializes submissions and reports *accepted* rather than falsely claiming that motion or print cancellation has completed.

This is an **API-first control foundation**, not yet a fully wired Observatory UI or complete replacement for the existing MKS executor. The existing direct MKS command routes remain locked in OctoPrint mode. There is no arbitrary G-code, jog, temperature-setting, print-start, file-upload, or emergency-stop implementation in this increment. Do not infer the REST API can guarantee emergency-stop delivery. Use the machine's physical stop/power isolation for emergencies.

The OctoPrint API key needs appropriate OctoPrint permissions for the selected action. HTTP 401/403, 409/422, timeouts, and connectivity failures return sanitized errors, never a silent fallback to native MKS.

## Validation

Run `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, and `uv run --no-sync pytest` from `integrations/mks-wifi`. Automated tests use stubbed OctoPrint endpoints; no live printer commands are issued by CI. Bench validation is still required before relying on controls during real printing.

## Observatory UI and recorded print history

The **Machine** view now presents the guarded OctoPrint controls when the OctoPrint backend is active. The browser retrieves `GET /api/octoprint/control/capabilities` before enabling buttons, requires the operator to acknowledge physical attendance and clear homing travel, and prompts for the exact operation confirmation. The server independently enforces these conditions, fresh state, and a short duplicate-command cooldown. The native MKS executor stays disabled in OctoPrint mode.

The **Print History** view uses `GET /api/print/history?limit=500` and Observatory's SQLite `print_sessions` table. It supports filename search, outcome filters, recorded timestamps, duration, progress and CSV export. This history reflects sessions observed while Observatory was running, not historical jobs that OctoPrint recorded before Observatory started. The session metadata retention is 365 days; the feedrate samples retain their separate shorter retention period. Session outcomes are explicitly marked unverified when the available evidence does not establish successful completion. The browser shows up to 500 latest records per request and reports the total separately; it does not yet implement pagination.

Do not enable machine controls without bench validation. Deploying these changes does not automatically enable them. Keep `NEPTUNE_OCTOPRINT_CONTROLS_ENABLED=false` for history-only monitoring.
