"""Native Mac worker: prepare changed projects automatically, never deploy them."""

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .controller import Controller, now
from .project import digest


def slicer_version(controller):
    executable = os.getenv("ORCASLICER_PATH", "orca-slicer")
    resolved = shutil.which(executable)
    info = Path(resolved).stat() if resolved else None
    stamp = (
        executable,
        info.st_mtime_ns if info else None,
        info.st_size if info else None,
        id(controller.version),
    )
    cached = getattr(controller, "_worker_slicer_probe", None)
    if cached and cached[0] == stamp and time.monotonic() - cached[1] < 30:
        return cached[2]
    try:
        value = controller.version(executable)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        value = "unavailable:" + str(exc)
    controller._worker_slicer_probe = (stamp, time.monotonic(), value)
    return value


def reconcile_once(controller):
    """Persist source reconciliation so restarts do not repeat jobs or retry failures."""
    queued = 0
    for item in controller.discover():
        if item["error"]:
            continue
        root, manifest = controller.project(item["name"])
        policy = manifest.spec.deployment
        if not (policy.autoBuild or policy.autoSlice):
            continue
        version = slicer_version(controller) if policy.autoSlice else None
        inputs = controller.inputs(root, manifest)
        signature = digest([inputs, version])
        with controller.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT signature FROM automation WHERE project=?", (item["name"],)
            ).fetchone()
            if prior and prior[0] == signature:
                continue
            active = db.execute(
                "SELECT 1 FROM jobs WHERE project=? AND status IN ('queued','running')",
                (item["name"],),
            ).fetchone()
            if active:
                continue
            for plate in manifest.spec.plates:
                ready = db.execute(
                    "SELECT * FROM artifacts WHERE project=? AND plate=? ORDER BY created DESC",
                    (item["name"], plate.name),
                ).fetchall()
                if policy.autoSlice and any(
                    controller.fresh(dict(r))
                    and json.loads(r["receipt"])["inputs"] == inputs
                    and json.loads(r["receipt"])["slicer"] == version
                    and (not policy.autoStage or r["staged"])
                    for r in ready
                ):
                    continue
                if policy.autoSlice and policy.autoBuild:
                    action = "build-slice-stage" if policy.autoStage else "build-slice"
                elif policy.autoSlice:
                    action = "slice-stage" if policy.autoStage else "slice"
                else:
                    action = "build"
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, item["name"], plate.name, action, "queued", None, now()),
                )
                queued += 1
            db.execute("INSERT OR REPLACE INTO automation VALUES(?,?)", (item["name"], signature))
    return queued


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage", default=os.getenv("NEPTUNE_MANUFACTURING_STORAGE", "/data/manufacturing")
    )
    parser.add_argument(
        "--projects",
        default=os.getenv("NEPTUNE_MANUFACTURING_PROJECTS", "/data/manufacturing/projects"),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--watch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically prepare changed projects (default: enabled)",
    )
    args = parser.parse_args()
    controller = Controller(Path(args.storage), Path(args.projects))
    # One long-running build worker per registry; a restart can identify abandoned jobs.
    import fcntl

    with (controller.storage / ".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A manufacturing worker already owns this registry")
        with controller.connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed',result=? WHERE status='running'",
                (
                    json.dumps(
                        {"error": "Build worker stopped before completion; retry preparation"}
                    ),
                ),
            )
        stopped = threading.Event()

        def heartbeat():
            while not stopped.is_set():
                controller.heartbeat("watching" if args.watch else "manual")
                stopped.wait(5)

        controller.heartbeat("watching" if args.watch else "manual")
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while True:
                if args.watch:
                    try:
                        reconcile_once(controller)
                    except Exception as exc:
                        print(
                            json.dumps({"status": "reconciliation_error", "error": str(exc)}),
                            flush=True,
                        )
                worked = controller.work_once()
                if args.once:
                    print(json.dumps({"processed": worked}))
                    return
                time.sleep(0.2 if worked else 2)
        finally:
            stopped.set()
            thread.join(timeout=1)
            controller.heartbeat("stopped")


if __name__ == "__main__":
    main()
