"""Deterministic geometry-derived plate PNG, independent of slicer GUI thumbnails."""

from __future__ import annotations

import base64
import math
from pathlib import Path

from PIL import Image, ImageDraw

from .orca import NS, PROD, stl_mesh

IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"


def transform(point, matrix):
    m = [float(v) for v in matrix.split()]
    if len(m) != 12 or not all(math.isfinite(v) for v in m):
        raise ValueError("Invalid 3MF transform")
    return tuple(sum(point[j] * m[j * 3 + i] for j in range(3)) + m[9 + i] for i in range(3))


def render(project, plate, geometry: Path, output: Path):
    faces = []
    for obj in plate.objects:
        offset = project.mapping(obj)
        vertices, triangles = stl_mesh(geometry / obj.export)
        parent = project.model.find(f"c:resources/c:object[@id='{obj.projectObjectId}']", NS)
        part = next(
            p
            for p in parent.findall("c:components/c:component", NS)
            if p.get("objectid") == obj.meshObjectId
            and p.get(f"{{{PROD}}}path", "").lstrip("/") == obj.meshPath
        )
        item = project.model.find(f"c:build/c:item[@objectid='{obj.projectObjectId}']", NS)
        if item is None:
            raise ValueError("Missing build transform")
        points = [
            transform(
                transform(
                    tuple(v[i] - offset[i] for i in range(3)), part.get("transform", IDENTITY)
                ),
                item.get("transform", IDENTITY),
            )
            for v in vertices
        ]
        # Orthographic view in the printer's Z-up coordinate system.
        projected = [
            (0.7071 * (x - y), 0.4082 * (x + y) - 0.8165 * z, 0.5774 * (x + y + z))
            for x, y, z in points
        ]
        for tri in triangles:
            a, b, c = (points[i] for i in tri)
            u = [b[i] - a[i] for i in range(3)]
            v = [c[i] - a[i] for i in range(3)]
            n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            length = math.sqrt(sum(k * k for k in n)) or 1
            shade = 0.45 + 0.55 * abs(sum(n[i] * (-0.3, 0.4, 0.86)[i] for i in range(3)) / length)
            color = tuple(min(255, int(k * shade)) for k in (105, 199, 216))
            faces.append(([projected[i] for i in tri], color))
    points = [p for face, _ in faces for p in face]
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    scale = 560 / max(xmax - xmin, ymax - ymin, 1e-6)
    image = Image.new("RGB", (640, 640), (16, 31, 48))
    drawing = ImageDraw.Draw(image)
    for face, color in sorted(faces, key=lambda f: sum(p[2] for p in f[0])):
        drawing.polygon(
            [
                (320 + (x - (xmin + xmax) / 2) * scale, 320 + (y - (ymin + ymax) / 2) * scale)
                for x, y, _ in face
            ],
            fill=color,
        )
    image = image.resize((320, 320), Image.Resampling.LANCZOS)
    image.save(output, format="PNG", optimize=True)
    return image


def embed_png(gcode: Path, png: Path):
    raw = gcode.read_bytes()
    begin = b"; Print Bay geometry preview\n"
    end = b"; Print Bay geometry preview end\n"
    if raw.startswith(begin):
        marker = raw.find(end)
        if marker < 0:
            raise ValueError("Incomplete generated preview prefix")
        raw = raw[marker + len(end) :]
    encoded = base64.b64encode(png.read_bytes()).decode("ascii")
    lines = ["; Print Bay geometry preview", f"; thumbnail begin 320x320 {len(encoded)}"]
    lines += ["; " + encoded[i : i + 76] for i in range(0, len(encoded), 76)]
    lines += ["; thumbnail end", "; Print Bay geometry preview end"]
    gcode.write_bytes(("\n".join(lines) + "\n").encode("ascii") + raw)
