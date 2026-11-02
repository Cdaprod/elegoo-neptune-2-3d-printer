# Troubleshooting and FAQ

## The dashboard will not open

- Check `docker compose ps` and the health status.
- Confirm TCP port 8000 is available and the browser is using the host's current LAN address.
- On another device, ensure it is on the same non-isolated Wi-Fi/LAN and that the host firewall allows private-network access.
- Check logs with `docker compose logs --tail=100 neptune-observatory`.

## Sign-in fails

Use the exact token from the local `.env`. If changed, recreate the service with `docker compose up -d`; do not paste the token into screenshots or support posts. If lost, set a new value in `.env` and restart.

## Printer appears disconnected or stale

- Confirm `MKS_WIFI_HOST` resolves/reaches the printer and TCP port `MKS_WIFI_PORT` (normally 8080) is accessible.
- Stop other software that might own the MKS module's single native TCP client.
- Check the MKS module and printer are powered and on the same network.
- Review the status freshness and diagnostics response. Polling reconnects with bounded backoff; stale data remains labeled stale.

## Why are coordinates unavailable?

The target firmware's M114 response previously contained only `ok`. Without coordinates, the app shows unavailable position telemetry and can still show planned paths inferred from G-code/progress. These estimates are not measured locations.

## Why don't touchscreen movements appear in the command log?

The log records commands sent by Observatory. Touchscreen commands may be handled inside the printer firmware and never appear on the Wi-Fi socket. Temperature changes may be detected when telemetry exposes changed targets; exact motion and extrusion are unavailable unless firmware reports supporting state.

## Why is wireless upload disabled?

The source contains a possible upload pathway, but saving, integrity, and overwrite behavior have not been validated on the installed binary. The SD listing is not proof of download/upload capability. Keep the card workflow until this is verified.

## Storage or SQLite errors

Check Docker Desktop storage and `docker system df -v`. Reclaim only clearly disposable build cache. Do not prune volumes or the Observatory data volume. Use `/api/diagnostics` and the [storage guide](observatory-storage.md) to check persistence and SQLite health.

## Emergency handling

Network commands and the dashboard are not an emergency stop. If unexpected motion, heating, or extrusion occurs, use the printer's physical stop/power control and inspect the machine before resuming.
