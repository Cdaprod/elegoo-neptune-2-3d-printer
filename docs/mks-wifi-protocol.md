# MKS WiFi protocol findings

## Evidence labels

- **Source-confirmed:** present in the official Makerbase `MKS-WIFI` firmware source.
- **Package-verified:** inspected in the published `mkswifi==0.1.0` package and its metadata.
- **User-observed:** supplied from prior network testing; not repeated by this implementation.
- **Mock-tested:** verified against local loopback test servers only.
- **Live-verified:** reserved for a response received from the current printer address.
- **Unknown:** not established by the source or a live response.

## Transport and framing

- **Source-confirmed:** the ESP8266 firmware creates a raw `WiFiServer` on TCP port
  8080. TCP input is split at CR and LF boundaries into text G-code lines.
- **Package-verified:** the client writes one ASCII command per line terminated by
  `\r\n`, reads text lines and parses replies. No TCP handshake beyond ordinary
  TCP connection setup is used; no binary preamble or checksum is used on the
  host-to-ESP TCP stream.
- **Source-confirmed:** ESP8266-to-Robin MCU UART traffic is a separate binary
  protocol. The firmware source builds frames with `0xA5` header, type byte,
  two-byte little-endian data length, payload, and `0xFC` tail. The inspected
  frame builder contains no checksum field. This UART framing is not sent by the
  Python client.
- **Source-confirmed:** the stock source sets `MAX_SRV_CLIENTS` to 1; on an
  incoming connection it stops an already-connected client before storing the
  new one. A telemetry probe can interrupt a slicer or another host, so the CLI
  requires `--confirm-single-client-risk`.
- **Source-confirmed:** unhandled G-code is packaged to the MCU over UART; the
  WiFi firmware itself directly synthesizes responses for M997, M27, M992,
  M994, and M115. The M105 handler exists only in a commented block in the
  inspected firmware revision, so temperature behavior depends on forwarding
  and the connected printer firmware.
- **Unknown:** exact firmware revision installed on this Neptune 2 and whether
  its M105 response is forwarded in this build.

### Segmented motion execution

Makerbase's [public MKS-WIFI source](https://github.com/makerbase-mks/MKS-WIFI/blob/master/firmware_source/MksWifi/MksWifi.ino)
defines one active TCP client (`MAX_SRV_CLIENTS = 1`) and a fixed 10-slot
command ring queue (one slot is reserved to distinguish full from empty). It
splits incoming TCP bytes on CR/LF, packages G-code for the MCU UART bridge, and
relays printer replies. Observatory's existing executor sends one G-code line
and waits for `ok` before sending another, avoiding a multi-line burst.

That source does **not** prove the installed printer binary is identical or
provide an independent movement-complete signal for each G1 segment. A returned
`ok` is recorded as transport/controller acknowledgment, not completion of
physical motion. Marlin documents [`M400`](https://marlinfw.org/docs/gcode/M400.html)
as a wait for all planner moves to finish. The orbital macro uses an initial
M400 compatibility preflight (before homing) and inserts a barrier between
each motion phase. This is a documented synchronization mechanism, not a live
verification that the installed Elegoo firmware supports it. A rejected
preflight stops before movement; if later commands fail or the operator
cancels, the final G28 is not sent. Runtime travel settings and offsets remain
unavailable through MKS telemetry, so the macro uses only the nominal
220 × 220 × 250 mm printable volume and requires explicit clear-path confirmation.

## TFT28/32 identification and operator-prompt commands

The observed `TFT28/32` value is **not a Marlin firmware version**. In the
official Makerbase WiFi source, `M115` selects the returned `FIRMWARE_NAME`
from the WiFi firmware's `M3_TYPE` setting (`TFT24`, `TFT28`, `TFT32`, Robin,
and related board/display labels). It identifies the configured MKS WiFi
module/display family. It does not report the Elegoo firmware build date,
Marlin version, compile-time options, or prove which TFT hardware/firmware is
connected. The MKS WiFi source revision inspected also has its own firmware
version constant; that is separate from the `TFT28/32` response.

Generic Marlin's G-code documentation describes the following behavior, but
the exact Elegoo build flags and the physical TFT's handling remain unknown.
No commands in this table were sent during this investigation.

