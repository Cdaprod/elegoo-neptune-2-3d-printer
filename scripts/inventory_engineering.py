#!/usr/bin/env python3
"""Bounded read-only engineering inventory. Never executes or hydrates sources."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import zipfile
from pathlib import Path

EXCLUDE = {
    ".git",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    "cache",
    "caches",
    "site-packages",
    ".trash",
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
    "system volume information",
    "$recycle.bin",
    "build",
    "dist",
    ".pio",
    ".pytest_cache",
    ".ruff_cache",
    "library caches",
}
TYPES = {
    ".step": "step",
    ".stp": "step",
    ".stl": "stl",
    ".obj": "obj",
    ".3mf": "slicer_project",
    ".gcode": "execution_gcode",
    ".gco": "execution_gcode",
    ".gc": "execution_gcode",
    ".blend": "blender",
    ".fcbak": "freecad_backup",
}
ROOTS = [
    "Documents",
    "Desktop",
    "Downloads",
    "Projects",
    "Developer",
    "GitHub",
    "repos",
    "Library/Application Support/FreeCAD",
    "Library/Application Support/OrcaSlicer",
]


def classify(path):
    suffix = path.suffix.lower()
    if re.fullmatch(r"\.fcstd\d*", suffix):
        return "freecad_backup" if suffix != ".fcstd" else "freecad"
    if suffix in TYPES:
        return TYPES[suffix]
    if path.name.lower() in {
        "model.yaml",
        "model.yml",
        "model-manifest.json",
    } or path.name.lower().endswith(".model.yaml"):
        return "model_manifest"
    if suffix == ".py":
        if any(
            word in path.stem.lower()
            for word in ("secret", "credential", "password", "token", "auth")
        ):
            return None
        if not any(
            re.search(
                r"3d|cad|hardware|model|geometry|bracket|enclosure|design",
                part,
                re.IGNORECASE,
            )
            for part in path.parts
        ):
            return None
        try:
            with path.open("rb") as source:
                header = source.read(65536).decode("utf-8", errors="replace")
            if re.search(r"(?:import|from)\s+(?:FreeCAD|cadquery|bpy)\b", header):
                return "cad_source"
        except OSError:
            pass
    return None


def metadata(path, kind):
    result = {"processor": kind, "geometry_status": "not_tessellated"}
    try:
        if kind in {
            "freecad",
            "freecad_backup",
            "slicer_project",
        } and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                result["embedded_geometry_files"] = sum(
                    name.endswith((".brp", ".brep", ".model")) for name in names
                )
                entry = (
                    "Document.xml" if kind.startswith("freecad") else "3D/3dmodel.model"
                )
                if (
                    entry in names
                    and archive.getinfo(entry).file_size <= 2 * 1024 * 1024
                ):
                    data = archive.read(entry)
                    result["object_count"] = len(
                        re.findall(rb"<(?:Object |object\b)", data)
                    )
                    # IDs are evidence only; never automatically become registry identity.
                    ids = re.findall(rb'(?:Uid|uuid)="([A-Za-z0-9-]{8,80})"', data)
                    result["embedded_identifiers"] = sorted({v.decode() for v in ids})[
                        :20
                    ]
        elif kind == "stl":
            with path.open("rb") as source:
                header = source.read(84)
            if len(header) == 84:
                count = int.from_bytes(header[80:84], "little")
                if 84 + 50 * count == path.stat().st_size:
                    result["triangle_count"] = count
                    result["encoding"] = "binary_stl"
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        result["metadata_status"] = "unavailable"
    return result


def git_context(path, cache):
    for parent in list(path.parents)[:16]:
        if parent in cache:
            return cache[parent]
        if (parent / ".git").exists():
            try:
                process = subprocess.run(
                    ["git", "-C", str(parent), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                head = process.stdout.strip()
                result = (
                    {
                        "root": str(parent),
                        "head": head,
                        "evidence": "repository_context_not_file_revision",
                    }
                    if re.fullmatch(r"[a-f0-9]{40,64}", head)
                    else None
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            cache[parent] = result
            return result
    return None


def inventory(
    roots, max_files=20000, max_dirs=30000, max_bytes=8 * 1024**3, seconds=180
):
    start = time.monotonic()
    files = []
    errors = []
    discovered = []
    consumed = 0
    directories = 0
    git_cache = {}
    seen_paths = set()
    stopped = False
    discovered = [
        {
            "path": str(Path(candidate).expanduser()),
            "exists": Path(candidate).expanduser().is_dir(),
        }
        for candidate in roots
    ]
    for root_info in discovered:
        if not root_info["exists"]:
            continue
        root = Path(root_info["path"]).resolve()
        root_directories = 0
        for directory, children, names in os.walk(root, followlinks=False):
            directories += 1
            root_directories += 1
            if root_directories > 5000:
                root_info["bounded_incomplete"] = True
                break
            if (
                directories > max_dirs
                or len(files) >= max_files
                or time.monotonic() - start > seconds
            ):
                stopped = True
                break
            children[:] = sorted(
                name
                for name in children
                if name.lower() not in EXCLUDE
                and not name.lower().endswith((".app", ".framework"))
                and not name.startswith(".")
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or name.startswith(".") or str(path) in seen_paths:
                    continue
                try:
                    flags = getattr(path.stat(), "st_flags", 0)
                except OSError:
                    continue
                dataless = bool(flags & getattr(stat, "SF_DATALESS", 0x40000000))
                kind = (
                    None
                    if dataless and path.suffix.lower() == ".py"
                    else classify(path)
                )
                if kind is None:
                    continue
                seen_paths.add(str(path))
                try:
                    before = path.stat()
                    size = before.st_size
                    item = {
                        "path": str(path),
                        "kind": kind,
                        "size_bytes": size,
                        "sha256": None,
                        "context": "test_or_generated"
                        if any(
                            p.lower() in {"tests", "fixtures", "examples", ".localdata"}
                            for p in path.parts
                        )
                        else "engineering_candidate",
                        "authority": "unresolved",
                        "model_identity": "unassigned",
                    }
                    if (
                        not dataless
                        and size <= 512 * 1024**2
                        and consumed + size <= max_bytes
                        and time.monotonic() - start <= seconds
                    ):
                        with path.open("rb") as source:
                            digest = hashlib.sha256()
                            count = 0
                            while block := source.read(65536):
                                count += len(block)
                                if (
                                    count > 512 * 1024**2
                                    or time.monotonic() - start > seconds
                                ):
                                    raise ValueError("Fingerprint budget exceeded")
                                digest.update(block)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (
                            after.st_size,
                            after.st_mtime_ns,
                        ):
                            raise ValueError("Source changed during inventory")
                        item["sha256"] = digest.hexdigest()
                        consumed += count
                        item.update(metadata(path, kind))
                    else:
                        item["fingerprint_status"] = "budget_or_size_limit"
                    item["git_context"] = git_context(path, git_cache)
                    files.append(item)
                except (OSError, ValueError) as error:
                    errors.append({"path": str(path), "error": str(error)[:200]})
                if len(files) >= max_files:
                    stopped = True
                    break
            if stopped:
                break
        if stopped:
            break
    by_hash = {}
    for item in files:
        if item["sha256"]:
            by_hash.setdefault(item["sha256"], []).append(item["path"])
    duplicates = [
        {"sha256": sha, "locations": paths, "evidence": "exact_bytes_only"}
        for sha, paths in by_hash.items()
        if len(paths) > 1
    ]
    by_identifier = {}
    for item in files:
        for identity in item.get("embedded_identifiers", []):
            by_identifier.setdefault(identity, []).append(item["path"])
    candidates = [
        {
            "evidence": "shared_embedded_identifier",
            "identifier": identity,
            "locations": sorted(set(paths)),
            "state": "candidate_requires_review",
            "confidence": "unverified",
            "warning": "Embedded IDs may be inherited from templates; not model identity",
        }
        for identity, paths in by_identifier.items()
        if len(set(paths)) > 1
    ]
    return {
        "schema_version": 1,
        "read_only": True,
        "roots": discovered,
        "files": files,
        "counts": {
            kind: sum(item["kind"] == kind for item in files)
            for kind in sorted({i["kind"] for i in files})
        },
        "total_bytes": sum(item["size_bytes"] for item in files),
        "fingerprinted_bytes": consumed,
        "exact_duplicates": duplicates,
        "fingerprinting_complete": all(item["sha256"] for item in files),
        "unfingerprinted_bytes": sum(
            item["size_bytes"] for item in files if not item["sha256"]
        ),
        "authority_policy": "All authorities unresolved unless explicitly declared and approved",
        "candidate_relationships": candidates,
        "errors": errors,
        "bounded_incomplete": stopped
        or any(root.get("bounded_incomplete") for root in discovered),
        "directories_visited": directories,
        "identity_policy": "No models or authorities created; names, paths and dates are not identity evidence",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    priorities = []
    for parent in (
        Path.home() / "Desktop",
        Path.home() / "Library/Mobile Documents/com~apple~CloudDocs",
    ):
        if parent.is_dir():
            priorities.extend(
                str(path)
                for path in parent.iterdir()
                if path.is_dir()
                and ("3d" in path.name.lower() or "model" in path.name.lower())
            )
    roots = args.root or [
        str(Path.cwd()),
        *priorities,
        *[str(Path.home() / p) for p in ROOTS],
        "/Volumes",
    ]
    result = inventory(roots)
    args.output.write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "counts",
                    "total_bytes",
                    "fingerprinted_bytes",
                    "bounded_incomplete",
                    "directories_visited",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
