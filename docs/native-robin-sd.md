# Native Robin Nano SD retrieval (RSD/1)

## Current acceptance boundary

The host implementation can retrieve actual byte streams from the experimental
RSD/1 firmware through the existing OctoPrint USB connection, verify chunks, cache
content and reconstruct deposited-filament geometry. Automated firmware and host
fixtures demonstrate the pipeline. **The physical printer has not been flashed or
used for acceptance. Stock Elegoo/MKS firmware must not be reported as retrievable.**

Firmware work is isolated on [`experimental/robin-sd-read-v1`](https://github.com/Cdaprod/elegoo-neptune-2-3d-printer/tree/experimental/robin-sd-read-v1/firmware/robin-sd-read).
That patch builds against a pinned Makerbase Marlin revision; it is not a verified
replacement for the installed Elegoo firmware or its TFT configuration. Production
`make deploy` never builds, copies or flashes MCU/ESP firmware.

## Source investigation

These are source findings, not observations of the installed firmware:

* [MKS-WIFI `39b8053427dc89a5038a87f06f401ed87212bea0`](https://github.com/makerbase-mks/MKS-WIFI/blob/39b8053427dc89a5038a87f06f401ed87212bea0/firmware_source/MksWifi/MksWifi.ino):
  `/upload` registers an incoming upload handler; `do_transfer` in
  `TRANSFER_GET_FILE` pops the upload FIFO and packages fragments toward the MCU.
  `/api/files` route registration is inside `#if 0`. `FILE_DOWNLOAD_PORT` being
  defined does not implement an SD-to-host server. No supported arbitrary file
  download was found in this revision.
* [Makerbase Marlin `af5eb011d08f2a2e19809e38470f678958530b29`](https://github.com/makerbase-mks/Mks-Robin-Nano-Marlin2.0-Firmware/tree/af5eb011d08f2a2e19809e38470f678958530b29):
  `M20` enumerates files; `M23`/`M24` select/start printing. The extension uses none
  of these as a byte-reading mechanism. `CardReader::getroot()` and the existing
  `SdFile`/SdBaseFile abstraction permit an independent `O_READ` handle. The
  examined G-code dispatcher has no M794 handler. M993–M997 have unrelated,
  potentially destructive functions and are deliberately not reused.
* In the same Marlin revision, `wifi_module.cpp::package_to_wifi` uses a 256-byte
  text buffer; it is not a binary transport. RSD/1 uses only the primary USB UART,
  and its M794 handler is blocked in the stock WiFi dispatch path. No ESP8266
  firmware modification is required.
* [Elegoo distribution `bd7ec78efce29f8002698546875eedd6262d6522`](https://github.com/NARUTOfzr/ZNP-Robin-Nano-V1.2-V1.3/tree/bd7ec78efce29f8002698546875eedd6262d6522)
  distinguishes F103 V1.2 (64 KiB RAM, 0x7000 offset) from F407 V1.3. It contains
  model-specific factory configuration and recovery binaries. This does not prove
  the installed board/MCU/bootloader or establish recovery compatibility.

## Transport and protocol

`Observatory → authenticated OctoPrint Simple API → existing printer command queue
→ USB UART → STM32 SdFile(O_READ) → existing FAT/SD driver`.

There are no extra serial/TCP clients. The separate `neptune_sd_read` plugin leaves
the existing read-only terminal plugin unchanged. It requires OctoPrint CONTROL
permission for exchanges, MONITOR permission for capabilities, an observed
`Cap:NEPTUNE_SD_READ:1`, explicit plugin enablement, a recognized USB serial port
and exactly OPERATIONAL state. It never probes M794 on unadvertised stock firmware.
The firmware independently rejects printing, paused jobs, selected print files,
uploads, logging, queued planner movement, missing media and expired sessions.
State checks are repeated for every chunk, not just when the UI enables a button.

Wire command: `M794 V1 O<operation> Q<sequence> S<session> A<offset> B<count>`.
STAT/OPEN append `P` plus **three decimal digits per ASCII byte** of the root DOS
8.3 alias. Decimal encoding avoids Marlin interpreting hexadecimal A–F digits as
new parameters. The complete command is at most 95 bytes (MAX_CMD_SIZE is 96).
Operations O0–O6 are CAPABILITIES, LIST, STAT, OPEN, READ, CLOSE, CANCEL.

Golden OPEN: `M794 V1 O3 Q2 S0 A0 B0 P080065082084046071067079` (PART.GCO).
Golden capability response:

```text
RSD1 {"v":1,"op":"CAPABILITIES","q":1,"s":0,"chunk":192,"max_bytes":52428800,"hash":"sha256"}
```

Each reply is one newline-terminated `RSD1 ` plus bounded JSON (768 bytes max).
It contains integer `v`, `q`, `s` and the operation name in `op`. Rejections have
an `error` code. Duplicate fields, unsupported versions, malformed/oversized JSON,
wrong operations, sequences or sessions are errors. No unknown opcode is retried.

* CAPABILITIES returns `chunk=192`, `max_bytes=52428800` for version 1.
* LIST uses A as a root-directory cursor, one valid file per response, then
  `end:true`. `name` is the actionable DOS alias; optional `display_hex` preserves
  the long display filename without putting quotes/control bytes on the wire.
  Directories and non-G-code files are excluded; traversal is rejected.
* STAT returns `size` and `token`: media mutation epoch, FAT cluster, size and
  creation/write date/time. These are snapshot metadata, **not a content hash**.
* OPEN creates one explicit session, returns the snapshot and holds a separate
  read-only file. It never calls CardReader's print-file selection API.
* READ requires the session, increasing Q and the exact next byte offset A.
  B is 1–192 bytes. It returns `offset`, snapshot `token`, hex `data` and unsigned
  IEEE CRC-32 `crc`. The host checks every field and exact byte count.
* CLOSE requires complete sequential byte accounting and returns the firmware’s
  streaming SHA-256; the host compares it to its independently accumulated digest.
  CLOSE/CANCEL close the independent handle. Firmware also expires sessions after
  15 seconds without valid activity, or when media/machine state becomes unsafe.
  Mount/release/write-open/delete invalidate the mutation epoch.

The host re-STATs before publication, compares firmware and host SHA-256 over every byte,
fsyncs and atomically publishes the content-addressed file. The independently known original SHA-256
for hardware acceptance must be independently supplied; a locally computed digest
alone is not proof the physical source was read correctly. CRC is not authentication.
FAT timestamps have limited resolution; out-of-band edits that deliberately preserve
all metadata cannot be detected by metadata alone. No filename or snapshot token
is treated as a content identity.

Recovery intentionally starts a **new read from offset zero**, discarding partial
unverified data. It does not replay an ambiguous last command or resume using a
stale SD handle. Cancellation is bounded by the in-flight request (5-second plugin
reply deadline, 7-second HTTP deadline); unreachable handles expire in firmware.
A transfer has a one-hour host deadline. Large files can exceed that deadline on
115200-baud serial; failure is explicit and no partial asset is published.

## Assets, APIs and HMI

`NativeSdStorage` stores SHA-256 named blobs under `/data/native-sd/blobs`, in the
existing persistent Observatory data volume. Default blob quota is 256 MiB;
individual files are bounded to 50 MiB. Oldest cached content is evicted without
altering SD or OctoPrint files. Incomplete `.part` files are removed after errors,
cancellation and restart. Existing blobs are SHA-verified before serving. Identical
contents deduplicate; equal names with different bytes remain different assets.

The asset record tracks provider `printer_sd`, original display filename, DOS path,
size, snapshot metadata, SHA-256, retrieval time, cache location/availability,
transfer status, geometry/thumbnail availability, source verification time and
available print metadata. Unavailable modification times/print metadata remain null.
SD entries without bytes, verified SD caches, OctoPrint/local files and staged
manufacturing artifacts retain separate provenance. A local filename match never
suppresses an unrelated SD entry or proves the two contents equal. PR #35's local
and external-reader import remain supporting providers, not native SD retrieval.

Authenticated routes:

* GET `/api/native-sd`: live directory and verified cached assets, or explicit
  unavailable/unsupported status with cached assets retained.
* POST `/api/native-sd/transfers`, JSON `{"filename":"PART.GCO"}`: one transfer at
  a time, selected-file priority, no uncontrolled background bulk reads.
* GET / DELETE `/api/native-sd/transfers/{id}`: progress / cancellation.
* GET `/api/native-sd/assets/{sha256}/gcode|geometry|thumbnail`: verified cached
  bytes and the existing fabrication reconstruction/SVG pipeline.
* POST `/api/native-sd/assets/{sha256}/import-local`: optional collision-safe copy
  using PR #35's importer; requires HMI import enablement and never selects/prints.
* The existing `/api/printer/sdcard/capabilities` negotiates the configured bridge;
  legacy read-by-name serves only an explicitly identified verified cache copy.
  Unsupported stock configurations retain an honest unsupported response.

The existing Fabrication gallery adds storage filtering, source labels, retrieval,
progress/cancel and the existing interactive inspector. Only retrieved bytes get
3D badges/canvases. Print Bay's staged artifacts, explicit print confirmation,
readiness checks, cinematic studio, settings and desktop/mobile views are preserved.
The inspector cancels stale fetches and releases grid and WebGL context resources.

## Validation and preview artifacts

`tests/robin_sd_simulator.py` is deterministic and has no hardware I/O. The host
suite covers serialization, malformed frames, paths, CRC, session/sequence/offset
checks, media removal, modifications, interruption/restart, cancellation, duplicate
names, cache integrity, permission/state guards and real geometry reconstruction.
`firmware/robin-sd-read/test_handler.py` on the experimental branch compiles the
actual M794 code against stubbed serial/FAT dependencies, rather than testing a
second implementation. The STM32 build validates compiler/API compatibility only.

`node scripts/test-native-sd-hmi.cjs` uses simulated native API responses and actual
G-code fixture geometry at 1280×400. It checks progress, filtering, filename-only
entries, visible interactive canvases, six inspector lifecycles and overflow.
The authenticated backend test separately exercises the full simulated protocol →
atomic cache → real geometry API pipeline. These are not physical transport tests.
Rendered screenshots are `docs/screenshots/native-sd-*-1280x400.png`.

PR #36's latest failure was Controls navigation after a canvas already existed.
Local repetitions and the new lifecycle stress test retain the visible-canvas
assertion. No timeout has been increased and the navigation assertion is unchanged.
Do not describe an unreproduced intermittent failure as a proven root-cause fix.

## Deployment and physical commissioning

Normal host update, after the host PR has merged:

```sh
cd ~/elegoo-neptune-2-3d-printer
git pull --ff-only origin main
make deploy
```

Keep `NEPTUNE_NATIVE_SD_READ_ENABLED=false` until firmware and USB commissioning
are approved. The OctoPrint plugin also defaults to `enabled: false`. Updating the
host neither changes the configured MKS WiFi connection nor flashes the board.

**Before requesting flash approval**, complete the experimental branch recovery
checklist: exact board photo/revision, MCU marking, installed firmware identifier,
bootloader offset/filename, compatible factory binary/config/fonts, checksums,
backup and restoration procedure, and display/touch/thermistor/heater/stepper/SD pin
comparison. The factory recovery candidate is not yet verified on this machine.
No automatic flash command is provided by production deployment.

After separate operator-approved firmware commissioning, the optional
`integrations/octoprint/compose.usb.yaml` overlay exposes exactly one verified host
device to the existing OctoPrint container. Set
`COMPOSE_FILE=compose.yaml:integrations/octoprint/compose.usb.yaml`,
`OCTOPRINT_SERIAL_DEVICE=/dev/serial/by-id/<verified-printer-device>` in `.env`.
This reuses the existing USB overlay and preserves its device mapping. Verify
container device permissions. Run `make deploy`, then select `/dev/ttyUSB-printer` through
OctoPrint only after the printer is safely idle; the initializer preserves that
explicit selection. Existing WiFi settings and volumes remain intact. This is a
one-time transport commissioning step, not a second OctoPrint instance.

After an idle USB connection
owned by OctoPrint, enable plugin setting `plugins.neptune_sd_read.enabled`, then
`NEPTUNE_NATIVE_SD_READ_ENABLED=true` and `make deploy`. Reconnect/authentication
must use the existing persisted OctoPrint configuration and credentials. Never open
an extra USB console while OctoPrint owns the connection.

Hardware acceptance remains **not run**:

1. Operator identifies a pre-existing known SD G-code and independent original SHA-256.
2. HMI enumerates its DOS alias and display filename on the motherboard SD.
3. Start retrieval through the HMI; record firmware version, USB owner, transfer log,
   byte count, source token and final SHA-256.
4. Independently hash the cached download and compare to the original; save evidence.
5. Inspect the deposited-filament reconstruction and orbit/zoom it on the touchscreen.
6. Confirm no motion, heating, print selection/start, SD writes/deletes occurred.
7. Verify existing firmware/TFT/SD and operator controls remain available. Any test
   that actually moves/heats/prints requires its own explicit operator authorization.
8. Exercise cancellation, disconnect and SD removal only when safe and separately
   authorized; verify a clean new-session retry and unchanged source file.
