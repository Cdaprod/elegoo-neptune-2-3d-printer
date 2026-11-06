# Neptune 2 — OctoPrint on Raspberry Pi

Optional, independent Docker Compose deployment for an Elegoo Neptune 2 running its **existing Marlin firmware**. **No firmware flashing required.** This stack is separate from `integrations/mks-wifi/` and does not alter Neptune Observatory.

## Prerequisites

- Raspberry Pi running Linux, Docker Engine and Docker Compose v2.
- A USB data cable connecting the Pi to the printer.
- Exclusive access to the printer serial connection while OctoPrint is controlling a print.
- A compatible OctoPrint Docker image for your Pi's CPU architecture.

## Deploy on the Pi

```bash
cd integrations/octoprint
cp .env.example .env
ls -l /dev/serial/by-id/
readlink -f /dev/serial/by-id/<your-printer-device>
```

Set `OCTOPRINT_SERIAL_DEVICE` in `.env` to the **resolved host device** returned by `readlink -f` (typically `/dev/ttyUSB0` or `/dev/ttyACM0`). This Compose file maps it into the container as `/dev/ttyUSB-printer`. If `/dev/serial/by-id/` is absent, inspect `dmesg` or `ls /dev/ttyUSB* /dev/ttyACM*` after connecting; do not guess a port.

```bash
docker compose config
docker compose up -d
docker compose ps
docker compose logs --tail=100 octoprint
```

Visit `http://<raspberry-pi-ip>:5000`, complete the OctoPrint first-run wizard, enable access control, and select **`/dev/ttyUSB-printer`** as the serial port. Use the printer's Marlin-compatible baud rate (try auto-detect first). Test connection, temperatures and homing **before** sending a print. If the serial device is missing, confirm the USB cable, host device path and permissions; update `.env` and recreate the container if the device path changes.

## Operational safety

**Neptune Observatory remains a separate deployment** and may still reach the same printer over MKS WiFi. Do not initiate prints, motion, heating, firmware updates or other G-code operations from both systems simultaneously. Prefer one active control path; disable Observatory command execution (set `NEPTUNE_CONTROL_EXECUTION_ENABLED=false` in its own environment and recreate that service) while commissioning OctoPrint. Passive monitoring may still generate queries, so verify compatibility before relying on simultaneous polling. Do not run two print jobs or connect multiple serial clients.

OctoPrint does **not** require Klipper, Moonraker or a firmware flash. The Pi stays USB-connected for OctoPrint control, but the original Marlin printer interface remains available when OctoPrint is disconnected.

## Touchscreen

OctoPrint's browser UI can be opened in a local Pi touchscreen browser in kiosk mode; a dedicated touchscreen frontend can be evaluated later. This Compose stack intentionally does not assume a particular display driver or XPT2046 calibration. Do not expose port 5000 to the public internet without an authenticated reverse proxy or VPN.

## Persistence and rollback

The `octoprint-data` Docker named volume preserves settings, uploads and plugins. Back it up before upgrades. `docker compose down` stops/removes the service **without** deleting its volume; avoid `down -v` unless you intend to erase OctoPrint data. Pin the image to a tested release/digest after initial commissioning.
