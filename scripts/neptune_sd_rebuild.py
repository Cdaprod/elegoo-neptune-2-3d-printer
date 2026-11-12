#!/usr/bin/env python3
"""Recoverable Neptune 2 SD card rebuild for macOS (no command-line flags).

Run: python3 scripts/neptune_sd_rebuild.py

Discovers genuine removable SD media, backs up and SHA-256 verifies files,
flattens G-code to unique 8.3 *.GCO names, formats FAT32/MBR using the original
volume label, restores and verifies files, then ejects. Interrupted runs resume
from a verified manifest. All backups are retained.

A destructive format always requires explicit confirmation: ERASE diskN.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MIN_SIZE = 1_000_000_000
MAX_SIZE = 128_000_000_000
BACKUP_ROOT = Path.home() / "Neptune2-SD-Backups"
MACOS_FILES = {
    ".DS_Store",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    ".TemporaryItems",
    ".VolumeIcon.icns",
}
PRINT_SUFFIXES = {".gcode", ".gco", ".g"}
MANIFEST_VERSION = 2


class RebuildError(RuntimeError):
    """A safety condition or a recoverable operation failure."""


def say(message: str) -> None:
    print(message, flush=True)


def command(*argv: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(argv), capture_output=True, check=True)


def plist_command(*argv: str) -> dict[str, Any]:
    try:
        value = plistlib.loads(command(*argv).stdout)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise RebuildError(
            f"Could not query macOS disk information: {' '.join(argv)}"
        ) from exc
    if not isinstance(value, dict):
        raise RebuildError("Unexpected diskutil plist response")
    return value


def is_sd_card(info: dict[str, Any]) -> bool:
    """An SD reader may report Internal=True while its media is removable."""
    size = info.get("TotalSize") or info.get("Size")
    return (
        info.get("WholeDisk") is True
        and info.get("RemovableMedia") is True
        and info.get("BusProtocol") == "Secure Digital"
        and info.get("VirtualOrPhysical") != "Virtual"
        and isinstance(size, int)
        and not isinstance(size, bool)
        and MIN_SIZE <= size <= MAX_SIZE
        and bool(
            re.fullmatch(
                r"disk[0-9]+", str(info.get("DeviceIdentifier", ""))
            )
        )
    )


def fingerprint(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "bus": info.get("BusProtocol"),
        "capacity_bytes": info.get("TotalSize") or info.get("Size"),
        "media_name": (
            info.get("MediaName") or info.get("DeviceMediaName") or "unknown"
        ),
        "removable": info.get("RemovableMedia"),
        "whole": info.get("WholeDisk"),
    }


def disks() -> list[dict[str, Any]]:
    listing = plist_command("diskutil", "list", "-plist")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in listing.get("AllDisksAndPartitions", []):
        name = (
            str(item.get("DeviceIdentifier", ""))
            if isinstance(item, dict)
            else ""
        )
        if not re.fullmatch(r"disk[0-9]+", name) or name in seen:
            continue
        seen.add(name)
        try:
            info = plist_command(
                "diskutil", "info", "-plist", f"/dev/{name}"
            )
        except RebuildError:
            continue
        if is_sd_card(info):
            candidates.append(info)
    return sorted(
        candidates, key=lambda item: int(item["DeviceIdentifier"][4:])
    )


def choose_card(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        raise RebuildError(
            "No removable Secure Digital card (1–128 GB) found. "
            "Insert it and retry."
        )
    if len(candidates) == 1:
        return candidates[0]
    say("Multiple removable Secure Digital cards found:")
    for idx, info in enumerate(candidates, 1):
        identity = fingerprint(info)
        say(
            f"  {idx}. /dev/{info['DeviceIdentifier']} "
            f"— {identity['media_name']}, "
            f"{identity['capacity_bytes'] / 1e9:.1f} GB"
        )
    response = input("Card number (Enter cancels): ").strip()
    if (
        not response.isdecimal()
        or not 1 <= int(response) <= len(candidates)
    ):
        raise RebuildError("No card selected")
    return candidates[int(response) - 1]


def volume_for_disk(info: dict[str, Any]) -> tuple[Path, str]:
    disk = str(info["DeviceIdentifier"])

    def mounted_volumes() -> list[tuple[Path, str]]:
        listing = plist_command(
            "diskutil", "list", "-plist", f"/dev/{disk}"
        )
        volumes: list[tuple[Path, str]] = []
        for whole in listing.get("AllDisksAndPartitions", []):
            if (
                not isinstance(whole, dict)
                or whole.get("DeviceIdentifier") != disk
            ):
                continue
            for part in whole.get("Partitions", []):
                if isinstance(part, dict) and part.get("MountPoint"):
                    volumes.append(
                        (
                            Path(part["MountPoint"]),
                            str(part["DeviceIdentifier"]),
                        )
                    )
        return volumes

    volumes = mounted_volumes()
    if not volumes:
        try:
            command("diskutil", "mountDisk", f"/dev/{disk}")
        except subprocess.CalledProcessError as exc:
            raise RebuildError(
                f"Could not mount SD /dev/{disk}; backup remains intact"
            ) from exc
        volumes = mounted_volumes()
    if len(volumes) != 1:
        raise RebuildError(
            f"Expected one mounted SD volume, "
            f"found {len(volumes)} on /dev/{disk}"
        )
    mount, partition = volumes[0]
    if not mount.is_dir() or not mount.is_absolute():
        raise RebuildError("The SD volume is not mounted")
    if not os.path.ismount(mount):
        raise RebuildError(f"Refusing unverified mount point: {mount}")
    pinfo = plist_command(
        "diskutil", "info", "-plist", f"/dev/{partition}"
    )
    label = (
        pinfo.get("VolumeName")
        or pinfo.get("VolumeLabel")
        or mount.name
    )
    return mount, str(label)


def fat_label(original: str) -> str:
    if (
        not original
        or len(original) > 11
        or not re.fullmatch(r"[A-Za-z0-9 _-]+", original)
    ):
        raise RebuildError(
            f"Original label {original!r} cannot be preserved as FAT32; "
            "no erase performed"
        )
    return original


def ignored(relative: Path) -> bool:
    return any(
        part in MACOS_FILES or part.startswith("._")
        for part in relative.parts
    )


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirs:
            path = base / name
            if path.is_symlink():
                raise RebuildError(
                    f"Cannot safely preserve symlink directory: {path}"
                )
        dirs[:] = sorted(
            name
            for name in dirs
            if not ignored((base / name).relative_to(root))
        )
        for name in sorted(names):
            path = base / name
            rel = path.relative_to(root)
            if ignored(rel):
                continue
            if path.is_symlink() or not path.is_file():
                raise RebuildError(
                    f"Refusing symlink or unusual file: {path}"
                )
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                for chunk in iter(
                    lambda: handle.read(1024 * 1024), b""
                ):
                    size += len(chunk)
                    digest.update(chunk)
            found[rel.as_posix()] = {
                "size": size,
                "sha256": digest.hexdigest(),
            }
    return found


def normalize_name(stem: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", stem.upper()) or "PRINT"


def safe_nonprint_path(relative: str) -> None:
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{x}" for x in range(1, 10)),
        *(f"LPT{x}" for x in range(1, 10)),
    }
    for component in Path(relative).parts:
        if (
            component in {".", ".."}
            or len(component.encode("utf-8")) > 240
            or re.search(r'[<>:"\\|?*\x00-\x1f]', component)
            or component.endswith(".")
            or component.endswith(" ")
            or component.upper().split(".")[0] in reserved
        ):
            raise RebuildError(
                f"File cannot be represented on FAT32: {relative}"
            )


def plan_names(files: dict[str, Any]) -> dict[str, str]:
    """Flatten print files to unique root-level 8.3 GCO names."""
    reserved: set[str] = set()
    for original in files:
        if Path(original).suffix.lower() not in PRINT_SUFFIXES:
            safe_nonprint_path(original)
            reserved.add(original.casefold())
            reserved.add(Path(original).parts[0].casefold())
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for original in sorted(
        files, key=lambda name: (name.casefold(), name)
    ):
        if Path(original).suffix.lower() not in PRINT_SUFFIXES:
            continue
        stem = normalize_name(Path(original).stem)
        for number in range(1_000_000):
            suffix = "" if number == 0 else str(number)
            candidate = (
                stem[: 8 - len(suffix)] + suffix + ".GCO"
            )
            folded = candidate.casefold()
            if folded not in reserved and folded not in used:
                mapping[original] = candidate
                used.add(folded)
                break
        else:
            raise RebuildError("Exhausted 8.3 G-code filenames")
    expected = {
        mapping.get(source, source): details
        for source, details in files.items()
    }
    if len({path.casefold() for path in expected}) != len(expected):
        raise RebuildError("Case-insensitive target filename collision")
    return mapping


def expected_files(
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        manifest["name_map"].get(path, path): details
        for path, details in manifest["source_files"].items()
    }


def save_manifest(
    folder: Path, manifest: dict[str, Any]
) -> None:
    path = folder / "manifest.json"
    temp = folder / "manifest.json.new"
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def load_manifest(folder: Path) -> dict[str, Any]:
    data = json.loads(
        (folder / "manifest.json").read_text(encoding="utf-8")
    )
    if (
        data.get("version") != MANIFEST_VERSION
        or not isinstance(data.get("source_files"), dict)
    ):
        raise RebuildError(
            f"Unsupported or damaged backup manifest: {folder}"
        )
    return data


def verified_backup(
    folder: Path, manifest: dict[str, Any]
) -> bool:
    original = folder / "original"
    return (
        original.is_dir()
        and inventory(original) == manifest["source_files"]
    )


def pending_backup(
    info: dict[str, Any], base: Path
) -> tuple[Path, dict[str, Any]] | None:
    if not base.is_dir():
        return None
    matching: list[tuple[Path, dict[str, Any]]] = []
    for folder in sorted(
        base.glob("neptune-sd-*"), reverse=True
    ):
        if not (folder / "manifest.json").is_file():
            continue
        try:
            manifest = load_manifest(folder)
            if (
                manifest.get("stage") == "complete"
                or manifest.get("fingerprint") != fingerprint(info)
            ):
                continue
            if verified_backup(folder, manifest):
                matching.append((folder, manifest))
        except (RebuildError, OSError, ValueError, KeyError):
            continue
    if not matching:
        return None
    if len(matching) > 1:
        raise RebuildError(
            "Multiple pending verified backups match this card. "
            "Inspect them; none will be overwritten."
        )
    return matching[0]


def perform_backup(
    mount: Path,
    label: str,
    info: dict[str, Any],
    base: Path,
) -> tuple[Path, dict[str, Any]]:
    source_files = inventory(mount)
    if not source_files:
        raise RebuildError(
            "No user files on card and no recovery backup found; "
            "refusing pointless erase"
        )
    plan = plan_names(source_files)
    required = sum(
        item["size"] for item in source_files.values()
    )
    if shutil.disk_usage(base).free < (
        required + 256 * 1024 * 1024
    ):
        raise RebuildError(
            "Insufficient Mac space for verified backup "
            "plus 256 MiB safety margin"
        )
    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    folder = base / f"neptune-sd-{stamp}"
    folder.mkdir(mode=0o700)
    say(f"Backing up to {folder} ...")
    try:
        shutil.copytree(
            mount, folder / "original", symlinks=True
        )
        if (
            inventory(mount) != source_files
            or inventory(folder / "original") != source_files
        ):
            raise RebuildError(
                "Backup verification failed or source changed; "
                "CARD NOT ERASED"
            )
        manifest = {
            "version": MANIFEST_VERSION,
            "created_utc": (
                datetime.now(timezone.utc).isoformat()
            ),
            "stage": "backed_up",
            "source_disk": info["DeviceIdentifier"],
            "fingerprint": fingerprint(info),
            "original_label": fat_label(label),
            "source_files": source_files,
            "name_map": plan,
            "note": (
                "Original files retained. Restore flattens "
                "G-code files to 8.3 root names."
            ),
        }
        save_manifest(folder, manifest)
    except Exception:
        say(f"Backup retained for inspection at: {folder}")
        raise
    return folder, manifest


def recheck_card(
    disk: str, initial: dict[str, Any]
) -> None:
    current = plist_command(
        "diskutil", "info", "-plist", f"/dev/{disk}"
    )
    if (
        not is_sd_card(current)
        or fingerprint(current) != fingerprint(initial)
    ):
        raise RebuildError(
            "Physical card identity changed; refusing to write"
        )


def verify_fat32_mbr(info: dict[str, Any]) -> None:
    """Independently verify partition layout after diskutil eraseDisk."""
    disk = str(info["DeviceIdentifier"])
    listing = plist_command(
        "diskutil", "list", "-plist", f"/dev/{disk}"
    )
    items = [
        item
        for item in listing.get("AllDisksAndPartitions", [])
        if isinstance(item, dict)
        and item.get("DeviceIdentifier") == disk
    ]
    if len(items) != 1:
        raise RebuildError(
            "Could not verify destination partition layout"
        )
    whole = items[0]
    map_type = str(whole.get("Content", ""))
    if map_type != "FDisk_partition_scheme":
        raise RebuildError(
            f"Expected MBR; found {map_type!r}"
        )
    parts = [
        part
        for part in whole.get("Partitions", [])
        if isinstance(part, dict)
    ]
    if len(parts) != 1:
        raise RebuildError(
            "Expected one FAT32 partition after format"
        )
    part = parts[0]
    pinfo = plist_command(
        "diskutil",
        "info",
        "-plist",
        f"/dev/{part['DeviceIdentifier']}",
    )
    evidence = " ".join(
        str(value or "")
        for value in (
            part.get("Content"),
            pinfo.get("FilesystemName"),
            pinfo.get("FileSystemPersonality"),
            pinfo.get("Content"),
        )
    ).upper()
    if "FAT32" not in evidence and "FAT_32" not in evidence:
        raise RebuildError(
            f"FAT32 could not be verified: {evidence!r}"
        )


def confirm_erase(
    info: dict[str, Any],
    label: str,
    backup: Path,
) -> None:
    name = str(info["DeviceIdentifier"])
    identity = fingerprint(info)
    say(
        f"\nERASE TARGET: /dev/{name} "
        f"({identity['capacity_bytes'] / 1e9:.1f} GB)\n"
        f"  Media: {identity['media_name']}\n"
        f"  Restored name: {label}\n"
        f"  Verified backup: {backup}\n"
        f"  Format: FAT32 with MBR partition table"
    )
    token = f"ERASE {name}"
    if input(
        f"Type '{token}' to erase this physical SD card: "
    ).strip() != token:
        raise RebuildError(
            "Erase declined; verified backup retained"
        )


def card_files_are_expected(
    current: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return set(current).issubset(expected)


def restored(
    mount: Path,
    folder: Path,
    manifest: dict[str, Any],
) -> None:
    expected = expected_files(manifest)
    current = inventory(mount)
    if not card_files_are_expected(current, expected):
        extra = sorted(set(current) - set(expected))
        raise RebuildError(
            f"Unexpected files on card: {extra[:8]}"
        )
    for source, details in manifest["source_files"].items():
        target = manifest["name_map"].get(source, source)
        output = mount / target
        if current.get(target) == details:
            continue
        output.parent.mkdir(
            parents=True, exist_ok=True
        )
        shutil.copy2(folder / "original" / source, output)
    if inventory(mount) != expected:
        raise RebuildError(
            "SHA-256 restore verification failed; "
            "backup retained"
        )


def run_workflow(
    info: dict[str, Any],
    backup_root: Path = BACKUP_ROOT,
    *,
    confirm: Callable[
        [dict[str, Any], str, Path], None
    ] = confirm_erase,
    runner: Callable[
        ..., subprocess.CompletedProcess[bytes]
    ] = command,
    mount_reader: Callable[
        [dict[str, Any]], tuple[Path, str]
    ] = volume_for_disk,
    check_card: Callable[
        [str, dict[str, Any]], None
    ] = recheck_card,
    verify_layout: Callable[
        [dict[str, Any]], None
    ] = verify_fat32_mbr,
) -> Path:
    if not is_sd_card(info):
        raise RebuildError(
            "Selected device is not a verified "
            "physical removable SD card"
        )
    backup_root.mkdir(parents=True, exist_ok=True)
    disk = str(info["DeviceIdentifier"])
    mount, volume_label = mount_reader(info)
    if backup_root.resolve().is_relative_to(
        mount.resolve()
    ):
        raise RebuildError(
            "Backup location is on target SD card"
        )

    pending = pending_backup(info, backup_root)
    if pending:
        folder, manifest = pending
        if not verified_backup(folder, manifest):
            raise RebuildError(
                "Pending backup verification failed"
            )
        say(
            f"Found verified interrupted run: "
            f"{folder} (stage: {manifest['stage']})."
        )
    else:
        label = fat_label(volume_label)
        folder, manifest = perform_backup(
            mount, label, info, backup_root
        )

    expected = expected_files(manifest)
    label = manifest["original_label"]
    current = inventory(mount)

    if manifest["stage"] in {
        "formatted", "restoring", "verified"
    }:
        if label.casefold() != volume_label.casefold():
            raise RebuildError(
                "Destination volume name differs "
                "from saved label; refusing restore"
            )
        if not card_files_are_expected(
            current, expected
        ):
            raise RebuildError(
                "Unexpected data on card "
                "since formatting; refusing restore"
            )
    else:
        source_matches = (
            current == manifest["source_files"]
        )
        formatted_and_empty = (
            not current
            and volume_label.casefold() == label.casefold()
        )
        if not source_matches and not formatted_and_empty:
            raise RebuildError(
                "Card contents differ from verified backup; "
                "refusing to overwrite unknown data"
            )
        if source_matches:
            confirm(info, label, folder)
            check_card(disk, info)
            say("Formatting SD as FAT32 / MBR ...")
            try:
                runner(
                    "diskutil", "eraseDisk",
                    "FAT32", label, "MBRFormat",
                    f"/dev/{disk}",
                )
            except subprocess.CalledProcessError as exc:
                raise RebuildError(
                    "diskutil eraseDisk failed; backup preserved "
                    "and run is resumable"
                ) from exc

        manifest["stage"] = "formatted"
        save_manifest(folder, manifest)
        # Re-read the new mount: the old path can be stale.
        mount, observed_label = mount_reader(info)
        if observed_label.casefold() != label.casefold():
            raise RebuildError(
                f"Formatted label mismatch: "
                f"{observed_label!r}"
            )

    check_card(disk, info)
    verify_layout(info)
    if not card_files_are_expected(
        inventory(mount), expected
    ):
        raise RebuildError(
            "Unexpected destination files; refusing restore"
        )

    manifest["stage"] = "restoring"
    save_manifest(folder, manifest)
    say(f"Restoring and SHA-256 verifying {mount} ...")
    restored(mount, folder, manifest)

    _, observed_label = mount_reader(info)
    if observed_label.casefold() != label.casefold():
        raise RebuildError(
            "Volume label changed during restore"
        )
    manifest["stage"] = "verified"
    save_manifest(folder, manifest)
    runner("sync")
    try:
        runner(
            "diskutil", "eject", f"/dev/{disk}"
        )
    except subprocess.CalledProcessError as exc:
        raise RebuildError(
            "Files verified but eject failed; "
            "backup retained, eject manually"
        ) from exc

    manifest["stage"] = "complete"
    manifest["completed_utc"] = (
        datetime.now(timezone.utc).isoformat()
    )
    save_manifest(folder, manifest)
    say(
        "\nSUCCESS: FAT32/MBR card restored, "
        "SHA-256 verified and ejected.\n"
        f"Original volume name: {label}\n"
        f"G-code files: {len(manifest['name_map'])}\n"
        f"Backup retained: {folder}\n"
        "Test the printer before deleting the backup."
    )
    return folder


def main() -> int:
    if sys.platform != "darwin":
        raise RebuildError(
            "Requires macOS diskutil; no disks modified"
        )
    info = choose_card(disks())
    say(
        "Detected removable SD: "
        f"/dev/{info['DeviceIdentifier']}"
    )
    run_workflow(info)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        RebuildError,
        OSError,
        subprocess.CalledProcessError,
        KeyboardInterrupt,
    ) as exc:
        print(
            f"\nSTOPPED: {exc}\n"
            "Existing backups were not deleted.",
            file=sys.stderr,
        )
        sys.exit(1)