| Command | Generic Marlin behavior / build dependency | Exact Neptune 2 result |
| --- | --- | --- |
| `M117` | Set a short LCD status message. Whether it appears depends on Marlin display configuration; host notifications may require `HOST_STATUS_NOTIFICATIONS`. | Unknown. `TFT28/32` alone does not establish that the stock panel displays it. |
| `M0` / `M1` | Stop/wait for user interaction or an optional timeout. A working local “click to continue” depends on an attached display/input path; headless host behavior depends on host-action configuration. | Unknown. Not established from the WiFi identity. |
| `M600` | Filament-change routine requires `ADVANCED_PAUSE_FEATURE` and a compatible user interaction path; may involve heating, retraction, and moves. | Unknown; no evidence that Advanced Pause is compiled or that the TFT presents its prompt. |
| `M108` | Break/cancel a Marlin wait, including waits from M0/M1/M600 or heat waits; queued delivery may require `EMERGENCY_PARSER`. It is not a prompt command. | Unknown; never tested. |
| `M876` | Respond to a host prompt when `HOST_PROMPT_SUPPORT` is compiled; it is a host-to-firmware response, not a command that creates a TFT prompt. Handling while blocked may depend on emergency parsing. | Unknown/likely inapplicable to a standalone touchscreen without a compatible host-prompt exchange. |

The official MKS TFT family has a separate touchscreen firmware and serial
connection/protocol; that is distinct from the ESP8266's raw TCP G-code socket.
The available repository and the `M115` WiFi type string do not prove the
stock Elegoo touchscreen's exact model, firmware, serial wiring mode, or support
for Marlin host-prompt actions. Generic Marlin docs establish possible firmware
features, not that this printer implements them.

### Smallest supervised physical-screen check

After separate explicit authorization for a physical G-code test, the least
invasive display-text check is one `M117 OBSERVATORY TEST` sent through the
known local path while watching the physical screen. It does not heat, move, or
start a print, but it is still a G-code write and has not been sent here. It
only verifies display text, not an interactive prompt. The smallest useful
interactive-prompt check is then a short, supervised `M0` with a documented
timeout and a visible harmless message; keep a human at the panel to tap
continue and be prepared to stop the test locally. Do not use `M600` or `M108`
as discovery probes: M600 can invoke thermal/motion behavior, while M108 is a
wait-cancel mechanism rather than a prompt test. A failed M0 UI response can
leave command processing waiting until its timeout, so do not test it without
confirming the timeout syntax for the installed build and a local recovery
path. This procedure remains pending operator authorization.

## Existing Python package assessment

Inspected with an isolated `uv run --python 3.13 --with mkswifi==0.1.0` environment:

- Version `0.1.0`, `Requires-Python: >=3.12`, no runtime dependencies.
- Public exports include `MksWifiClient`, `MksWifiConnectionError`, typed status
  models, and response parsers.
- `MksWifiClient(host, port=8080, timeout=5.0)` exposes `get_status()` and
  `get_firmware()`. Both use native `asyncio.open_connection`, send fixed
  read-only query lists, use one absolute connection/read deadline, and close
  the writer in `finally`.
- `get_status()` sends `M105`, `M997`, `M27`, `M994`, and `M992`; `get_firmware()`
  sends only `M115`. The two calls make separate TCP connections.
- It raises `MksWifiConnectionError` when connection is refused, nothing arrives,
  or the deadline expires without a response. Partial/missing fields are returned
  as `None` when any response text did arrive.
- Package documentation says Wanhao D12/TFT24 is known working and similar MKS
  Robin printers are expected to work. Neptune 2 is not independently verified;
  keep the dependency pinned and validate against the user's printer.

## Source-confirmed telemetry commands

| Command | Source-reported data | Read-only | Notes |
| --- | --- | --- | --- |
| `M105` | Nozzle/bed temperatures | Yes | The MKS-specific responder is commented out in inspected source; forwarded behavior is build-dependent. |
| `M997` | Idle/printing/paused state | Yes | MKS firmware synthesizes this response. |
| `M27` | Print progress percentage | Yes | May remain 0 or stale on some firmware. |
| `M994` | Current file name and size | Yes | File size may be unreliable. |
| `M992` | Elapsed print time | Yes | MKS firmware synthesizes this response. |
| `M115` | `FIRMWARE_NAME:Robin` / `TFT24` etc. | Yes | Identifies the MKS/board type in inspected source, not necessarily full Marlin version. |

