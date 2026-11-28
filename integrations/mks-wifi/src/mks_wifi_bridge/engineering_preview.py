"""Bounded source-STL display derivatives. Never executes CAD source or hydrates originals."""

import hashlib
import io
import json
import math
import os
import re
import struct
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


class EngineeringPreview:
    def __init__(self, archive):
        self.archive = archive
        self.cache = archive.root / "derivatives"
        self.cache.mkdir(exist_ok=True)

    def cache_write(self, path, data):
        descriptor, temporary = tempfile.mkstemp(dir=self.cache, prefix=".surface-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        files = sorted(self.cache.glob("stl-surface-v2-*"), key=lambda value: value.stat().st_mtime)
        total = sum(value.stat().st_size for value in files)
        for value in files:
            if total <= 200 * 1024 * 1024:
                break
            if value != path:
                size = value.stat().st_size
                value.unlink(missing_ok=True)
                total -= size

    def geometry(self, sha):
        cached = self.cache / ("stl-surface-v2-" + sha + ".json")
        if cached.is_file():
            model = json.loads(cached.read_text())
            if model.get("artifact_sha256") == sha:
                with self.archive.db() as db:
                    row = db.execute(
                        "SELECT metadata FROM fabrication_artifacts WHERE sha256=?", (sha,)
                    ).fetchone()
                metadata = json.loads(row["metadata"]) if row else {}
                if metadata.get("source_3mf"):
                    model.update(
                        source="derived_3mf_surface",
                        units=metadata.get("units"),
                        provenance=metadata.get("geometry_provenance"),
                        display=metadata.get("display"),
                    )
                return model
        with self.archive.db() as db:
            row = db.execute(
                "SELECT filename,metadata FROM fabrication_artifacts WHERE sha256=?", (sha,)
            ).fetchone()
            sources = [
                item[0]
                for item in db.execute(
                    "SELECT path FROM artifact_locations "
                    "WHERE sha256=? AND provider='engineering_inventory'",
                    (sha,),
                )
            ]
        if row is None or not row[0].lower().endswith(".stl"):
            raise ValueError("Native display extraction currently supports STL surfaces")
        data = None
        for value in sources:
            path = Path(value)
            try:
                if path.is_symlink() or path.stat().st_size > 50 * 1024 * 1024:
                    continue
                with os.fdopen(
                    os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb"
                ) as stream:
                    raw = stream.read(50 * 1024 * 1024 + 1)
                if len(raw) <= 50 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == sha:
                    data = raw
                    break
            except OSError:
                continue
        if data is None:
            try:
                data = self.archive.verified(sha).read_bytes()
            except OSError as error:
                raise ValueError("Fingerprint-matching source STL is unavailable") from error
        triangles = []
        if len(data) >= 84 and 84 + 50 * int.from_bytes(data[80:84], "little") == len(data):
            count = int.from_bytes(data[80:84], "little")
            if count > 150000:
                raise ValueError("STL display budget exceeded; preprocess on the build node")
            stride = 1
            for index in range(0, count, stride):
                values = struct.unpack_from("<9f", data, 84 + 50 * index + 12)
                triangles.extend(values)
        else:
            values = re.findall(rb"\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)", data)
            count = len(values) // 3
            if count > 150000:
                raise ValueError("STL display budget exceeded; preprocess on the build node")
            stride = 1
            for index in range(0, count, stride):
                for vertex in values[index * 3 : index * 3 + 3]:
                    triangles.extend(float(value) for value in vertex)
        if not triangles or any(
            not math.isfinite(value) or abs(value) > 1e7 for value in triangles
        ):
            raise ValueError("Invalid or empty STL surface")
        bounds = [
            [min(triangles[axis::3]) for axis in range(3)],
            [max(triangles[axis::3]) for axis in range(3)],
        ]
        result = {
            "source": "original_stl_surface",
            "artifact_sha256": sha,
            "bounds": bounds,
            "surface_vertices": triangles,
            "stats": {"source_triangles": count, "rendered_triangles": len(triangles) // 9},
            "units": "source_coordinates_unverified",
            "provenance": "fingerprint_verified_source_geometry",
        }

        metadata = json.loads(row["metadata"])
        if metadata.get("source_3mf"):
            result.update(
                source="derived_3mf_surface",
                units=metadata.get("units", "source_coordinates_unverified"),
                provenance=metadata.get("geometry_provenance"),
                display=metadata.get("display"),
            )
        self.cache_write(cached, json.dumps(result, separators=(",", ":")).encode())
        return result

    def thumbnail(self, sha):
        cached = self.cache / ("stl-surface-v2-" + sha + ".png")
        if cached.is_file():
            return cached.read_bytes()
        model = self.geometry(sha)
        positions = model["surface_vertices"]
        faces = []
        for index in range(0, len(positions), 9):
            points = [positions[index + i : index + i + 3] for i in (0, 3, 6)]
            projected = [((x - y) * 0.7071, (x + y) * 0.4082 - z * 0.8165) for x, y, z in points]
            u = [points[1][a] - points[0][a] for a in range(3)]
            v = [points[2][a] - points[0][a] for a in range(3)]
            normal = [
                u[1] * v[2] - u[2] * v[1],
                u[2] * v[0] - u[0] * v[2],
                u[0] * v[1] - u[1] * v[0],
            ]
            length = math.sqrt(sum(value * value for value in normal)) or 1
            illumination = 0.36 + 0.64 * abs(
                sum(normal[a] * [0.3, 0.5, 0.8124][a] for a in range(3)) / length
            )
            color = (int(130 * illumination), int(181 * illumination), int(203 * illumination))
            faces.append((sum(x + y + z for x, y, z in points), projected, color))
        flat = [point for _, face, _ in faces for point in face]
        lo = [min(point[a] for point in flat) for a in range(2)]
        hi = [max(point[a] for point in flat) for a in range(2)]
        scale = min(440 / max(hi[0] - lo[0], 1e-9), 300 / max(hi[1] - lo[1], 1e-9))
        image = Image.new("RGB", (512, 384), "#091827")
        draw = ImageDraw.Draw(image)
        for _, face, color in sorted(faces):
            points = [
                ((x - (lo[0] + hi[0]) / 2) * scale + 256, (y - (lo[1] + hi[1]) / 2) * scale + 192)
                for x, y in face
            ]
            draw.polygon(points, fill=color)
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.cache_write(cached, output.getvalue())
        return output.getvalue()
