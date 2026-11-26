# Raspberry Pi appliance prerequisites

Run `bash deploy/raspberry-pi/provision.sh` on Raspberry Pi OS ARM64.

## Display

Run as the desktop user, not root:

- `bash deploy/raspberry-pi/rotate-display.sh --list`
- `bash deploy/raspberry-pi/rotate-display.sh 90` for a temporary test.
- `bash deploy/raspberry-pi/rotate-display.sh 90 --persist` after checking touch corners.
- Use 270 for the opposite orientation or normal to restore portrait, with --persist to save that choice.

Persistence uses a dedicated XDG desktop autostart entry, without replacing labwc configuration. Remove `~/.config/autostart/neptune-display.desktop` (or the equivalent XDG_CONFIG_HOME path) to disable it. Verify after reboot; desktop startup ordering has not yet been physically tested.

The observed preferred HDMI-A-1 mode is 400x1280 at 60.234001 Hz, scale 1, with a successful 90-degree session transform. The profile retains legacy timings for reference only. No boot files are changed.

Cage does not inherit desktop autostart. Its future launcher must consume the same profile and apply the output transform through a compositor-supported mechanism before enabling production mode. Cage rotation and touch mapping remain unverified.

## Application

Follow the root README and `make up` authorization workflow; do not bypass OctoPrint application key approval. If using Docker without sudo, explicitly grant your trusted account docker group membership and reconnect; this is root-equivalent access. No automatic printer motion or kiosk activation is performed.

## Pi 4 commissioning (2026-11-26)

Raspberry Pi OS Desktop ARM64 (Trixie), native Wayland desktop, HDMI-A-1 at 400x1280 transformed 90 degrees to 1280x400. USB touch enumerates as 0712:000a, WaveShare WaveShare, using hid-multitouch and libinput touch capability. A replacement data-capable USB cable resolved absent enumeration. The observed feature-report mismatch warning did not prevent touch interaction. No custom driver or boot overlay was required. Corner accuracy and reboot persistence remain unverified.

Use `lsusb -d 0712:000a`, `sudo libinput list-devices` and `sudo dmesg --follow-new --human` to diagnose touch. Do not hardcode event4. If enumeration is absent, test a known data cable and USB port before changing calibration.

Run `make up` over SSH; approve the one-time app-key URL in a graphical browser on the LAN, replacing loopback with the Pi hostname/address. Keep the command running. `BROWSER=true make up` was verified as a workaround. The key is saved in private .env; use `make token` locally for the separate Observatory dashboard token. Never commit either credential. Finish OctoPrint's first-run wizard before authorization.

Both containers built and started, and LAN dashboard authentication was verified. OctoPrint should use RAW TCP to the configured MKS WiFi endpoint (port 8080), not RFC2217. Live telemetry and print submission remain unverified. Do not run a competing raw TCP client.

Use `make kiosk` from the active desktop account (or SSH with the active Wayland socket). Persistent kiosk startup and restricted local authentication are follow-up work; retain LAN token enforcement and machine-control safety gates. Do not expose the HTTP services publicly.

OctoPrint Python 3.10.18 is end-of-life. The observed working subset is Pillow 12.0.0, Slicer-Thumbnails 1.2.3, gcode-thumbnail-tool 0.1.4, OctoPrint-SpoolManager 1.7.8, Print-Job-History 1.17.5; pip check passed. Slicer-Thumbnails 1.2.4 failed resolving Pillow >=13.0.0. Avoid UI plugin upgrades; migrate to a supported Python base through tested ARM64/AMD64 builds, preserving the persistent volume. Full dependency locking and migration validation remain outstanding.

## Managed keyboard-free kiosk

Run `make kiosk-setup` once, then `make kiosk-enable` to install the user service and desktop autostart. Requires desktop auto-login for unattended boot. `make kiosk` starts in background, `make kiosk-status` checks systemd, `make kiosk-logs` attaches to a dedicated tmux journal viewer (Ctrl+B then D detaches), `make kiosk-stop` stops until next start/login, `make kiosk-disable` removes autostart and stops. `make kiosk-debug` is foreground only; stop managed service first. systemd owns the browser process, tmux is recreated at desktop login. Journal retention across reboot depends on journald storage configuration. Physical reboot validation pending. Existing LAN authentication remains required. The kiosk uses a dedicated browser profile with basic password storage; do not save unrelated passwords there.

## Commissioning handoff — 2026-11-26

Physical operator validation: Chromium now opens fullscreen under the managed kiosk service; touch taps and one-finger scrolling work without selecting text. The previous browser window showed automation chrome despite --kiosk. The repository launcher now requests no fixed Playwright viewport and omits --enable-automation; compare against the Pi-local patch before replacing it. The exact local launcher diff was not captured.

The working labwc rc.xml uses the Openbox namespace, a touch entry with deviceName `WaveShare WaveShare (USB 1-1.4)`, mapToOutput `HDMI-A-1`, mouseEmulation `no`, and a libinput device category matching that name with calibrationMatrix `0 1 0 -1 0 1`. USB topology is observed, not portable. Backups rc.xml.pre-calibration and rc.xml.pre-native-touch exist locally. Reload from SSH with `pkill -HUP -u "$(id -u)" -x labwc` (labwc --reconfigure failed because LABWC_PID was absent). Preserve unrelated XML and discover device name on each host. Profile values are recorded but automatic application is still pending.

### Next development: recover ultrawide HMI

The currently displayed localhost:8000 page is the standard desktop dashboard, not the dedicated 1280x400 HMI. Recover `feat/waveshare-79-hmi` and `feat/gcode-3d-fabrication-gallery` against current main; inspect ancestry and reconcile overlapping backend changes rather than blindly merging stale branches. Reuse existing hmi.html, hmi.js, hmi_model_viewer.js, gcode_fabrication.py and tests. Select the dedicated HMI route from the Waveshare kiosk profile, preserve shared backend and token enforcement, prioritize horizontal navigation and 3D model gallery, test 1280x400 overflow and touch gestures, and generate actual screenshots. Preserve desktop/mobile UI. No printer motion or heating during UI validation.

Before pulling on the Pi, inspect git status and diff; preserve local launcher edits and labwc backups. Reboot persistence, corner accuracy, live printer telemetry and print submission remain unverified. Do not reset local changes or commit .env.