No arbitrary command endpoint exists in this integration. It never sends motion,
heat, fan, file-selection, print-control, upload, or configuration commands.

## Optional M114 position reporting

The Makerbase TCP source has no special `M114` implementation; its generic
line-command path forwards unknown-to-MKS commands to the controller and
relays responses. The installed Elegoo firmware's `M114` support is therefore
unknown until a separately authorized query is made. If enabled, the dashboard
uses the existing single poller and I/O lock, queries only while freshly idle,
and waits at least 10 seconds between requests by default. The capability is
off by default. Ordinary Marlin M114 reports planned position rather than
measured stepper position; optional real-time M114 support is not assumed.
See [motion telemetry notes](motion-telemetry.md).

## Touchscreen-originated activity and state reconciliation

The official [MKS-WIFI firmware source](https://github.com/makerbase-mks/MKS-WIFI/blob/master/firmware_source/MksWifi/MksWifi.ino)
periodically requests M27, M992, M994, M991, M997, and (in the inspected
revision) M115 over its controller link. It does not define a TCP event feed
that relays every touchscreen G-code command. Makerbase TFT firmware has a
separate controller serial path; the available [MKS TFT source](https://github.com/makerbase-mks/MKS-TFT35-Firmware/blob/master/User/uart_model/mks_tft_function.c)
shows touchscreen actions being queued to the controller over that path. This
is evidence about the MKS architecture, not proof of the exact Elegoo TFT build
installed on this printer.

The Observatory records changes visible in its existing telemetry: current
printer state, active filename, and hotend/bed temperature targets. A target
change correlated with a recent Observatory command is shown as a firmware
state observation. An unmatched target change is shown as an external state
change with source unknown; it could be from the TFT, another client, or another
firmware path. Exact TFT commands are not captured.

The current MKS status model carries no X/Y/Z/E position, fan state, stepper
activity, or extruder movement field. M114's previously observed response was
only `ok`, without coordinates. Therefore the TFT-reported Z+10 movement and
filament extrusion/retraction cannot be detected or reconstructed through the
currently available telemetry. A camera, firmware event/position instrumentation,
or another independent sensor channel would be required for that detail. The
Motion dashboard leaves these signals unavailable rather than inferring them
from heat, idle status, or operator narration.

## SD card and `elegoo.txt`

- Standard Marlin documents `M20` as a read-only SD directory listing. A single
  live `M20` request to this Neptune 2 returned a complete listing bounded by
  `Begin file list` and `End file list`.
- Makerbase source includes code that maps an HTTP file-list handler to
  `M20 1:<path>`, but registration of the OctoPrint-style `/api/files` handlers
  is inside a disabled `#if 0` block in the inspected source. User-observed
  `GET /api/files/sdcard` returning 404 is consistent with that disabled route.
- The source/file-list behavior is for listing files, not reading arbitrary file
  contents. No documented read-only file-content API for `elegoo.txt` was found.
  Listing support must not be treated as download support.
- The inspected Makerbase source registers a raw-body `POST /upload` handler
  with an `X-Filename` argument. It accepts transfers only while its internal
  printer state is idle and routes file fragments through the ESP-to-controller
  transfer buffer. Its HTTP success branch accepts either `upload_success` or
  `rcv_end_flag`; therefore HTTP 200 alone is not proof that all bytes were
  stored. The handler exposes no content hash or read-back verification. This
  route is source-confirmed but untested on the installed binary; Observatory
  keeps upload off by default and does not retry ambiguous transfers.
- The live root listing contained 18 entries and did not report `elegoo.txt`.
  This only establishes that the name was absent from the returned root
  listing at probe time; it does not rule out nested paths or card changes.
- No source-confirmed or live-verified remote file-content download method was
  found. In the inspected Makerbase source revision, `FILE_DOWNLOAD_PORT` is
  only a constant (11188); no listener is created on that port. The active
  `/upload` handler and the `TRANSFER_GET_FILE` fragment state participate in
  the upload-to-controller path, not a read-back API for files already on the
  SD card. The OctoPrint-style `/api/files` routes that could provide file
  operations are inside `#if 0`. Directory listing therefore does not imply
  arbitrary file reads. For previews without removing the card, index the
  original OrcaSlicer exports from a read-only Mac folder or use Observatory's
  existing one-file thumbnail import. These local previews are matched by
  filename only and do not prove the printer-side file bytes are identical.
