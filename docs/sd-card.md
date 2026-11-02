# SD-card workflow

The printer's microSD card can remain installed. Neptune Observatory issues a read-only root listing request through the MKS WiFi client and presents the returned directory entries. A root listing does not imply that the app can read arbitrary file contents.

## Current behavior

- **List/refresh:** supported by the observed M20 response path.
- **Read/download a G-code file:** not established by the installed interface; use OrcaSlicer or inspect the card directly when file contents are needed.
- **Select/start/pause/resume/cancel:** fixed dashboard operations are implemented with authentication and runtime state checks. Exact behavior remains firmware-dependent; a returned acknowledgment is recorded separately from print progress/completion.
- **Upload:** a separate source-documented route exists but is disabled by default and not validated against the installed firmware. Do not enable it for routine use until the saved file is verified, including bytes/integrity and overwrite behavior.

## Safe workflow

1. Slice with the intended Neptune 2 profile in OrcaSlicer.
2. Transfer the G-code using the established card workflow while upload remains unverified.
3. Refresh the Observatory SD listing and select the exact file.
4. Review the dashboard's available file/state details and confirm the print action deliberately.
5. Watch printer telemetry and physical printer behavior; keep access to the printer's controls and power switch.

Print progress can be used to map a matching G-code path as an estimate. It is not a live position sensor. If the MKS interface does not expose a matching filename or progress, the app must not claim synchronized toolhead position.
