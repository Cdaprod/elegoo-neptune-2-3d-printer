# Commissioned Raspberry Pi appliance

The deployment uses one Observatory backend, OctoPrint, and the dedicated `/hmi`
presentation at localhost:8000. Desktop/mobile clients keep `/`. Run all Make
commands as the commissioned desktop user, not root. Production updates need
read access to GitHub, no GitHub write credentials or separate frontend deployment.

## Update the commissioned Pi

Inspect and preserve any local changes first (`git status --short`). Then:

```sh
cd ~/elegoo-neptune-2-3d-printer
git pull --ff-only origin main
make deploy
```

`make deploy` applies the hardware profile, installs the pinned Playwright runtime,
reuses the existing OctoPrint application key and Observatory token, builds the
existing Compose services, installs desktop autostart, and restarts the managed
kiosk. Existing Docker volumes and the browser profile are retained. Sudo may be
needed for the journald drop-in. First provisioning still uses `provision.sh` and
the documented one-time OctoPrint authorization workflow; updates do not reset it.
Never run `docker compose down -v` as part of an update.

## Physically validated baseline

`74fe9cf`, `a5d955e`, `2bd105a`, and **`c585943`** record commissioning.
The exact fullscreen implementation is captured at `c585943`; earlier statements
that its diff was uncaptured or native touch/fullscreen was pending are obsolete.

- Raspberry Pi 4, Raspberry Pi OS Desktop ARM64 Trixie, host `cda-rpi-4-printer`.
- Wayland/labwc 0.20.2, HDMI-A-1 native 400×1280, transform 90 → logical 1280×400.
- USB 0712:000a, hid-multitouch, actual device name discovered from sysfs on deploy.
- Touch maps to HDMI-A-1, matrix `0 1 0 -1 0 1`, `mouseEmulation="no"`.
- Operator confirmed fullscreen, tapping, and one-finger scrolling without text selection.
- Dedicated persistent Chromium profile, Playwright authentication, systemd user
  lifecycle, CDP native fullscreen, and persistent tmux journal viewer are retained.

`authenticated.py` retains no fixed viewport, omits `--enable-automation`, and
requests `Browser.setWindowBounds` fullscreen after authentication. The Waveshare
profile now selects `/hmi`; `NEPTUNE_KIOSK_PATH=/` is the explicit diagnostic fallback.
No Cage compositor or replacement kiosk is introduced.

## Idempotent host configuration

`configure-appliance.sh` and `configure_appliance.py` are called by `make deploy`.
They discover the touchscreen by vendor/product rather than event number or USB
port. Existing labwc namespace, unrelated elements and comments are preserved;
a single `rc.xml.pre-neptune` checkpoint is retained. Malformed XML or ambiguous/
absent touchscreen identity stops deployment rather than guessing. Only matching
WaveShare entries are reconciled; matrix and native touch values come from the
validated profile. Changes reload the running labwc user process.

Display rotation uses the existing `rotate-display.sh 90 --persist` desktop
autostart mechanism. Working EDID/boot settings are not replaced.

A dedicated `/etc/systemd/journald.conf.d/60-neptune.conf` sets:

```ini
[Journal]
Storage=persistent
SplitMode=uid
SystemMaxUse=128M
MaxRetentionSec=14day
```

Other journal configuration is preserved. Inspect effective settings with
`systemd-analyze cat-config systemd/journald.conf`; host-specific later drop-ins
can override these values. `make kiosk-logs` attaches to the existing tmux viewer;
Ctrl+B then D detaches. `make kiosk-status`, `make kiosk-restart`, `make kiosk-stop`
and `make kiosk-disable` retain their existing meanings. Desktop auto-login remains
necessary for unattended user-session startup.

## Unified HMI, Print Bay and fabrication gallery

The HMI has persistent machine status and horizontal Overview, Print Bay,
Fabrication gallery, Observations and Controls navigation. Print Bay reuses its
existing page in a same-origin compact presentation, with the same authentication
cookie and API; no duplicate app or backend is started. Plate previews use exported
STL meshes. Automatic build/slice/stage runs on the configured Mac worker; Pi-local
staged artifacts remain available offline. Configure the existing manufacturing
agent overlay if synchronizing from a Mac; no build-node credentials are invented
or required merely to deploy the appliance UI.

The root stack now exposes the existing OctoPrint volume read-only to Observatory;
only `/mnt/octoprint/octoprint/uploads` is indexed for fabrication geometry. Source files are
never modified. The standalone optional read-only host-directory overlay remains
available. Cached reconstruction lives in the existing Observatory data volume.
A remote printer SD filename alone cannot provide geometry without G-code bytes.

File selection never prints. Machine controls and files use the existing Observatory handlers within a bounded
HMI operations panel, with authentication, Operator Mode, readiness checks and
explicit confirmation.
Uploads and physical actions remain explicit. No motion, heating, uploads or print
submission are part of application validation.

## Verification and remaining acceptance

Browser regressions exercise exactly 1280×400, native CDP touch swipes, independent
horizontal gallery scrolling, isolated orbit gestures, document overflow, shared
login cookies, WebGL rendering and desktop/mobile parity. Screenshots in
`docs/screenshots/hmi-*-1280x400.png` are actual Chromium renders with synthetic
fixtures, not production telemetry or photographed CAD. CI builds/import-checks the
Observatory image on native ARM64, alongside backend/browser and OctoPrint image tests.

This software integration has not been installed on the physical Pi by Codex.
Reboot persistence, touch corner accuracy and performance of the integrated gallery
on its GPU remain on-device acceptance checks. Physical printing is not exercised.
The existing OctoPrint base/plugin Python migration remains separate work; no plugin
upgrade is performed by this integration. Keep HTTP services on a trusted LAN.

Native Robin Nano SD retrieval is documented in [the native SD integration guide](../../docs/native-robin-sd.md). Stock firmware remains unsupported for byte retrieval; experimental firmware is isolated and never flashed by `make deploy`.

Factory USB observations, exclusive ownership checks and the one-time USB migration
are documented in [Native Robin SD access](../../docs/native-robin-sd.md). Factory SD file-content retrieval remains unverified; no firmware flashing is part of deployment.
