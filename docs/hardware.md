# Hardware support

## Target hardware

- **Printer:** Elegoo Neptune 2, Cartesian bed-slinger.
- **Mainboard:** MKS Robin Nano V1.2 / ZNP variant; STM32F103VET6 reported by the user.
- **Network module:** MKS WiFi ESP8266.
- **Display:** stock Elegoo touchscreen (the `TFT28/32` WiFi response is not a complete touchscreen or Marlin version report).
- **Slicer:** OrcaSlicer G-code can be inspected and replayed offline.

The application talks to the MKS WiFi module. It does not require firmware replacement and does not inspect CAD projects.

## Compatibility evidence

The dashboard and shared poller have read printer status, temperatures, firmware identifier, print status, and an SD-card root listing from the target setup. `G28 X` was previously observed by the operator to move the carriage to its reference switch and stop normally. The app logs this as historical operator-observed evidence; it does not restore homed state after restart.

Most action templates follow Marlin-style commands, but command support and behavior depend on the installed Elegoo/MKS firmware build. A generic Marlin command reference is not proof that a command is enabled in this build. See [feature status](feature-status.md) for per-feature limitations.

## Known constraints

- The MKS module's native TCP service supports one active client in the inspected Makerbase source; an Observatory connection can displace another host.
- M114 previously returned only `ok`, without a usable coordinate report. Continuous firmware-position telemetry is therefore unavailable.
- TFT-originated movement/extrusion is not necessarily visible on the MKS TCP command stream. State changes are shown only when a telemetry signal supports them.
- M20 root listing is available. Reading file contents is not established. Upload behavior is separately gated and unverified on the installed firmware.
- `TFT28/32` is an MKS module/display-family identifier, not a reliable full firmware release string.

Do not assume that behavior on a different Neptune model, board revision, Wi-Fi module, or firmware binary will match.
