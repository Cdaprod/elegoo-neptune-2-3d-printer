"""Shared artifact classification and bounded transport admission (no execution)."""

import re
import struct
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

ARTIFACT_EXTENSIONS = {
    "execution_gcode": {".gcode", ".gco", ".gc", ".g"},
    "design_freecad": {".fcstd", ".fcstd1", ".fcstd2", ".fcbak"},
    "manufacturing_geometry": {".step", ".stp", ".stl", ".obj"},
    "slicer_project": {".3mf"},
    "cad_source": {".py"},
    "design_blender": {".blend"},
    "observation": {".png", ".jpg", ".jpeg", ".webm", ".mp4", ".json"},
}
TRANSPORT_KINDS = {
    "execution_gcode",
    "design_freecad",
    "manufacturing_geometry",
    "slicer_project",
}
# Native documents only; backup formats are not part of the remote intake contract.
TRANSPORT_EXCLUSIONS = {".fcstd1", ".fcstd2", ".fcbak"}
MAX_BYTES = 50 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,160}\.[A-Za-z0-9]{1,8}")


def transport_kind(filename):
    if not NAME.fullmatch(filename) or ".." in filename:
        raise ValueError("Supply a plain supported engineering filename")
    suffix = Path(filename).suffix.lower()
    for kind in TRANSPORT_KINDS:
        if suffix in ARTIFACT_EXTENSIONS[kind] and suffix not in TRANSPORT_EXCLUSIONS:
            return kind
    raise ValueError("Unsupported engineering artifact format")


def validate_package(path, suffix):
    # Inspect the container directory only. Geometry and XML parsing belong downstream.
    try:
        with ZipFile(path) as package:
            entries = package.infolist()
            names = {entry.filename for entry in entries}
            if (
                len(entries) > 3000
                or len(names) != len(entries)
                or sum(entry.file_size for entry in entries) > 512 * 1024**2
                or any(entry.file_size > 128 * 1024**2 for entry in entries)
            ):
                raise ValueError("Package expansion or member budget exceeded")
            for entry in entries:
                name = entry.filename
                if (
                    PurePosixPath(name).is_absolute()
                    or "\\" in name
                    or ":" in name
                    or any(part in {".", ".."} for part in name.split("/"))
                    or entry.flag_bits & 1
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise ValueError("Unsafe package member")
            if suffix == ".3mf":
                valid = "[Content_Types].xml" in names and any(
                    name.lower().startswith("3d/") and name.lower().endswith(".model")
                    for name in names
                )
            else:
                valid = "Document.xml" in names
            if not valid:
                raise ValueError("Required engineering package members are absent")
    except BadZipFile as error:
        raise ValueError("Invalid engineering package") from error


def validate_transport_content(path, filename):
    """Check bounded format signatures, not geometric validity or print safety."""
    kind = transport_kind(filename)
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise ValueError("Artifact exceeds transfer limit")
    suffix = Path(filename).suffix.lower()
    if suffix in {".3mf", ".fcstd"}:
        validate_package(path, suffix)
        return kind
    with path.open("rb") as source:
        sample = source.read(1024 * 1024)
        source.seek(max(0, size - 4096))
        tail = source.read()
    if suffix == ".stl" and len(sample) >= 84:
        count = struct.unpack_from("<I", sample, 80)[0]
        if count and size == 84 + 50 * count:
            return kind
    if b"\0" in sample or b"\0" in tail:
        raise ValueError("Unexpected binary content")
    if suffix == ".stl":
        valid = (
            sample.lstrip().lower().startswith(b"solid")
            and b"facet" in sample.lower()
            and b"vertex" in sample.lower()
            and b"endsolid" in tail.lower()
        )
    elif suffix == ".obj":
        valid = bool(re.search(rb"(?m)^\s*v\s+\S+\s+\S+\s+\S+", sample)) and bool(
            re.search(rb"(?m)^\s*f\s+\S+\s+\S+\s+\S+", sample + b"\n" + tail)
        )
    elif suffix in {".step", ".stp"}:
        valid = sample.lstrip().startswith(b"ISO-10303-21;") and tail.rstrip().endswith(
            b"END-ISO-10303-21;"
        )
    else:
        valid = bool(re.search(rb"(?im)^\s*(?:N\d+\s*)?[GMT]\s*\d+", sample))
    if not valid:
        raise ValueError("Content does not match engineering artifact format")
    return kind
