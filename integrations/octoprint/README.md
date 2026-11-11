# Neptune 2 — OctoPrint: network-first, USB fallback

This integration runs OctoPrint alongside the existing Neptune Observatory repository without changing the printer's Marlin firmware. **Network mode is the default** and works on Docker Desktop for macOS without a USB device. The MKS WiFi raw TCP connection has already shown an operational status and live temperatures through OctoPrint's Remote Connection plugin on the target network. Final deployment validation remains host-specific.

## Network deployment (MacBook or Raspberry Pi)

From the repository root:

```bash
docker compose up -d --build
```

The root Compose stack includes both OctoPrint and Observatory on the same network. Configure the **root** `.env` from [../../.env.example](../../.env.example), then start both using `docker compose up -d --build`. OctoPrint is available at `http://localhost:5001` (configurable via root `OCTOPRINT_HOST_PORT`); Observatory uses `http://octoprint:80` internally. Complete the OctoPrint first-run wizard and enable access control if this is a new installation.

### Remote Connection plugin

The custom [Dockerfile](Dockerfile) builds offline wheels for **OctoPrint-Remote_connection** during image build and the startup initializer installs them into the active Python environment from the URL tracked in [plugins.txt](plugins.txt):

```text
https://github.com/mstarostik/OctoPrint-Remote_connection/archive/main.zip
```

The image now ships a startup initializer, `91-configure-mks-remote`, which preconfigures the Remote Connection plugin with `OCTOPRINT_MKS_WIFI_HOST` and `OCTOPRINT_MKS_WIFI_PORT=8080` as **Raw TCP (`socket`)**, not RFC2217. The initializer adds the endpoint without deleting existing remote connections and selects it only if no prior serial port was deliberately selected. The settings survive normal rebuilds inside the existing `/octoprint` volume. An existing port choice is preserved, so check OctoPrint's Connection panel if you previously configured USB or another connection.

**Connection is intentionally manual by default.** `OCTOPRINT_MKS_AUTO_CONNECT=false` prevents automatic reconnection to the printer when the Docker stack starts. After verifying the selected `REMOTE:socket://<printer-host>:8080` port, connect from the OctoPrint UI. Set `OCTOPRINT_MKS_AUTO_CONNECT=true` only when you want OctoPrint to connect on startup and its current selected port matches the configured MKS remote. Raw TCP compatibility should be checked on the actual printer before printing.

**Single-client constraint:** OctoPrint exclusively owns the MKS TCP connection in the root stack. Observatory is forced into its OctoPrint API backend and does **not** connect directly to MKS. Do not start a separate direct-MKS Observatory, printer utility, or other competing native socket client during operation. The root stack needs a valid `OCTOPRINT_API_KEY` and `NEPTUNE_OBSERVATORY_TOKEN` in the private root `.env`. OctoPrint startup/connection issues are surfaced as stale Observatory data rather than silently falling back.

## USB fallback (Linux Raspberry Pi)

Connect a USB data cable between the Pi and printer. Find the actual device:

```bash
ls -l /dev/serial/by-id/
readlink -f /dev/serial/by-id/<your-printer-device>
```

Set `OCTOPRINT_SERIAL_DEVICE` in `integrations/octoprint/.env` to the resolved host path (for example `/dev/ttyUSB0` or `/dev/ttyACM0`). From the **integration directory**, run:

```bash
cd integrations/octoprint
docker compose -f compose.yaml -f compose.usb.yaml up -d
```

Select `/dev/ttyUSB-printer` in OctoPrint's Connection panel. USB passthrough is for a Linux Docker host, not macOS Docker Desktop. The USB override is deliberately not included by the root Compose stack.

## Persistence, touchscreen and safety

The `octoprint-data` named volume retains configuration, printer profiles, uploads, and plugin settings. The plugin's Python package is installed in the image on every rebuild. Do not use `docker compose down -v` unless you intend to delete this data. Back up the volume before major upgrades. Raspberry Pi touchscreen clients can access OctoPrint over the LAN in a browser kiosk; no printer USB connection is needed at the display if OctoPrint runs elsewhere.

