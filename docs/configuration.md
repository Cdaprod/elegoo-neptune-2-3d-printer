# Configuration and authentication

The Compose deployment reads `integrations/mks-wifi/.env`. Start with `.env.example`; deployment secrets and printer-specific values must remain local and must not be committed.

| Variable | Default | Notes |
| --- | --- | --- |
| `MKS_WIFI_HOST` | `printer.local` in template | Set to the current printer host/IP; required by Compose. |
| `MKS_WIFI_PORT` | `8080` | MKS native TCP endpoint. |
| `MKS_WIFI_TIMEOUT` | `2.5` seconds | Bound on an individual transport operation. |
| `NEPTUNE_OBSERVATORY_TOKEN` | none | Required for dashboard/API access in Compose. Use a unique random token. |
| `NEPTUNE_POLL_INTERVAL_SECONDS` | `2` | Shared telemetry polling interval. |
| `NEPTUNE_MOTION_POLLING_ENABLED` | `false` | Periodic idle-only position query opt-in. Current firmware response has not yielded usable coordinates. |
| `NEPTUNE_MOTION_QUERY_ENABLED` | `false` | Enables the separate one-shot position query UI. |
| `NEPTUNE_MOTION_POLL_INTERVAL_SECONDS` | `10` | Position query rate limit. |
| `NEPTUNE_CONTROL_EXECUTION_ENABLED` | Compose `true` | Enables fixed command handlers; Operator Mode still starts disarmed. |
| `NEPTUNE_EXTRUSION_MIN_TEMP_C` | `180` | Fresh nozzle temperature required for extrusion operations. |
| `NEPTUNE_SD_UPLOAD_ENABLED` | `false` | Keep disabled until installed-firmware upload and integrity behavior are established. |
| `NEPTUNE_COMMAND_TIMEOUT_SECONDS` | `15` | Bounded command operation timeout. Ambiguous operations are not retried automatically. |

The complete Compose file also accepts documented optional polling and state-storage overrides. Legacy allowlist variables are intentionally ignored; code-level fixed templates and validation are authoritative.

## First sign-in and LAN access

The dashboard uses the local token as its existing sign-in. Keep the service on a trusted network. To access it from another device, publish/bind it to the LAN (Compose already maps host port 8000), allow that port through the host firewall only on the private network, and open `http://<host-lan-address>:8000`. The browser session uses an HTTP-only cookie. HTTP is not encrypted; do not expose the service to the public internet or use it on untrusted Wi-Fi.

Operator Mode is separate from sign-in. It starts disarmed after each service startup and must be armed deliberately before machine controls are used. Arming does not bypass per-action safety checks or confirmation.

## Secret handling

- Never commit `.env`, access tokens, Wi-Fi credentials, command logs, SQLite files, or actual private printer addresses.
- Rotate a token if it was shared or exposed. Restart the Compose service after changing environment values.
- A browser cookie is a temporary authenticated session; signing out or restarting invalidates access.
- Review firewall/network isolation settings. Router port forwarding is not supported or recommended.
