#!/usr/bin/env python3
"""Private actual-artifact regression: immutable bytes, connected runs and error bounds."""

import argparse
import hashlib
import json
import math
import subprocess
import time
import types
from pathlib import Path

from mks_wifi_bridge.gcode_fabrication import ModelPreviewError, reconstruct


def distance(point, a, b):
    vector = [b[i] - a[i] for i in range(3)]
    length = sum(v * v for v in vector)
    factor = (
        max(0, min(1, sum((point[i] - a[i]) * vector[i] for i in range(3)) / length))
        if length
        else 0
    )
    return math.sqrt(sum((point[i] - a[i] - factor * vector[i]) ** 2 for i in range(3)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = types.ModuleType("verified_repository_baseline")
    source = subprocess.check_output(
        [
            "git",
            "show",
            args.baseline_ref + ":integrations/mks-wifi/src/mks_wifi_bridge/gcode_fabrication.py",
        ],
        text=True,
    )
    exec(compile(source, "repository-baseline", "exec"), baseline.__dict__)
    report = {
        "schema_version": 1,
        "environment": "actual_archived_artifacts_on_mac",
        "baseline": args.baseline_ref,
        "items": [],
        "source_modifications": False,
    }
    for fixture in json.loads(args.manifest.read_text())["artifacts"]:
        path = Path(fixture["path"])
        assert not path.is_symlink()
        before_stat = path.stat()
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == fixture["sha256"]
        item = {"sha256": fixture["sha256"], "filename": path.name}
        started = time.monotonic()
        try:
            baseline.MAX_SEGMENTS = 7500
            with path.open() as stream:
                sampled = baseline.reconstruct(stream)
            baseline.MAX_SEGMENTS = 2_000_000
            with path.open() as stream:
                full = baseline.reconstruct(stream)
            with path.open() as stream:
                current = reconstruct(stream)
            assert current["bounds"] == full["bounds"]
            assert current["stats"]["extrusion_segments"] == full["stats"]["extrusion_segments"]
            original = full["segments"]
            offset, maximum_error = 0, 0.0
            for run in current["path_ranges"]:
                spans = current["segments"][run["start"] : run["start"] + run["count"]]
                count = run["source_segments"]
                assert spans[0][:3] == original[offset][:3]
                assert spans[-1][3:6] == original[offset + count - 1][3:6]
                assert all(a[3:6] == b[:3] for a, b in zip(spans, spans[1:], strict=False))
                if run["closed"]:
                    assert spans[0][:3] == spans[-1][3:6]
                cursor = offset
                for span in spans:
                    while True:
                        assert cursor < offset + count
                        raw = original[cursor]
                        assert raw[6:8] == span[6:8]
                        maximum_error = max(
                            maximum_error,
                            distance(raw[:3], span[:3], span[3:6]),
                            distance(raw[3:6], span[:3], span[3:6]),
                        )
                        cursor += 1
                        if raw[3:6] == span[3:6]:
                            break
                assert cursor == offset + count
                offset += count
            assert offset == len(original)
            assert maximum_error <= current["stats"]["max_deviation_mm"] + 0.001
            item.update(
                state="passed",
                original_segments=len(original),
                before_spans=len(sampled["segments"]),
                after_spans=len(current["segments"]),
                connected_runs=len(current["path_ranges"]),
                max_error_mm=maximum_error,
            )
        except (ModelPreviewError, baseline.ModelPreviewError) as error:
            assert fixture.get("expected") == "no_deposition", str(error)
            item.update(state="no_deposition", reason=str(error))
        assert (before_stat.st_size, before_stat.st_mtime_ns) == (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        item["seconds"] = time.monotonic() - started
        report["items"].append(item)
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(item), flush=True)


if __name__ == "__main__":
    main()