Do not expose port 5001 to the public internet. The existing Marlin firmware and original printer display remain intact. Network and USB connection modes are **alternatives**, not two simultaneous connections.

## Rebuild and verify

From the repository root:

```bash
git pull --rebase
docker compose --profile octoprint up -d --build
docker compose logs --tail=100 octoprint
```

Open `http://192.168.0.9:5001` on the local network and check **Settings → Plugin Manager** for Remote Connection. The build installs the package; its settings are entered once in the UI and persisted to `octoprint-data`. A normal `up -d --build` does **not** delete the volume. Do not use `down -v` when redeploying. The image build requires GitHub access to download the plugin; once built, the running container does not need to download it again.

The current image inherits the upstream Python version; a Python end-of-life warning is separate from network connectivity and should be addressed with a tested base-image upgrade. The plugin and MKS TCP handshake have not been verified against the physical printer.

## PrintJobHistory plugin (bundled and persistent)

The image now bundles **PrintJobHistory 1.17.5** from the official OctoPrint plugin repository's maintained `vojtakaniok` fork. The image build downloads its release archive and Python dependencies into the offline wheelhouse. The container startup initializer installs the pinned plugin into the active OctoPrint interpreter and verifies the `PrintJobHistory` plugin entry point before OctoPrint starts.

The plugin uses its default local SQLite storage under OctoPrint's persistent `/octoprint` volume. No external database, credentials, or runtime download is required. Its database is created by the plugin when OctoPrint loads it. Existing jobs are **not backfilled**: the plugin begins collecting results after it is installed and enabled. The existing Remote Connection raw TCP configuration is preserved by the volume.

**Observatory history:** Set `NEPTUNE_PRINTER_BACKEND=octoprint` and `OCTOPRINT_API_KEY` in the deployment environment, and keep `NEPTUNE_OCTOPRINT_CONTROLS_ENABLED=false` for monitoring only. Observatory's Print History page can switch between its own observed sessions and the PrintJobHistory plugin's reported outcomes. The adapter queries the authenticated, read-only `/plugin/PrintJobHistory/loadPrintJobHistoryByQuery` endpoint. An OctoPrint API key with permission to access that endpoint is required. A failed plugin call is reported; no direct MKS connection or printer command is attempted.

Rebuild with `docker compose --profile octoprint up -d --build`, preserving `octoprint-data`. Check `docker compose logs octoprint` for the startup plugin verification, then verify **Settings → Plugin Manager → Print Job History**. Plugin startup compatibility still requires deployment verification; CI mock tests cannot prove plugin activation on your specific running image.

## Dark mode (Themeify)

The custom image bundles and installs [Themeify](https://github.com/birkbjo/OctoPrint-Themeify) alongside Remote Connection during `docker compose --profile octoprint up -d --build`. Once rebuilt, open **Settings → Plugins → Themeify** and select its **Dark** theme. Themeify's theme selection is stored in OctoPrint's persistent `octoprint-data` volume and should survive future rebuilds; the image install alone does not guarantee Dark is selected automatically. For a correctly styled header, Themeify's documentation recommends **Settings → Appearance → Color: default** and disabling **Transparent Color**.

### Startup plugin recovery

The image builds a local wheelhouse under `/opt/octoprint-plugin-wheels`. The s6 initialization script `/etc/cont-init.d/90-install-octoprint-plugins` installs Remote Connection 0.1.1 and Themeify 1.2.2 using `--no-index`, then verifies both `octoprint.plugin` entry points. This addresses the observed case where packages existed in the built image but were absent in the running container. No plugin download is needed at container startup; installation is idempotent and preserves the `/octoprint` configuration volume. If the startup check fails, inspect `docker compose logs octoprint` for `[neptune-plugins]` lines. Verify with `docker exec neptune-octoprint python3 -m pip list | grep -Ei 'themeify|remote'`. The initialization fix still requires validation on the MacBook.
