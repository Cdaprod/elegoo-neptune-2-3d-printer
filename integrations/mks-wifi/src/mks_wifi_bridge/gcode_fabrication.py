"""Bounded, read-only 3D filament reconstruction from local sliced G-code.

Produces shaded isometric SVGs and a sampled Three.js-compatible geometry feed.
Never talks to the printer, writes into source directories, or asserts CAD fidelity.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, TextIO
from xml.sax.saxutils import escape

MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_SOURCE_LINES = 1_500_000
MAX_SEGMENTS = 350_000
MAX_CACHE_BYTES = 80 * 1024 * 1024
CACHE_VERSION = "filament-v4-connected"
COMMAND = re.compile(r"^\s*(?:N\d+\s*)?([GMT])\s*(\d+)", re.I)
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
PARAMETER = re.compile(r"([XYZEIFJR])\s*(" + NUMBER + r")", re.I)
WIDTH = re.compile(r"^;\s*(?:line_width|extrusion_width)\s*=\s*(" + NUMBER + r")", re.I)
COLORS = ("#9db5c6", "#618eac", "#dfb57e", "#cbd9e2")


class ModelPreviewError(ValueError):
    """Safe rendering failure; caller can fall back to an embedded thumbnail."""


def _feature(comment: str, current: int) -> int:
    value = comment.strip().upper()
    if not value.startswith(("TYPE:", "FEATURE:")):
        return current
    if "SUPPORT" in value:
        return 2
    if "INFILL" in value or "FILL" in value:
        return 1
    if any(tag in value for tag in ("SKIN", "TOP", "BOTTOM", "SOLID")):
        return 3
    return 0


def simplify_path(points, tolerance):
    """Bounded-error RDP: endpoints, closed rings and separate deposition runs survive."""
    if len(points) <= 3:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    operations = 0
    while stack:
        first, last = stack.pop()
        a, b = points[first], points[last]
        direction = [b[i] - a[i] for i in range(3)]
        length = sum(v * v for v in direction)
        farthest, distance = None, tolerance * tolerance
        for index in range(first + 1, last):
            operations += 1
            if operations > 20_000_000:
                raise ModelPreviewError("Connected path simplification exceeded CPU budget")
            point = points[index]
            factor = (
                max(0, min(1, sum((point[i] - a[i]) * direction[i] for i in range(3)) / length))
                if length
                else 0
            )
            error = sum((point[i] - a[i] - factor * direction[i]) ** 2 for i in range(3))
            if error > distance:
                farthest, distance = index, error
        if farthest is not None:
            keep.add(farthest)
            stack.extend(((first, farthest), (farthest, last)))
    result = [points[index] for index in sorted(keep)]
    # Small closed features must not collapse into a line or triangle.
    if points[0] == points[-1] and len(result) < min(9, len(points)):
        return points
    return result


def reconstruct(stream: TextIO) -> dict[str, Any]:
    """Read G0/G1 extrusion, G90/G91, M82/M83, G92, and XY I/J arcs."""
    position = {axis: 0.0 for axis in "XYZE"}
    xyz_relative = False
    e_relative = False
    units = 1.0
    kind = 0
    part_feature = False
    part_minimum = [math.inf] * 3
    part_maximum = [-math.inf] * 3
    width = 0.44
    segments: list[list[float | int]] = []
    minimum = [math.inf] * 3
    maximum = [-math.inf] * 3
    emitted = 0
    ranges = []
    path = []
    path_kind = 0
    path_width = width
    layer_height = 0.2
    last_layer_z = None
    path_source_count = 0
    max_error = 0.0

    def flush_path():
        nonlocal path, path_source_count, max_error
        if len(path) < 2:
            path = []
            return
        tolerance = min(0.035, path_width * 0.08)
        reduced = simplify_path(path, tolerance)
        start = len(segments)
        if start + len(reduced) - 1 > MAX_SEGMENTS:
            raise ModelPreviewError(
                "Connected geometry exceeds display budget; original is preserved"
            )
        for a, b in zip(reduced, reduced[1:], strict=False):
            segments.append([*(round(v, 3) for v in a + b), round(path_width, 3), path_kind])
        ranges.append(
            {
                "start": start,
                "count": len(reduced) - 1,
                "source_segments": path_source_count,
                "closed": reduced[0] == reduced[-1],
                "height": layer_height,
            }
        )
        max_error = max(max_error, tolerance)
        path = []
        path_source_count = 0

    chord_arcs = 0
    lines = 0
    for lines, raw in enumerate(stream, 1):
        if lines > MAX_SOURCE_LINES:
            raise ModelPreviewError("G-code exceeds the bounded renderer line limit")
        code, _, comment = raw.partition(";")
        if comment:
            tag = comment.strip().upper()
            if tag.startswith(("TYPE:", "FEATURE:")):
                flush_path()
            height = re.match(r"^\s*layer_height\s*=\s*(" + NUMBER + r")", comment, re.I)
            if height and 0.03 <= float(height[1]) <= 1.2:
                flush_path()
                layer_height = float(height[1])
            if tag.startswith(("TYPE:", "FEATURE:")):
                # Explicit slicer feature evidence, never a spatial outlier heuristic.
                part_feature = any(
                    word in tag
                    for word in (
                        "PERIMETER",
                        "WALL",
                        "INFILL",
                        "SOLID",
                        "TOP",
                        "BOTTOM",
                        "SKIN",
                        "GAP FILL",
                        "BRIDGE",
                        "SUPPORT",
                    )
                ) and not any(word in tag for word in ("SKIRT", "BRIM", "PRIME", "WIPE", "CUSTOM"))
            kind = _feature(comment, kind)
            match = WIDTH.match(";" + comment)
            if match:
                proposed = float(match.group(1))
                if 0.15 <= proposed <= 2:
                    width = proposed
        cmd = COMMAND.match(code)
        if cmd is None:
            continue
        family, operation = cmd.group(1).upper(), int(cmd.group(2))
        if family == "G" and operation in (90, 91):
            xyz_relative = operation == 91
            continue
        if family == "M" and operation in (82, 83):
            e_relative = operation == 83
            continue
        if family == "G" and operation in (20, 21):
            units = 25.4 if operation == 20 else 1.0
            continue
        if family != "G" or operation not in (0, 1, 2, 3, 92):
            continue
        args = {
            axis.upper(): float(value) * units
            for axis, value in PARAMETER.findall(code[cmd.end() :])
        }
        if any(not math.isfinite(v) or abs(v) > 10000 for v in args.values()):
            continue
        if operation == 92:
            flush_path()
            for axis in "XYZE":
                if axis in args:
                    position[axis] = args[axis]
            continue
        old = position.copy()
        for axis in "XYZE":
            if axis in args:
                relative = e_relative if axis == "E" else xyz_relative
                position[axis] = old[axis] + args[axis] if relative else args[axis]
        if any(not math.isfinite(v) or abs(v) > 10000 for v in position.values()):
            position = old
            continue
        delta_e = args.get("E", 0.0) if e_relative else position["E"] - old["E"]
        if delta_e <= 0.000001:
            flush_path()
            continue
        start = (old["X"], old["Y"], old["Z"])
        end = (position["X"], position["Y"], position["Z"])
        points = [start]
        if operation in (2, 3) and ("I" in args or "J" in args):
            cx, cy = old["X"] + args.get("I", 0.0), old["Y"] + args.get("J", 0.0)
            radius = math.hypot(old["X"] - cx, old["Y"] - cy)
            begin = math.atan2(old["Y"] - cy, old["X"] - cx)
            finish = math.atan2(end[1] - cy, end[0] - cx)
            sweep = (
                (finish - begin) % (2 * math.pi)
                if operation == 3
                else -((begin - finish) % (2 * math.pi))
            )
            if abs(sweep) < 1e-8:
                sweep = (2 * math.pi) * (1 if operation == 3 else -1)
            if radius > 0.00001:
                steps = max(2, min(300, math.ceil(abs(sweep) * radius / 1.25)))
                for n in range(1, steps):
                    fraction = n / steps
                    angle = begin + fraction * sweep
                    points.append(
                        (
                            cx + radius * math.cos(angle),
                            cy + radius * math.sin(angle),
                            start[2] + (end[2] - start[2]) * fraction,
                        )
                    )
        elif operation in (2, 3):
            chord_arcs += 1  # R-arcs: only straight chord, disclosed in stats.
        points.append(end)
        for a, b in zip(points, points[1:], strict=False):
            if not 0.00001 < math.dist(a, b) <= 5000:
                continue
            emitted += 1
            for i in range(3):
                minimum[i] = min(minimum[i], a[i], b[i])
                maximum[i] = max(maximum[i], a[i], b[i])
                if part_feature:
                    part_minimum[i] = min(part_minimum[i], a[i], b[i])
                    part_maximum[i] = max(part_maximum[i], a[i], b[i])
            if path and (path[-1] != a or path_kind != kind or path_width != width):
                flush_path()
            if not path:
                if a[2] == b[2] and a[2] > 0:
                    inferred = a[2] - (last_layer_z or 0)
                    if 0.03 <= inferred <= 1.2:
                        layer_height = inferred
                    last_layer_z = a[2]
                path = [a]
                path_kind, path_width = kind, width
            path.append(b)
            path_source_count += 1
            if len(path) >= 8192:
                flush_path()  # Bounded memory; the next run starts at the same endpoint.
    flush_path()
    if not emitted or not segments:
        raise ModelPreviewError("No deposited filament paths found in G-code")
    return {
        "source": "reconstructed_gcode_extrusion",
        "segments": segments,
        "path_ranges": ranges,
        "bounds": [minimum, maximum],
        "display_bounds": [part_minimum, part_maximum]
        if all(math.isfinite(v) for v in part_minimum + part_maximum)
        else [minimum, maximum],
        "stats": {
            "extrusion_segments": emitted,
            "rendered_segments": len(segments),
            "sampling_stride": 1,
            "simplification": "connected_rdp_v1",
            "max_deviation_mm": max_error + 0.001,
            "deposition_runs": len(ranges),
            "original_toolpath": "immutable_canonical_gcode",
            "arcs_represented_as_chords": chord_arcs,
            "source_lines": lines,
            "approximate": True,
        },
    }


def render_svg(model: dict[str, Any], view: str = "iso") -> bytes:
    """Project filament with layered highlights, shadows, feature shading."""
    if view not in ("iso", "top", "front"):
        raise ModelPreviewError("Unsupported model camera view")
    lo, hi = model["bounds"]
    cx, cy, cz = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)]

    def project(p: list[float] | tuple[float, ...]) -> tuple[float, float]:
        x, y, z = p[0] - cx, p[1] - cy, p[2] - cz
        if view == "top":
            return x, -y
        if view == "front":
            return x, -z
        return (x - y) * 0.7071068, (x + y) * 0.3535534 - z * 0.8660254

    coords = [project(s[:3]) for s in model["segments"]]
    coords.extend(project(s[3:6]) for s in model["segments"])
    xs, ys = zip(*coords, strict=True)
    xmin, ymin = min(xs), min(ys)
    dx, dy = max(xs) - xmin, max(ys) - ymin
    zoom = min(804 / max(dx, 0.1), 558 / max(dy, 0.1), 32)
    offset_x = 480 - (xmin + dx / 2) * zoom
    offset_y = 351 - (ymin + dy / 2) * zoom

    def screen(p: tuple[float, float]) -> tuple[float, float]:
        return p[0] * zoom + offset_x, p[1] * zoom + offset_y

    thickness = max(1.2, min(5.0, 0.42 * zoom * 0.85))
    floor = min(645, 351 + dy * zoom / 2 + 31)
    output = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="720" '
        'viewBox="0 0 960 720" role="img" '
        'aria-label="Shaded 3D reconstruction of deposited G-code extrusion">',
        '<defs><radialGradient id="bg"><stop stop-color="#1d3a52"/>'
        '<stop offset="1" stop-color="#081623"/></radialGradient>'
        '<radialGradient id="shadow"><stop stop-color="#020a13" '
        'stop-opacity=".72"/><stop offset="1" stop-color="#020a13" '
        'stop-opacity="0"/></radialGradient></defs>',
        '<rect width="960" height="720" fill="url(#bg)"/>',
        f'<ellipse cx="480" cy="{floor:.1f}" '
        f'rx="{min(410, max(75, dx * zoom * 0.68)):.1f}" '
        'ry="38" fill="url(#shadow)"/>',
    ]
    ordered = sorted(
        model["segments"], key=lambda s: ((s[2] + s[5]) / 2, (s[0] + s[3] + s[1] + s[4]) / 4)
    )
    paths: dict[tuple[int, int], list[str]] = defaultdict(list)
    for n, seg in enumerate(ordered):
        a, b = screen(project(seg[:3])), screen(project(seg[3:6]))
        category = int(seg[7]) % len(COLORS)
        paths[(n // 170, category)].append(f"M{a[0]:.1f},{a[1]:.1f}L{b[0]:.1f},{b[1]:.1f}")
    for (_, category), lines in paths.items():
        path = " ".join(lines)
        output.extend(
            [
                f'<path d="{path}" stroke="#152a3c" '
                f'stroke-width="{thickness + 1.4:.2f}" stroke-linecap="round" fill="none"/>',
                f'<path d="{path}" stroke="{COLORS[category]}" '
                f'stroke-width="{thickness:.2f}" stroke-linecap="round" fill="none"/>',
                f'<path d="{path}" stroke="#f5fcff" '
                f'stroke-width="{max(0.35, thickness * 0.18):.2f}" '
                'stroke-linecap="round" fill="none" opacity=".24"/>',
            ]
        )
    sizes = [b - a for a, b in zip(lo, hi, strict=True)]
    output.extend(
        [
            '<path d="M22 662 H938" stroke="#3b5971" opacity=".5"/>',
            '<text x="31" y="39" fill="#daeffe" font-family="sans-serif" '
            'font-size="18" font-weight="700">G-CODE / DEPOSITED GEOMETRY</text>',
            f'<text x="31" y="691" fill="#a4c1d5" font-family="monospace" '
            f'font-size="16">{sizes[0]:.1f} × {sizes[1]:.1f} × {sizes[2]:.1f} mm'
            f" | {escape(view.upper())} | "
            f"{model['stats']['rendered_segments']:,} connected spans</text>",
            "</svg>",
        ]
    )
    return "".join(output).encode("utf-8")


class FabricationPreviewService:
    """Fingerprint-keyed persistent cache with race-resistant no-follow reads."""

    def __init__(self, source_dir: str | Path | None, cache_dir: str | Path | None):
        self.source_dir = Path(source_dir) if source_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def _open(self, relative: str) -> tuple[int, os.stat_result]:
        if self.source_dir is None or not relative or "\\" in relative or "\x00" in relative:
            raise ModelPreviewError("No valid G-code source was configured")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or path.as_posix() != relative
            or not path.parts
            or any(p in ("..", ".") for p in relative.split("/"))
            or path.suffix.lower() != ".gcode"
        ):
            raise ModelPreviewError("Invalid G-code file path")
        try:
            directory = os.open(self.source_dir, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise FileNotFoundError("Configured G-code volume is unavailable") from exc
        try:
            for name in path.parts[:-1]:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                os.close(fd)
                raise ModelPreviewError("Source must be regular G-code no larger than 50 MB")
            return fd, info
        except OSError as exc:
            raise ModelPreviewError("G-code source unavailable or symbolic link") from exc
        finally:
            os.close(directory)

    def _prune(self) -> None:
        if not self.cache_dir:
            return
        files = sorted(
            list(self.cache_dir.glob("*.json")) + list(self.cache_dir.glob("*.png")),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        size = 0
        for path in files:
            size += path.stat().st_size
            if size > MAX_CACHE_BYTES:
                path.unlink(missing_ok=True)

    def geometry(self, relative: str) -> dict[str, Any]:
        fd, info = self._open(relative)
        try:
            fingerprint = "\0".join(
                map(
                    str, (CACHE_VERSION, relative, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                )
            )
            key = hashlib.sha256(fingerprint.encode()).hexdigest()
            target = self.cache_dir / (key + ".json") if self.cache_dir else None
            if target and target.is_file():
                try:
                    with target.open(encoding="utf-8") as source:
                        cached = json.load(source)
                    if cached.get("version") == CACHE_VERSION:
                        return cached
                except (OSError, ValueError):
                    pass
            with os.fdopen(os.dup(fd), encoding="utf-8", errors="replace") as source:
                model = reconstruct(source)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ModelPreviewError("G-code source changed during reconstruction")
            model["version"] = CACHE_VERSION
            model["cache_key"] = key
            if target:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        mode="w", dir=target.parent, suffix=".tmp", encoding="utf-8", delete=False
                    ) as output:
                        json.dump(model, output, separators=(",", ":"))
                        temporary = Path(output.name)
                    os.replace(temporary, target)
                    self._prune()
                except OSError:
                    pass  # Storage issues must not prevent read-only preview.
            return model
        finally:
            os.close(fd)

    def preview(self, relative: str, view: str = "iso") -> bytes:
        if view not in ("iso", "top", "front"):
            raise ModelPreviewError("Unsupported model camera view")
        return render_svg(self.geometry(relative), view)

    def raster_preview(self, relative: str, view: str = "iso") -> bytes:
        """Bounded static raster: browsers never repaint thousands of SVG strokes per card."""
        from PIL import Image, ImageDraw

        if view not in {"iso", "top", "front"}:
            raise ModelPreviewError("Unsupported model camera view")
        model = self.geometry(relative)
        target = (
            self.cache_dir / (model["cache_key"] + "-raster-v1-" + view + ".png")
            if self.cache_dir
            else None
        )
        if target and target.is_file():
            return target.read_bytes()
        lo, hi = model.get("display_bounds", model["bounds"])
        center = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)]

        def project(p):
            x, y, z = [p[i] - center[i] for i in range(3)]
            return (
                (x, -y)
                if view == "top"
                else (x, -z)
                if view == "front"
                else ((x - y) * 0.7071, (x + y) * 0.3536 - z * 0.8660)
            )

        points = [
            project((x, y, z))
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]
        bounds = [(min(p[a] for p in points), max(p[a] for p in points)) for a in (0, 1)]
        scale = min(
            1260 / max(bounds[0][1] - bounds[0][0], 0.1),
            880 / max(bounds[1][1] - bounds[1][0], 0.1),
        )

        def screen(p):
            x, y = project(p)
            return ((x - sum(bounds[0]) / 2) * scale + 768, (y - sum(bounds[1]) / 2) * scale + 560)

        image = Image.new("RGB", (1536, 1152), "#091827")
        draw = ImageDraw.Draw(image)
        ordered = sorted(
            model["segments"], key=lambda s: ((s[2] + s[5]) / 2, (s[0] + s[3] + s[1] + s[4]) / 4)
        )
        for segment in ordered:
            line = [screen(segment[:3]), screen(segment[3:6])]
            width = max(2, min(12, round(float(segment[6] or 0.42) * scale * 0.85)))
            draw.line(line, fill="#142a3b", width=width + 2)
            draw.line(line, fill=COLORS[int(segment[7]) % len(COLORS)], width=width)
        image = image.resize((768, 576), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
        data = output.getvalue()
        if target:
            temporary = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, suffix=".tmp", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                os.replace(temporary, target)
                self._prune()
            except OSError:
                pass
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return data
