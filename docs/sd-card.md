# SD-card workflow

The printer's microSD card can remain installed. Neptune Observatory issues a read-only root listing request through the MKS WiFi client and presents the returned directory entries. A root listing does not imply that the app can read arbitrary file contents.

## Current behavior

- **List/refresh:** supported by the observed M20 response path.
- **Read/download a G-code file:** not established by the installed interface; use OrcaSlicer or inspect the card directly when file contents are needed.
- **Select/start/pause/resume/cancel:** fixed dashboard operations are implemented with authentication and runtime state checks. Exact behavior remains firmware-dependent; a returned acknowledgment is recorded separately from print progress/completion.
- **First-layer-only print:** the shared live SD picker is used by the first-layer macro. M20 cannot inspect file contents, so a filename is not treated as proof. The operator must explicitly confirm that the selected G-code contains only the intended first layer. Observatory cannot interrupt an ordinary multi-layer file at layer one; a regular print runs to the file's natural end.
- **Upload:** Makerbase source contains a raw-body `/upload?X-Filename=...` route, but the installed-device transfer was not tested. The source handler can return HTTP 200 when its upload-success flag or its request-end flag is set, so HTTP acceptance alone does not prove the file was fully stored. Observatory keeps uploads disabled by default, blocks an existing filename, and does not retry an ambiguous transfer. Keep the SD-card workflow until a separately authorized test verifies a new filename in a fresh M20 listing and independently validates its contents.
- **Print preview thumbnails:** when you choose an OrcaSlicer G-code export in the SD Card page, Observatory reads a supported embedded PNG/JPEG thumbnail and stores only that small image in its persistent SQLite database. It does not retain or send the G-code during preview extraction. After the matching file appears in the M20 listing, its preview appears in the file row. G-code files copied to the card through another route cannot be previewed unless you also select the matching export in Observatory; M20 does not return file contents or thumbnails. Up to 100 previews are retained.
- **Automatic local index (recommended):** configure OrcaSlicer to export to a stable folder on the Mac and mount that folder into Observatory read-only. The SD card stays installed in the printer. Observatory reuses its embedded-thumbnail parser and stores only the image and selected header metadata in SQLite. A preview can be paired with a fresh M20 row by an exact, unique firmware-compatible filename. M20 confirms the name is present, but the cached local export is not proof that the printer-side bytes are identical. Cached previews remain visible when the export folder is offline and are marked as cached; filename collisions are never guessed.

## Rebuild a removable Neptune SD card on macOS

Insert the microSD card into the Mac, then run `make sd-rebuild` from the repository root (equivalent to `python3 scripts/neptune_sd_rebuild.py`). No command-line flags or disk numbers are required.

The script discovers a **whole removable Secure Digital device**, even when a built-in Mac SD reader reports `Internal: True`, and refuses ambiguous or unsafe media. It reads the existing volume label, backs up all card data to `~/Neptune2-SD-Backups`, verifies user-file SHA-256 hashes, and prepares collision-safe root-level `8.3` `.GCO` names for Neptune 2 printing. It preserves all originals in the backup and restores non-G-code files to their original paths.

**Before a destructive erase**, the script displays the exact target disk, capacity, original volume name, and verified backup folder, then requires typing `ERASE diskN`. It formats FAT32 with an MBR partition table, independently checks the resulting filesystem, restores and verifies files, and ejects the card. The original volume name is restored instead of being hardcoded.

**Automatic recovery:** if formatting, copying, or eject fails, the backup and manifest remain in place. Insert the same card and run the same `make sd-rebuild` command again. The script locates its verified pending backup and resumes from the recorded stage without command-line recovery flags. It refuses to proceed if the card contents, identity, label, or destination files contradict the saved backup; a potentially destructive situation always fails safely rather than guessing.

This is a local macOS workflow and does **not** send commands to the printer, OctoPrint, or Observatory. It does not promise recovery from physically damaged media, corrupted source files, unsupported original volume labels, or a different SD controller. Keep the Mac backup until the restored card has been tested on the Neptune 2. The rebuild script's regression tests run in GitHub's Validate workflow.

## Safe workflow

1. Slice with the intended Neptune 2 profile in OrcaSlicer.
2. Transfer the G-code using the established card workflow while upload remains unverified.
3. Refresh the Observatory SD listing and select the exact file.
4. Review the dashboard's available file/state details and confirm the print action deliberately.
5. Watch printer telemetry and physical printer behavior; keep access to the printer's controls and power switch.

For a first-layer test, prepare and inspect a G-code file containing only layer one, transfer it through the established SD-card workflow, refresh the listing, and choose **Print first-layer-only SD file**. The picker uses the exact filename from the current M20 response and the final confirmation asks you to attest that the selected file contains only one layer. The SD listing cannot independently verify that claim. This action starts the selected file to its natural end; it does not clip a full print at layer one.

### Index thumbnails from the SD volume

To keep the SD card in the printer, set OrcaSlicer’s G-code export destination to a stable folder on the Mac. Set `NEPTUNE_GCODE_SOURCE_PATH` in the local `.env` to that existing absolute folder, then start with the read-only overlay:

```sh
docker compose -f compose.yaml -f compose.neptune-volume.yaml up -d --build
```

Example `.env` entry:

```dotenv
NEPTUNE_GCODE_SOURCE_PATH=/Users/your-name/Documents/OrcaSlicer/exports
```

The folder must already exist; the Compose overlay refuses to create it and grants read-only access. After changing the setting, restart with the command above. Observatory rescans the folder when the SD listing is loaded/refreshed, retains cached thumbnails when the source is offline, and clearly labels them as local cache. It matches only exact case-insensitive firmware-compatible basenames; it does not guess short 8.3 aliases. Duplicate local basenames or duplicate M20 names are never linked. The legacy `NEPTUNE_GCODE_VOLUME_PATH` setting still works for a mounted volume, but no card removal is needed when Orca exports stay in the configured folder.

Alternatively, use the existing **Choose an OrcaSlicer export** file picker in the SD Card page to cache one export’s embedded thumbnail. This reads the selected file in the browser, retains only its thumbnail/metadata, and does not send or store its G-code.

Print progress can be used to map a matching G-code path as an estimate. It is not a live position sensor. If the MKS interface does not expose a matching filename or progress, the app must not claim synchronized toolhead position.
