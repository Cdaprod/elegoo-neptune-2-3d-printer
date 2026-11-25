# Waveshare 7.9-inch Neptune HMI

The same Neptune Observatory container now exposes two frontends:

- `/` — existing desktop/mobile Observatory including authenticated printer controls.
- `/hmi` — 1280 × 400 landscape, touch-first, **read-only** Waveshare interface.

The display is mounted vertically, facing the operator, on the top stationary chassis extrusion. The UI itself is landscape, with no tilt compensation or scaled desktop widgets.

## Deployment (no additional app or backend)

The HMI is included in the existing Observatory image and is already served on the regular Observatory port (`http://<pi-host>:8000/hmi`). No separate frontend service, Quantum File Browser container, new credentials, or additional printer connection is required.

To also publish an optional dedicated HMI port, use the standalone Observatory compose:

```sh
docker compose -f integrations/mks-wifi/compose.yaml -f integrations/mks-wifi/compose.hmi.yaml up -d --build
```

Open `http://<docker-host>:8001/hmi` for the same backend, or use port 8000. To use the root OctoPrint-combined stack, run `docker compose up -d --build` from the root directory and open `http://<docker-host>:8000/hmi`; do not launch a second independent Observatory against the same persistent volume.

The Compose overlay only changes published ports. It does not change how the application accesses OctoPrint, the MKS printer, credentials, the history database, or the existing Docker volume.

## Waveshare kiosk

Set the HDMI display to its physical landscape native mode **1280 × 400**, and configure touch orientation to match. On Raspberry Pi OS, enable proper multitouch rather than pointer-only emulation when swipe gestures are desired. In a kiosk session already configured with an unlocked local browser profile, launch:

```sh
chromium --kiosk --no-first-run --disable-session-crashed-bubble http://127.0.0.1:8000/hmi
```

When the Observatory runs elsewhere, use its reachable hostname instead of localhost. The kiosk does not embed a token in the URL or environment; log in using the Observatory token over a trusted network. Use HTTPS through a reverse proxy when clients connect over an untrusted network. Authentication uses the existing 12-hour HttpOnly session cookie.

The HMI is designed to occupy the browser's actual viewport, not scale the desktop dashboard. Avoid OS/browser scaling that makes 1280 × 400 physical pixels different from the CSS viewport. Test touch-target size on the real panel.

## Features

- Compact persistent live connection/nozzle/bed header.
- Swipeable Overview / Models / Observations / Controls views, plus explicit large nav buttons.
- Independently horizontally scrollable model cards with touch scroll snapping.
- Shaded G-code-derived 3D geometry thumbnails for mounted files (with cached OrcaSlicer PNG/JPEG fallback), plus a touch-friendly interactive Three.js inspector.
- Existing printer SD cached listing and thumbnail endpoint, with source indications.
- Read-only observed nozzle/bed history plots with no fabricated samples.
- Offline, stale, and authentication feedback.
- Safe handoff to full Observatory for operator-gated machine controls.

**Important limitations:** The 3D gallery reconstructs an approximation of extruded filament from **locally mounted G-code**. Without local G-code bytes, printer SD listings still rely on the old thumbnail fallback. No original STEP/STL/3MF meshes, arbitrary mounted file browser, camera integration, or printer-control shortcuts are added. Selecting a model never starts a print. For rendering limitations, authentication, mount configuration and caching, see [G-code fabrication gallery](gcode-fabrication-gallery.md).

To ingest local G-code previews, mount a read-only directory at `/mnt/neptune-source` in the existing `neptune-observatory` container, as documented in the existing storage/setup guides. Avoid storing runtime files in the repository.

## Smoke test

Run the API tests and JavaScript syntax checks in the existing development environment:

```sh
cd integrations/mks-wifi
uv run --extra web pytest tests/test_observatory.py -q
cd ../..
node --check integrations/mks-wifi/src/mks_wifi_bridge/static/hmi.js
```

With Chromium and Playwright available, run `node scripts/test-hmi-navigation.cjs` against a locally running Observatory on port 8000. The browser script verifies 1280 × 400 geometry, touch nav, and that the existing dashboard remains available. It does not dispatch printer commands.
