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
V3 default to V1.2/F103VE, primary UART3 (retained upstream selection; the WiFi module owns USART1), one extruder, standalone A4988-style
interfaces, FSMC TFT35, onboard SD and no USB-host volume. BLTouch/bilinear leveling
are disabled in this test configuration. These are NOT verified printer settings.
Do not substitute them for the user's calibrated configuration. In particular,
Elegoo's existing TFT UI, screen assets, touch calibration, extruder steps, driver
polarity and thermistor definitions must be preserved when this small handler is
ported to the exact installed firmware fork. Porting that handler does not require
changing ESP8266 firmware or replacing the whole printer firmware ecosystem.

The operator identified the installed firmware as `Robin_nano_v2.1.6.b` through M115
and the board as MKS Robin Nano v1.2. The MCU marking and bootloader remain
unverified; a firmware identifier does not establish the PCB revision.
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

The USB primary retains upstream `SERIAL_PORT=3`; `wifiSerial_STM32.cpp`
constructs the WiFi interface on USART1. Do not assign the primary serial owner
to USART1 while that module is enabled. The installed board schematic and
USB bridge still require physical confirmation.

UART3 configuration cross build: 57,696 bytes static RAM and 325,804 bytes flash;
actual handler fixtures pass. The earlier UART1 candidate is superseded and must
not be used. The compile-only CI regenerates checksums for this patch.

## Operator-reported USB evidence — 2026-11-27

The operator reports enumeration and selection through `/dev/ttyUSB0`, with this
response from the installed `Robin_nano_v2.1.6.b` factory firmware (identified by
a subsequent M115 handoff):

```text
M23 DISPMNT01.gcode
File opened: DISPMNT01.gcode Size: 2113357
File selected
ok
M27
SD printing byte 0/2113357
ok
```

This establishes reported file selection and a 2,113,357-byte size. The `M27` value
is a print-file cursor, not file contents returned to the host. It does not verify
RSD/1 installation, a READ chunk, a source SHA-256 match or native 3D reconstruction.
The report does not authorize further printer commands or flashing. A selected
print-file handle may make the read extension's concurrency guard refuse a transfer;
that guard must not be bypassed, and no automatic deselection is performed.
Use the alias returned by RSD LIST for future reads; do not guess or truncate an
8.3 alias from the long display filename.

## Factory USB handoff reconciliation

No experimental firmware has been flashed. The operator verified CH340
`1a86:7523`, Linux `ch341`, `/dev/ttyUSB0`, 115200 8N1, M115/M503/M105/M114
and M21/M20/M20 L/M23/M27. `DISPMNT01.gcode` reports 2113357 bytes and cursor
zero; selection/size does not verify file bytes or printing. The host branch
preserves this transcript and exposes stock observations through the existing
exclusive OctoPrint owner. RSD/1 is separately capability-negotiated and disabled.

Makerbase factory-family source `MKS-Robin-Nano-Firmware` at
`b346a1f3b6d3296b8efee87e06c92da504b5c0c3`, `Src/wifi_module.cpp:1425`,
enqueues M105 from its internal WiFi handler. This is a plausible source of
unsolicited echo/temperature traffic with no external USB writer, not proof of
the installed binary's origin. Host tests require command-specific completion
with interleaved temperature traffic. `Src/usart.c` maps USART3 to PB10/PB11 and
USART1 to PA9/PA10. UART3 remains corrected in this reference patch; physical
USB-to-UART routing and the exact factory fork/config still require verification.
This compile-only image must not replace the factory firmware on that evidence.
