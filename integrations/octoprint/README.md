# Neptune 2 — OctoPrint: network-first, USB fallback

This integration runs OctoPrint alongside the existing Neptune Observatory repository without changing the printer's Marlin firmware. **Network mode is the default** and works on Docker Desktop for macOS without a USB device. Network transport is experimental until the MKS WiFi raw TCP protocol has been validated with OctoPrint.

## Network deployment (MacBook or Raspberry Pi)

From the repository root:

```bash
docker compose --profile octoprint up -d --build
```

OctoPrint is available at `http://localhost:5001` (override `OCTOPRINT_HOST_PORT` in `integrations/octoprint/.env`). Complete the first-run wizard and enable access control.

### Remote Connection plugin

Install **OctoPrint-Remote_connection** through Settings → Plugin Manager → Get More → Install from URL using the URL tracked in [plugins.txt](plugins.txt):

```text
https://github.com/mstarostik/OctoPrint-Remote_connection/archive/main.zip
```

Restart OctoPrint. In the plugin's remote-connection settings, add the MKS WiFi hostname/IP and TCP port **8080**, selecting **raw TCP** (not RFC2217). Choose the new remote port in OctoPrint's Connection panel. The module accepts line-delimited G-code over a raw socket; its compatibility with OctoPrint's streaming/acknowledgment expectations remains **unverified**. Do not start a print until read-only M115/M105 tests and connection stability pass. This plugin is listed in the repository but **not automatically installed** by Docker Compose; use Plugin Manager for the first install. The persistent OctoPrint volume retains plugin installation.

**Single-client constraint:** MKS WiFi accepts only one active TCP client. Stop Observatory completely before testing OctoPrint:

```bash
docker compose stop neptune-observatory
```

Do not simultaneously operate both controllers. Avoid heating, jogging, SD printing, or uploading during initial connection tests. To restore Observatory, disconnect OctoPrint from the printer first, then `docker compose start neptune-observatory`.

## USB fallback (Linux Raspberry Pi)

Connect a USB data cable between the Pi and printer. Find the actual device:

```bash
ls -l /dev/serial/by-id/
readlink -f /dev/serial/by-id/<your-printer-device>
```

Set `OCTOPRINT_SERIAL_DEVICE` in `integrations/octoprint/.env` to the resolved host path (for example `/dev/ttyUSB0` or `/dev/ttyACM0`). From the **integration directory**, run:

```bash
cd integrations/octoprint
docker compose -f compose.yaml -f compose.usb.yaml --profile octoprint up -d
```

Select `/dev/ttyUSB-printer` in OctoPrint's Connection panel. USB passthrough is for a Linux Docker host, not macOS Docker Desktop. The USB override is deliberately not included by the root Compose stack.

## Persistence, touchscreen and safety

The `octoprint-data` named volume retains configuration, uploads and installed plugins. Do not use `docker compose down -v` unless you intend to delete this data. Back up the volume before major upgrades. Raspberry Pi touchscreen clients can access OctoPrint over the LAN in a browser kiosk; no printer USB connection is needed at the display if OctoPrint runs elsewhere.

Do not expose port 5001 to the public internet. The existing Marlin firmware and original printer display remain intact. Network and USB connection modes are **alternatives**, not two simultaneous connections.
