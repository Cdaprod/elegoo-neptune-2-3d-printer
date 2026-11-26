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
