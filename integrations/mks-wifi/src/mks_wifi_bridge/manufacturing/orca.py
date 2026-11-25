"""Surgical 3MF mesh replacement: preserve configuration and reject unsafe painting."""

from __future__ import annotations

import math
import re
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from .project import digest, filehash

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS = {"c": CORE}


def metadata(node):
    return {m.get("key"): m.get("value") for m in node.findall("metadata")}


def stl_mesh(path: Path):
    raw = path.read_bytes()
    vertices, triangles, indices = [], [], {}
    if len(raw) >= 84 and len(raw) == 84 + 50 * struct.unpack_from("<I", raw, 80)[0]:
        rows = [r[3:12] for r in struct.iter_unpack("<12fH", raw[84:])]
    else:
        values = re.findall(r"\bvertex\s+(\S+)\s+(\S+)\s+(\S+)", raw.decode("ascii"))
        if not values or len(values) % 3:
            raise ValueError("Invalid STL")
        rows = [
            sum((tuple(map(float, v)) for v in values[i : i + 3]), ())
            for i in range(0, len(values), 3)
        ]
    for row in rows:
        tri = []
        for i in (0, 3, 6):
            point = tuple(row[i : i + 3])
            if not all(math.isfinite(v) for v in point):
                raise ValueError("Non-finite vertex")
            if point not in indices:
                indices[point] = len(vertices)
                vertices.append(point)
            tri.append(indices[point])
        if len(set(tri)) != 3:
            raise ValueError("Degenerate STL triangle")
        triangles.append(tri)
    if not triangles:
        raise ValueError("Empty mesh")
    return vertices, triangles


