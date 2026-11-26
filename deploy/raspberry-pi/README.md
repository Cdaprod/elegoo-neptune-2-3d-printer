# Raspberry Pi appliance prerequisites

Run `bash deploy/raspberry-pi/provision.sh` on Raspberry Pi OS ARM64. Then run `bash deploy/raspberry-pi/rotate-display.sh --list` and `bash deploy/raspberry-pi/rotate-display.sh 90` as the logged-in desktop user. Use 270 for the opposite orientation or normal to revert. An optional second argument selects HDMI-A-1 or another actual output. Rotation is session-only; persist through the desktop display settings after physical validation. Cage needs separate validation and configuration. No boot files are changed; legacy timing is reference only. Check touch corners before enabling controls.

For application deployment follow the root README and `make up` authorization workflow; do not bypass OctoPrint application key approval. If using Docker without sudo, explicitly grant your trusted account docker group membership and reconnect; this is root-equivalent access. No automatic printer motion or kiosk activation is performed.
