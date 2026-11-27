#!/usr/bin/env python3
"""Copy G-code from an explicitly mounted removable card to OctoPrint local storage.

Dry-run by default. No media formatting, printer connection, file selection or print start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from urllib.parse import quote, urlencode

from authorize_octoprint import NoRedirects, local_url
from setup_env import ENV, parse_env

MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000
SUFFIXES = {".gcode", ".gco", ".gc", ".g"}
SAFE_STEM = re.compile(r"[^A-Za-z0-9_-]+")


class ImportFailure(RuntimeError):
    """A bounded, actionable import error."""


def verify_mount(path: Path) -> Path:
    if path.is_symlink():
        raise ImportFailure("The selected mount path must not be a symbolic link")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise ImportFailure("The selected card is not accessible") from exc
    if root == Path("/") or not root.is_dir() or not os.path.ismount(root):
        raise ImportFailure("Select the card's actual mount point (not / or a normal folder)")
    return root


def detect_mounts() -> list[Path]:
    """Discover mounted removable USB media, excluding the Pi's OS and boot volumes.

    This is discovery, never mounting or formatting. Multiple cards require a choice.
    """
    if sys.platform != "linux":
        return []
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,TRAN,RM,MOUNTPOINTS,FSTYPE"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        devices = json.loads(result.stdout).get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    found = []

    def visit(node: dict, removable: bool = False) -> None:
        eligible = removable or node.get("rm") in (1, True, "1") or node.get("tran") == "usb"
        if node.get("type") == "part" and eligible:
            for mount in node.get("mountpoints") or []:
                if isinstance(mount, str) and (
                    mount.startswith("/media/") or mount.startswith("/run/media/")
                ):
                    path = Path(mount)
                    if path.is_dir() and os.path.ismount(path) and not path.is_symlink():
                        found.append(path)
        for child in node.get("children") or []:
            if isinstance(child, dict):
                visit(child, eligible)

    for device in devices:
        if isinstance(device, dict):
            visit(device)
    return sorted(set(found))


def candidates(root: Path):
    """Bounded recursive traversal without following symlinks."""
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if not name.startswith(".") and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(files):
            item = Path(directory) / name
            if (
                name.startswith(".")
                or item.suffix.lower() not in SUFFIXES
                or item.is_symlink()
                or not item.is_file()
            ):
                continue
            count += 1
            if count > MAX_FILES:
                raise ImportFailure("Card contains more than 2000 G-code files; split the import")
            yield item


def read_source(path: Path, root: Path) -> bytes:
    """Check the final path and descriptor; reject oversized or changing files."""
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise ImportFailure(f"File escaped the selected volume: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ImportFailure(f"Not a regular file: {path.name}")
        if before.st_size > MAX_BYTES:
            raise ImportFailure(f"G-code exceeds 50 MiB preview limit: {path.name}")
        data = source.read(MAX_BYTES + 1)
        after = os.fstat(source.fileno())
    if len(data) > MAX_BYTES:
        raise ImportFailure(f"G-code exceeds 50 MiB preview limit: {path.name}")
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ) or len(data) != before.st_size:
        raise ImportFailure(f"File changed during import: {path.name}")
    return data


def filename_for(path: Path, root: Path, data: bytes, existing: dict) -> tuple[str, bool]:
    """Prefer the original root name; content-address collisions without overwriting."""
    relative = path.relative_to(root)
    suffix = path.suffix.lower()
    stem = SAFE_STEM.sub("-", path.stem.encode("ascii", "ignore").decode()).strip("-_")[:64]
    stem = stem or "model"
    original = path.name
    safe_root = (
        len(relative.parts) == 1
        and original.isascii()
        and len(original) <= 100
        and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_. -]*", original) is not None
    )
    preferred = original if safe_root else stem + "-" + hashlib.sha256(
        relative.as_posix().encode("utf-8")
    ).hexdigest()[:10] + suffix
    fingerprint = hashlib.sha1(data).hexdigest()
    present = existing.get(preferred.casefold())
    if present is None:
        return preferred, False
    if present.get("hash") == fingerprint:
        return preferred, True
    alternate = stem[:48] + "-" + hashlib.sha256(data).hexdigest()[:16] + suffix
    alternate_present = existing.get(alternate.casefold())
    if alternate_present is not None:
        if alternate_present.get("hash") in (fingerprint, None):
            return alternate, True
        raise ImportFailure(f"Content-addressed name collision: {alternate}")
    return alternate, False


def multipart(name: str, data: bytes) -> tuple[str, bytes]:
    """Only the OctoPrint local-file endpoint receives bytes; never a print command."""
    boundary = "neptune-" + os.urandom(18).hex()
    parts = [
        ('select', 'false'),
        ('print', 'false'),
    ]
    body = b"".join(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode()
            for field, value in parts
        )
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        'Content-Type: application/octet-stream\r\n\r\n'
    ).encode() + data + b"\r\n"
    return boundary, body + f'--{boundary}--\r\n'.encode()


def local_files(opener, base: str, key: str) -> dict:
    request = Request(base + "/api/files/local", headers={"X-Api-Key": key})
    with opener.open(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ImportFailure("OctoPrint returned an invalid local-file listing")
    result = {}
    for item in payload["files"]:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            result[item["name"].casefold()] = item
    return result


def upload(opener, base: str, key: str, name: str, data: bytes) -> None:
    boundary, body = multipart(name, data)
    request = Request(
        base + "/api/files/local",
        data=body,
        headers={"X-Api-Key": key, "Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    with opener.open(request, timeout=120) as response:
        if response.status not in (200, 201):
            raise ImportFailure(f"OctoPrint rejected upload of {name} (HTTP {response.status})")


def import_card(mount: Path, apply: bool = False, opener=None, env: dict | None = None) -> dict:
    root = verify_mount(mount)
    existing = {}
    base = ""
    key = ""
    if apply:
        if env is None:
            if not ENV.is_file():
                raise ImportFailure("Missing root .env; run make setup and authorize-octoprint")
            env = parse_env(ENV.read_text(encoding="utf-8"))
        key = (env or {}).get("OCTOPRINT_API_KEY", "")
        if not key:
            raise ImportFailure("OctoPrint API key missing; run make authorize-octoprint")
        base = local_url((env or {}).get("OCTOPRINT_HOST_PORT", "5001"))
        opener = opener or build_opener(NoRedirects())
        existing = local_files(opener, base, key)
    seen = {}
    summary = {"discovered": 0, "imported": 0, "skipped": 0, "dry_run": not apply}
    for path in candidates(root):
        summary["discovered"] += 1
        try:
            data = read_source(path, root)
        except ImportFailure as exc:
            print("SKIP", path.name, "-", exc)
            summary["skipped"] += 1
            continue
        # Track names within this card even on dry runs to disambiguate duplicate basenames.
        target, duplicate = filename_for(path, root, data, {**existing, **seen})
        if duplicate:
            print("SKIP already stored:", target)
            summary["skipped"] += 1
            continue
        print(("IMPORT" if apply else "DRY RUN") + ":", path.relative_to(root), "=>", target, len(data), "bytes")
        if apply:
            try:
                upload(opener, base, key, target, data)
            except (HTTPError, URLError, OSError) as exc:
                raise ImportFailure(f"OctoPrint upload failed for {target}: {exc}") from exc
            summary["imported"] += 1
        seen[target.casefold()] = {"name": target, "hash": hashlib.sha1(data).hexdigest()}
    print(json.dumps(summary, sort_keys=True))
    print("No print selected or started. Refresh the HMI Fabrication gallery.")
    return summary


def archive_card(mount: Path, apply=False, opener=None, env=None):
    """Reuse mounted-card discovery; stream originals to the unified archive, never OctoPrint."""
    root = verify_mount(mount).resolve()
    env = env if env is not None else parse_env(ENV.read_text()) if ENV.exists() else {}
    token = env.get("NEPTUNE_OBSERVATORY_TOKEN", "")
    if apply and not token:
        raise ImportFailure("Existing Observatory token is required for archive ingestion")
    opener = opener or build_opener(NoRedirects())
    result = {"discovered": 0, "archived": 0, "dry_run": not apply}
    for path in candidates(root):
        if not path.resolve(strict=True).is_relative_to(root):
            raise ImportFailure("Source escaped the selected removable volume")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
                raise ImportFailure("Source must be a nonempty bounded regular G-code file")
            hasher = hashlib.sha256()
            hashed = 0
            while block := source.read(65536):
                hashed += len(block)
                if hashed > MAX_BYTES:
                    raise ImportFailure("Source grew beyond ingestion limit")
                hasher.update(block)
            digest = hasher.hexdigest()
            if hashed != before.st_size:
                raise ImportFailure("Source changed during fingerprinting")
            source.seek(0)
            result["discovered"] += 1
            if apply:
                request = Request("http://127.0.0.1:8000/api/fabrication/ingest?" +
                                  urlencode({"filename": path.name}), data=source,
                                  headers={"Authorization": "Bearer " + token,
                                           "Content-Type": "application/octet-stream",
                                           "Content-Length": str(before.st_size),
                                           "X-Artifact-Provider": "removable_media",
                                           "X-Content-SHA256": digest,
                                           "X-Artifact-Source": "removable:" + quote(str(path))},
                                  method="POST")
                with opener.open(request, timeout=120) as response:
                    receipt = json.load(response)
                if receipt.get("sha256") != digest or not receipt.get("archived"):
                    raise ImportFailure("Archive receipt failed source SHA-256 verification")
                result["archived"] += 1
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ImportFailure("Source changed during archive ingestion")
        print(json.dumps({"file": str(path.relative_to(root)), "sha256": digest,
                          "progress": result}, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mount", nargs="?", type=Path, help="Optional mounted SD path; auto-detected if only one removable card")
    parser.add_argument("--apply", action="store_true", help="Copy files into OctoPrint; default dry run")
    parser.add_argument("--archive", action="store_true",
                        help="Hydrate canonical Pi storage instead of OctoPrint; never print")
    args = parser.parse_args()
    try:
        mount = args.mount
        if mount is None:
            options = detect_mounts()
            if len(options) != 1:
                detail = ", ".join(str(p) for p in options) if options else "none"
                raise ImportFailure(
                    f"Expected one mounted removable USB SD card; found {detail}. "
                    "Insert and mount the card or specify SD_MOUNT explicitly. "
                    "Use lsblk -o NAME,TRAN,RM,FSTYPE,LABEL,MOUNTPOINTS to inspect."
                )
            mount = options[0]
            print("Detected removable card:", mount)
        if args.archive:
            print(json.dumps(archive_card(mount, apply=args.apply), sort_keys=True))
        else:
            import_card(mount, apply=args.apply)
    except (ImportFailure, HTTPError, URLError, OSError, ValueError) as exc:
        parser.exit(1, f"SD import failed: {exc}\n")


if __name__ == "__main__":
    main()