class OrcaProject:
    def __init__(self, path: Path):
        self.path = path
        with ZipFile(path) as z:
            if len(z.namelist()) != len(set(z.namelist())):
                raise ValueError("Duplicate 3MF entries")
            self.data = {n: z.read(n) for n in z.namelist()}
        self.settings = ET.fromstring(self.data["Metadata/model_settings.config"])
        self.model = ET.fromstring(self.data["3D/3dmodel.model"])
        self.plates = {}
        for plate in self.settings.findall("plate"):
            m = metadata(plate)
            name = m.get("plater_name")
            if not name or name in self.plates:
                raise ValueError("Plate names must be nonempty and unique")
            self.plates[name] = (int(m["plater_id"]), plate)

    def plate(self, plate):
        if plate.name not in self.plates:
            raise ValueError(f"Unknown Orca plate: {plate.name}")
        index, node = self.plates[plate.name]
        actual = {metadata(n)["object_id"] for n in node.findall("model_instance")}
        expected = {o.projectObjectId for o in plate.objects}
        if actual != expected:
            raise ValueError(f"Plate membership mismatch: {actual} != {expected}")
        return index, node

    def mapping(self, obj):
        parent = self.model.find(f"c:resources/c:object[@id='{obj.projectObjectId}']", NS)
        if parent is None:
            raise ValueError("Missing project object")
        parts = parent.findall("c:components/c:component", NS)
        matches = [
            p
            for p in parts
            if p.get("objectid") == obj.meshObjectId
            and p.get(f"{{{PROD}}}path", "").lstrip("/") == obj.meshPath
        ]
        if len(matches) != 1:
            raise ValueError("Explicit mesh mapping does not match project")
        # Shared resources must not unexpectedly change objects on other plates.
        references = [
            p
            for p in self.model.findall(".//c:component", NS)
            if p.get(f"{{{PROD}}}path", "").lstrip("/") == obj.meshPath
            and p.get("objectid") == obj.meshObjectId
        ]
        if len(references) != 1:
            raise ValueError("Shared mesh requires explicit project reconciliation")
        config = self.settings.find(f"object[@id='{obj.projectObjectId}']")
        part = config.find(f"part[@id='{obj.meshObjectId}']")
        if part is None or part.get("subtype") != "normal_part":
            raise ValueError("Only explicit normal parts can be replaced")
        m = metadata(part)
        return tuple(float(m.get("source_offset_" + c, "0")) for c in "xyz")

    def reconcile(self, plate, geometry: Path, output: Path):
        self.plate(plate)
        changed = {}
        for obj in plate.objects:
            offset = self.mapping(obj)
            original = changed.get(obj.meshPath, self.data[obj.meshPath])
            root = ET.fromstring(original)
            mesh = root.find(f"c:resources/c:object[@id='{obj.meshObjectId}']/c:mesh", NS)
            if mesh is None:
                raise ValueError("Mapped mesh is missing")
            # Triangle painting is indexed by topology; preserving it blindly is unsafe.
            for tri in mesh.findall("c:triangles/c:triangle", NS):
                if set(tri.attrib) - {"v1", "v2", "v3"}:
                    raise ValueError("Changed mesh has facet painting; operator remap required")
            vertices, triangles = stl_mesh(geometry / obj.export)
            vs = "".join(
                '<vertex x="{:.9g}" y="{:.9g}" z="{:.9g}"/>'.format(
                    *tuple(v[i] - offset[i] for i in range(3))
                )
                for v in vertices
            )
            ts = "".join(f'<triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>' for t in triangles)
            # Patch only the selected object's mesh block; leave namespaces/IDs intact.
            text = original.decode()
            pattern = (
                r'(<object\b[^>]*\bid="'
                + re.escape(obj.meshObjectId)
                + r'"[^>]*>.*?<mesh>)(.*?)(</mesh>)'
            )
            replacement = "<vertices>" + vs + "</vertices><triangles>" + ts + "</triangles>"
            text, count = re.subn(
                pattern,
                lambda m, replacement=replacement: m[1] + replacement + m[3],
                text,
                flags=re.S,
            )
            if count != 1:
                raise ValueError("Unsupported 3MF mesh serialization")
            ET.fromstring(text)
            changed[obj.meshPath] = text.encode()
        with ZipFile(self.path) as source, ZipFile(output, "w") as dest:
            for info in source.infolist():
                dest.writestr(info, changed.get(info.filename, self.data[info.filename]))
        with ZipFile(output) as verify:
            for name, content in self.data.items():
                if name not in changed and verify.read(name) != content:
                    raise ValueError("Project preservation verification failed")
        return sorted(changed)

    def fingerprint(
        self, plate, geometry: Path, profiles: list[Path], version: str, parameters=None
    ):
        _, node = self.plate(plate)
        # Settings and transforms for this plate; other plate geometry is excluded.
        ids = {metadata(n)["object_id"] for n in node.findall("model_instance")}
        settings = [
            ET.tostring(n).decode() for n in self.settings.findall("object") if n.get("id") in ids
        ]
        transforms = [
            ET.tostring(n).decode()
            for n in self.model.findall("c:build/c:item", NS)
            if n.get("objectid") in ids
        ]
        components = [
            ET.tostring(n).decode()
            for n in self.model.findall("c:resources/c:object", NS)
            if n.get("id") in ids
        ]
        resources = {}
        for obj in plate.objects:
            self.mapping(obj)
            parent = self.model.find(f"c:resources/c:object[@id='{obj.projectObjectId}']", NS)
            for part in parent.findall("c:components/c:component", NS):
                path = part.get(f"{{{PROD}}}path", "").lstrip("/")
                if not path:
                    raise ValueError(
                        "Inline or nested component projects require a dedicated adapter"
                    )
                if path != obj.meshPath:
                    resources[path] = digest(self.data[path].hex())
        auxiliary = {
            n: digest(v.hex())
            for n, v in self.data.items()
            if n.startswith("Metadata/")
            and not re.search(r"(plate_|top_|pick_|model_settings|slice_info)", n)
        }
        return digest(
            {
                "pipeline": "print-bay-v2-geometry-png",
                "plate": ET.tostring(node).decode(),
                "objects": settings,
                "transforms": transforms,
                "components": components,
                "auxiliary": auxiliary,
                "resources": resources,
                "slicer": version,
                "parameters": {
                    o.id: {k: (parameters or {})[k] for k in o.parameterKeys} for o in plate.objects
                },
                "geometry": {o.id: filehash(geometry / o.export) for o in plate.objects},
                "profiles": [filehash(p) for p in profiles],
            }
        )


def version(executable: str):
    run = subprocess.run(
        [executable, "--help"], capture_output=True, text=True, timeout=30, check=True
    )
    if "--slice" not in run.stdout or "--outputdir" not in run.stdout:
        raise ValueError("Installed Orca CLI lacks plate slicing")
    return run.stdout.splitlines()[0]


def slice_plate(executable: str, project: Path, index: int, output: Path, profiles):
    args = [
        executable,
        "--slice",
        str(index),
        "--arrange",
        "0",
        "--orient",
        "0",
        "--outputdir",
        str(output),
    ]
    if profiles:
        raise ValueError(
            "Profile overrides require typed machine/process/filament support; "
            "embed effective profiles in the source 3MF for this version"
        )
    args.append(str(project))
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    (output / "slice.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError(f"OrcaSlicer failed ({result.returncode}); inspect slice.log")
    candidates = list(output.glob("*.gcode"))
    if len(candidates) != 1 or candidates[0].stat().st_size == 0:
        raise ValueError("Expected exactly one nonempty plate G-code")
    return candidates[0]
