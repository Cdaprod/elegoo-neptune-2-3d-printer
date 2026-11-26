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
