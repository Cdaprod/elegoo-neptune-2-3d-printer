# Physical SD printing and recovery acceptance — 2026-11-12

## Scope and evidence

This record documents **operator-observed, physical printer behavior** on an
Elegoo Neptune 2. It does **not** claim a live Observatory deployment, remote
MKS upload, or OctoPrint-owned print start was tested.

Earlier, selecting known G-code from the printer SD card left the touchscreen
at 0% and apparently frozen. A card occasionally failed to publish a block
device in macOS, so intermittent media/reader trouble remains a separate
possible cause. Initial card discovery showed a built-in Secure Digital reader
reported by macOS as `Internal=True` even though `Removable=True`, media
protocol was Secure Digital, and the card was physical, nonvirtual and about
31 GB.

The successful recovery used the following stages:

1. Copy the mounted card to a local Mac backup, including hidden metadata.
2. SHA-256 compare readable original files with the backup *before* erasure.
3. Plan unique uppercase, root-level 8.3 `.GCO` filenames for 16 print files;
   preserve non-print printer files unchanged in the restore set.
4. Unmount the busy card (macOS `loginwindow` initially blocked a diskutil
   unmount), format a **single FAT32 partition with MBR**, keeping the original
   `NEPTUNE` label, and mount it again.
5. Restore and SHA-256 verify **18/18** intended user files: 16 renamed print
   files plus `elegoo_cfg.CUR` and `mks_pft.sys`. Original files remain in
   the retained Mac backup.
6. Reinsert the SD card, select a print on the physical touchscreen, and
   observe an active timer, rising temperatures, extrusion and deposited
   first-layer paths. The previous immediate 0% freeze did not recur in this
   observed session.

The operator subsequently cleaned material accumulated on the nozzle, cleaned
and re-leveled the bed and applied glue stick. Later photos showed substantially
better first-layer attachment and a partially printed part. No formal complete
multi-hour print, card endurance test or destructive media health test was
reported, and the root cause (filesystem metadata vs filename compatibility vs
intermittent card hardware) was not isolated.

## Implemented recovery tooling

Run the repository's tool on **macOS**, from the repository root:

```sh
python3 scripts/neptune_sd_rebuild.py
```

The script performs removable SD detection, backup verification, 8.3 naming
collision handling, explicit erase approval, FAT32/MBR verification, restore
verification and interrupted-run recovery. It does not send printer G-code.

Important operational safeguards:

- The built-in macOS reader is acceptable despite `Internal=True`, but the
  card must be identified as removable **physical Secure Digital media**, not a
  system disk, virtual image, USB mass-storage device or unknown media type.
- Never assume `/dev/disk6` will remain the device identifier after reinsertion.
- Close Finder windows and unmount normally if macOS reports an in-use SD
  volume; never kill `loginwindow` or disable SIP to enable an erase.
- The destructive operation still requires typing `ERASE diskN` exactly.
- Keep the retained backup until a complete intended print has succeeded.

## Acceptance and remaining work

| Capability | Evidence | Status |
| --- | --- | --- |
| Local physical SD read and print startup | Touchscreen timer, heat and material deposition | **Operator-observed** |
| SD recovery with FAT32/MBR and 8.3 names | 18 restored files matched SHA-256 after format | **Operator-observed** |
| SD recovery script regression suite | Hardware-free interrupted-format, restore, collision and media guards | **Automated tests** |
| Independent media endurance and reader reliability | No sustained read/write verification | **Unverified** |
| OctoPrint/Observatory HTTP SD-status reconciliation | Separate protocol implementation and mocked regressions | **Not validated by this SD print** |
| Remote MKS SD upload and content readback | No independent printer-side byte verification | **Unverified, gated off** |
| Remotely dispatched SD start/pause/resume/cancel | No operator-observed remote command lifecycle in this session | **Unverified** |
| Nozzle/bed control via OctoPrint | Heating shown for a touchscreen-started file only | **Unverified remotely** |

A successful print confirms that the firmware can execute a file from the
recovered card; it is not proof of native MKS upload integrity, live XYZ
position reporting, independent host progress reporting, or completion of a
command sent over WiFi. Those boundaries must remain explicit in the UI and
the feature-status ledger.
