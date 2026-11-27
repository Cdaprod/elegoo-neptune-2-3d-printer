# Experimental Robin Nano RSD/1 firmware patch

**ISOLATED DEVELOPMENT BRANCH — NOT FLASH QUALIFIED.** Nothing here is invoked by
`make deploy`, and no flashing/uploading tool is included. Host changes are delivered
separately. Physical native SD retrieval remains unverified.

## Scope and pinned source

`marlin-rsd1.patch` applies only to Makerbase's Marlin source at
[`af5eb011d08f2a2e19809e38470f678958530b29`](https://github.com/makerbase-mks/Mks-Robin-Nano-Marlin2.0-Firmware/tree/af5eb011d08f2a2e19809e38470f678958530b29).
It adds a private M794 handler with a separately advertised RSD/1 capability, an
independent `SdFile` opened only with `O_READ`, media mutation/expiry hooks and a
WiFi dispatch guard. It never selects, executes, writes or removes an SD file.
Read response data uses a fixed 192-byte stack buffer; no whole-file MCU buffering.
STAT, OPEN and READ use existing FAT abstractions, not another filesystem driver.

The patch also changes the upstream **compile-only test configuration** from its
V3 default to V1.2/F103VE, primary UART1, one extruder, standalone A4988-style
interfaces, FSMC TFT35, onboard SD and no USB-host volume. BLTouch/bilinear leveling
are disabled in this test configuration. These are NOT verified printer settings.
Do not substitute them for the user's calibrated configuration. In particular,
Elegoo's existing TFT UI, screen assets, touch calibration, extruder steps, driver
polarity and thermistor definitions must be preserved when this small handler is
ported to the exact installed firmware fork. Porting that handler does not require
changing ESP8266 firmware or replacing the whole printer firmware ecosystem.

The installed firmware, MCU marking and bootloader have not been identified.
The public Elegoo distribution supplies factory binaries/configuration but does not
establish that this Makerbase checkout is the installed Elegoo source revision.
A successful build is not sufficient grounds to flash it.

## Reproducible build and handler tests

On a development machine with Git, Python 3.12+, C++17 compiler and network access:

```sh
python3 -m venv /tmp/rsd-build-tools
/tmp/rsd-build-tools/bin/pip install platformio==6.1.19
python3 firmware/robin-sd-read/build.py /tmp/rsd1-clean-build \
  --pio /tmp/rsd-build-tools/bin/pio
```

The destination must not already exist. The script fetches the exact source,
checks/applies the patch, compiles and tests the actual handler with deterministic
FAT/serial stubs, then builds `mks_robin_nano35`. It pins SOURCE_DATE_EPOCH to
1640995200, writes SHA-256 checksums into `.pio/build/mks_robin_nano35/rsd1-build.json`
and contains no upload target. The PlatformIO environment pins STM32 platform
12.1.x and GCC ARM 9.2.1 through the upstream configuration. Archive the manifest
and build log with any candidate image; do not infer binary identity from its name.

Recorded local clean build: macOS ARM64, PlatformIO 6.1.19, GCC ARM 9.2.1,
STM32 platform 12.1.1. Result: 57,696/65,536 bytes static RAM (88.0%),
325,804/524,288 bytes flash (62.1%). Approximately 7.7 KiB remains for dynamic/stack
use; real worst-case stack/heap headroom is **not measured**. The factory build's
RAM usage and feature compatibility are also unverified.

The native handler test exercises actual C++ handler logic for negotiation, LIST,
strict paths, bounded READ, CRC-32, session/sequence/offset checks, CLOSE/CANCEL,
modification, media removal, expiry and busy-state recovery. FAT, serial, time and
machine state are stubbed. The cross build checks the real Marlin APIs/compiler;
neither test validates real SDIO timing or any physical printer behavior.

## Protocol and host counterpart

See the host branch's `docs/native-robin-sd.md` for the wire specification, golden
packets, host parser/simulator, storage provenance, authenticated OctoPrint adapter
and hardware acceptance checklist. O0–O6 implement CAPABILITIES/LIST/STAT/OPEN/READ/
CLOSE/CANCEL. M794 is not an existing stock command and must never be probed without
its capability advertisement and explicit commissioning opt-in.

Root-level G-code files use the DOS alias for operations and an optional bounded
long display filename in LIST. Nested directories and arbitrary FAT characters
outside the strict alias allowlist are not implemented. This restriction is
explicit, not a claim to expose the complete generic filesystem. Interrupted reads
recover with a new session from offset zero; partial bytes are never published.

## Recovery gate before a separate flash approval

Do not flash while any item below remains unresolved:

- [ ] Photograph the actual motherboard silkscreen and MCU marking; distinguish
  MKS vs Elegoo ZNP and V1.2 vs V1.3. Do not rely on the product name alone.
- [ ] Record installed firmware identifier/version, TFT/display identity, WiFi build,
  current configuration and calibrated motion/extrusion/thermal values.
- [ ] Identify bootloader implementation, flash offset, encrypted image format and
  required update filename. The Makerbase test image is `Robin_nano35.bin`, whereas
  the Elegoo distribution uses `elegoo.bin`; renaming is NOT proof of compatibility.
- [ ] Obtain exact compatible factory recovery firmware/config/fonts/assets and
  verify independent checksums. Preserve copies outside the removable SD card.
- [ ] Compare the candidate's complete pin map against the actual board schematic:
  LCD FSMC, touchscreen, SDIO/detect, thermistors, heaters, fans, endstops, step/dir/
  enable, driver mode and polarity. Check single-extruder Neptune 2 calibration.
- [ ] Preserve the existing bootloader. If SD boot recovery fails, document the
  board-specific SWD access and compatible bootloader restoration with a qualified
  operator; this project does not guess an SWD image/address or invoke a programmer.
- [ ] Have an operator-reviewed restoration procedure and a separate explicit flash
  approval. No current approval is assumed or requested by this build script.

### Factory recovery candidate (not hardware-verified)

Published Elegoo source distribution:
[`NARUTOfzr/ZNP-Robin-Nano-V1.2-V1.3@bd7ec78efce29f8002698546875eedd6262d6522`](https://github.com/NARUTOfzr/ZNP-Robin-Nano-V1.2-V1.3/tree/bd7ec78efce29f8002698546875eedd6262d6522).
Its V1.2 candidate is `ZNP_Robin_Nano_V1.2 Firmware/V1.0.4/elegoo.bin`:
SHA-256 `9c11ba1782a7f66a9650bcab5014db9355d8d81770a93b3b931e35d864327d84`.
Matching Neptune 2 configuration is `Configuration files-V1.0.4/Neptune 2/elegoo.txt`;
font/image resources are under `Pic&font`. The vendor warns the image is V1.2-only.
This checksum identifies the downloaded file; it does NOT certify compatibility
with the installed bootloader or prove recovery has been tested.

Conditional restoration procedure after the gate and operator approval:

1. Stop normal use, safely power down, preserve the original SD contents and all
   calibrated configuration. Prepare a separate FAT32 recovery card according to
   the exact confirmed vendor bootloader procedure; never overwrite print files.
2. Put only the matching vendor recovery image and required matching configuration/
   font/image assets on that recovery card. Verify SHA-256 before and after copying.
3. Follow that board/bootloader's documented power-on update process. Observe the
   update result and expected vendor rename/status; do not interrupt power. The
   exact installed bootloader procedure is still pending identification, so this
   step must not be executed from this document alone.
4. Remove the update image when the confirmed procedure says to, retain the recovery
   card, restore calibrated settings through the supported vendor mechanism, then
   perform display/touch/SD and ambient-temperature read-only verification.
5. Any actuator, heater or print regression check needs its own explicit approval.
   Failed boot recovery requires the previously reviewed SWD recovery path; do not
   improvise flashing another board revision's bootloader.

## Physical acceptance (not performed)

A known pre-existing file must be enumerated and read through OctoPrint-owned USB,
match an independently known original SHA-256 and render real deposited geometry
on the 1280×400 HMI. Confirm no motion, heating, print start/selection or SD mutation,
and retain evidence that existing firmware/TFT/controls remain operational. Include
cancellation, interruption/recovery, media removal and source-change checks when
safe and authorized. Until then this branch must remain isolated from production.

SHA-256 is accumulated independently by the firmware during sequential reads and
returned on CLOSE; the host compares its own streamed SHA-256 before publishing.
The fixed-state implementation is Brad Conte’s public-domain SHA-256 from
[B-Con/crypto-algorithms@cfbde48414baacf51fc7c74f275190881f037d32](https://github.com/B-Con/crypto-algorithms/tree/cfbde48414baacf51fc7c74f275190881f037d32),
with original attribution retained in the patch. It is used for file integrity,
not signatures, credentials or side-channel-sensitive cryptography.

Clean build manifest for the complete patch:

```json
{
  "experimental_only": true,
  "hardware_accepted": false,
  "upstream": "https://github.com/makerbase-mks/Mks-Robin-Nano-Marlin2.0-Firmware.git",
  "revision": "af5eb011d08f2a2e19809e38470f678958530b29",
  "source_date_epoch": "1640995200",
  "platformio": "PlatformIO Core, version 6.1.19",
  "patch_sha256": "46a9de07c52a3a68eea6abe20ed147ab4e1d53710b5a9b9dbb0651dcb8d42e2d",
  "artifacts": {
    "Robin_nano35.bin": "d158f2b264ff82289f7ae153bdad50a7d90049e771b53da6ed87f0a9c1f4f25f",
    "firmware.bin": "75c0510caa10e75a3b55e0b4e3d3f34067294766a7e510557ab843cb2ac168a0"
  }
}
```
