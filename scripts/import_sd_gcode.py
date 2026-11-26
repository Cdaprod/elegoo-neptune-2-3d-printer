#!/usr/bin/env python3
"""Import G-code from an explicitly selected mounted card into OctoPrint local storage.
Never formats media, selects a job, or starts a print.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, build_opener
from authorize_octoprint import NoRedirects, local_url
from setup_env import ENV, parse_env

MAX_BYTES = 50 * 1024 * 1024
SUFFIXES = {".gcode", ".gco", ".gc", ".g"}

def candidates(root):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            p = Path(directory) / name
            if not name.startswith(".") and p.suffix.lower() in SUFFIXES and not p.is_symlink() and p.is_file():
                yield p

def multipart(name, data):
    boundary = "neptune-" + os.urandom(24).hex()
    parts = []
    for key in ("select", "print"):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\nfalse\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    return boundary, b"".join(parts) + f"--{boundary}--\r\n".encode()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mount", type=Path)
    parser.add_argument("--apply", action="store_true", help="Upload copies; default is dry run")
    args = parser.parse_args()
    root = args.mount.resolve(strict=True)
    if root == Path("/") or not root.is_dir() or not os.path.ismount(root):
        parser.error("Choose the actual mounted SD volume, not a parent or ordinary folder")
    files = list(candidates(root))
    if len(files) > 2000:
        parser.error("More than 2000 files; import a smaller card")
    env = parse_env(ENV)
    base = local_url(env.get("OCTOPRINT_HOST_PORT", "5000"))
    key = env.get("OCTOPRINT_API_KEY", "")
    opener = build_opener(NoRedirects())
    if args.apply and not key:
        parser.error("Run make authorize-octoprint first")
    existing = set()
    if args.apply:
        req = Request(base + "/api/files/local", headers={"X-Api-Key": key})
        with opener.open(req, timeout=15) as response:
            existing = {item.get("name", "").casefold() for item in json.load(response).get("files", [])}
    for p in files:
        if p.stat().st_size > MAX_BYTES:
            print("Skipped oversized:", p.name)
            continue
        data = p.read_bytes()
        if len(data) > MAX_BYTES:
            raise RuntimeError("File changed during read")
        # Preserve firmware-compatible root names when possible. Never overwrite.
        name = p.name
        if not name.isascii() or any(not (c.isalnum() or c in "._- ") for c in name):
            name = hashlib.sha256(data).hexdigest()[:20] + ".gcode"
        if name.casefold() in existing:
            print("Skipped existing name:", name)
            continue
        print("Import" if args.apply else "Would import", name, len(data), "bytes")
        if args.apply:
            boundary, body = multipart(name, data)
            req = Request(base + "/api/files/local", data=body, headers={"X-Api-Key": key, "Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
            with opener.open(req, timeout=120) as response:
                if response.status not in (200, 201):
                    raise RuntimeError("Unexpected upload response")
            existing.add(name.casefold())
    print("Refresh Fabrication gallery. No print was selected or started.")

if __name__ == "__main__":
    main()
