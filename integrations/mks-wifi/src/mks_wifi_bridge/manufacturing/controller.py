"""Filesystem artifacts, SQLite registry and explicit build/slice/stage transitions."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import orca
from .project import confined, digest, filehash, load, source_identity


def now():
    return datetime.now(UTC).isoformat()


def build_cad(root, manifest, output):
    request = {
        "root": str(root),
        "output": str(output),
        "source": manifest.spec.source.model_dump(),
        "objects": [o.model_dump() for p in manifest.spec.plates for o in p.objects],
    }
    request_path = output / "request.json"
    request_path.write_text(json.dumps(request))
    runner = Path(__file__).with_name("cad_runner.py")
    command = (
        [os.environ.get("FREECAD_CMD", "FreeCADCmd"), str(runner)]
        if manifest.spec.source.engine == "freecad"
        else [os.environ.get("CADQUERY_PYTHON", sys.executable), str(runner)]
    )
    result = subprocess.run(
        command,
        env={**os.environ, "MANUFACTURING_BUILD_REQUEST": str(request_path)},
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=root,
    )
    (output / "build.log").write_text(result.stdout + result.stderr)
    request_path.unlink()
    if result.returncode or not (output / "validation.json").exists():
        raise ValueError("CAD build failed; inspect build.log and executable configuration")


class Controller:
    def __init__(
        self,
        storage: Path,
        projects: Path,
        *,
        builder=build_cad,
        slicer=orca.slice_plate,
        version=orca.version,
    ):
        self.storage, self.projects = storage.resolve(), projects.resolve()
        self.builder, self.slicer, self.version = builder, slicer, version
        self.lock = threading.Lock()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.db = self.storage / "registry.sqlite"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY, project TEXT NOT NULL, plate TEXT NOT NULL,
              fingerprint TEXT NOT NULL, revision INTEGER NOT NULL,
              path TEXT NOT NULL, checksum TEXT NOT NULL, receipt TEXT NOT NULL,
              staged INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
              UNIQUE(project,plate,fingerprint), UNIQUE(project,revision));
            CREATE TABLE IF NOT EXISTS desired (
              project TEXT, plate TEXT, fingerprint TEXT, PRIMARY KEY(project,plate));
            CREATE TABLE IF NOT EXISTS automation (
              project TEXT PRIMARY KEY, signature TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS worker (
              id INTEGER PRIMARY KEY, last_seen TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, project TEXT NOT NULL, plate TEXT NOT NULL,
              action TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def roots(self):
        candidates = [self.projects] + sorted(
            p.parent for p in self.projects.glob("*/manufacturing.yaml")
        )
        return [p for p in candidates if (p / "manufacturing.yaml").is_file()]

    def project(self, name):
        matches = []
        for root in self.roots():
            if not root.resolve().is_relative_to(self.projects):
                continue
            try:
                manifest = load(root)
            except Exception:
                continue
            if manifest.metadata.name == name:
                matches.append((root, manifest))
        if len(matches) != 1:
            raise ValueError("Project must resolve to exactly one configured manifest")
        return matches[0]

    def discover(self):
        result = []
        for root in self.roots():
            try:
                m = load(root)
                result.append(
                    {
                        "name": m.metadata.name,
                        "repository": m.metadata.repository,
                        "plates": [p.model_dump() for p in m.spec.plates],
                        "source": source_identity(root, m),
                        "geometryAvailable": self.geometry(root, m).is_dir(),
                        "automation": m.spec.deployment.model_dump(),
                        "error": None,
                    }
                )
            except Exception as exc:
                result.append({"name": root.name, "error": str(exc), "plates": []})
        return result

    def inputs(self, root, m):
        return digest(
            {
                "source": source_identity(root, m),
                "manifest": m.model_dump(),
                "project": filehash(confined(root, m.spec.slicer.project)),
                "profiles": [filehash(confined(root, p)) for p in m.spec.slicer.profiles],
            }
        )

    def geometry(self, root, m):
        return (
            confined(self.storage, m.spec.build.outputDirectory)
            / m.metadata.name
            / digest([source_identity(root, m), [p.model_dump() for p in m.spec.plates]])
        )

    def validate_geometry(self, path, m):
        report = json.loads((path / "validation.json").read_text())
        for plate in m.spec.plates:
            for obj in plate.objects:
                if report[obj.id].get("valid") is not True or report[obj.id].get("solids") != 1:
                    raise ValueError("Geometry failed validation")
                orca.stl_mesh(path / obj.export)
        return report

    def build(self, root, m):
        path = self.geometry(root, m)
        if path.exists():
            self.validate_geometry(path, m)
            hashes = json.loads((path / "hashes.json").read_text())
            if any(filehash(path / n) != h for n, h in hashes.items()):
                raise ValueError("Cached geometry checksum mismatch")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name("." + uuid.uuid4().hex)
        temp.mkdir()
        before = source_identity(root, m)
        try:
            self.builder(root, m, temp)
            self.validate_geometry(temp, m)
            if before != source_identity(root, m):
                raise ValueError("CAD inputs changed during build")
            (temp / "hashes.json").write_text(
                json.dumps({p.name: filehash(p) for p in temp.iterdir() if p.is_file()})
            )
            temp.rename(path)
        except Exception:
            # Keep one failed build log for diagnosis, never register it as valid geometry.
            failed = path.parent / "last-failed"
            if failed.exists():
                shutil.rmtree(failed)
            temp.rename(failed)
            raise
        return path

    def plate(self, m, name):
        return next(p for p in m.spec.plates if p.name == name)

    def list_artifacts(self, project=None):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM artifacts "
                + ("WHERE project=? " if project else "")
                + "ORDER BY created DESC",
                (project,) if project else (),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["receipt"] = json.loads(value["receipt"])
            value["fresh"] = self.fresh(value)
            value["revision"] = f"V{value['revision']:03}"
            value.pop("path")
            result.append(value)
        return result

    def artifact(self, artifact_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown artifact")
        return dict(row)

    def fresh(self, row):
        try:
            root, m = self.project(row["project"])
            receipt = (
                json.loads(row["receipt"]) if isinstance(row["receipt"], str) else row["receipt"]
            )
            with self.connect() as db:
                desired = db.execute(
                    "SELECT fingerprint FROM desired WHERE project=? AND plate=?",
                    (row["project"], row["plate"]),
                ).fetchone()
            return receipt["inputs"] == self.inputs(root, m) and (
                desired is None or desired[0] == receipt["fingerprint"]
            )
        except Exception:
            return False

    def verified(self, artifact_id, *, staged=False):
        row = self.artifact(artifact_id)
        if not self.fresh(row):
            raise ValueError("Artifact is stale; reconcile source before deployment")
        if staged and not row["staged"]:
            raise ValueError("Artifact must be explicitly staged first")
        path = confined(self.storage, row["path"])
        receipt = json.loads(row["receipt"])
        for name, checksum in receipt["files"].items():
            if filehash(confined(path, name)) != checksum:
                raise ValueError("Artifact integrity verification failed")
        return row, path

    def stage(self, artifact_id):
        row, path = self.verified(artifact_id)
        _, m = self.project(row["project"])
        if not m.spec.staging.enabled:
            raise ValueError("Staging disabled by manifest")
        with self.connect() as db:
            db.execute("UPDATE artifacts SET staged=1 WHERE id=?", (row["id"],))
        return {"artifact": row["id"], "status": "staged", "checksum": row["checksum"]}

    @contextmanager
    def exclusive(self):
        # Cross-process lock: controller, watcher and worker can share a registry safely.
        import fcntl

        with self.lock, (self.storage / ".build.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def execute(self, project, plate_name, action, progress=None):
        progress = progress or (lambda phase: None)
        if action not in {
            "build",
            "slice",
            "build-slice",
            "slice-stage",
            "build-slice-stage",
            "reconcile",
        }:
            raise ValueError("Unknown manufacturing action")
        with self.exclusive():
            root, m = self.project(project)
            plate = self.plate(m, plate_name)
            if action in {"slice-stage", "build-slice-stage"} and not m.spec.staging.enabled:
                raise ValueError("Staging disabled by manifest")
            before = self.inputs(root, m)
            geometry = self.geometry(root, m)
            if action in {"build", "build-slice", "build-slice-stage", "reconcile"}:
                progress("Building and validating geometry")
                geometry = self.build(root, m)
            if action == "build":
                return {"status": "validated", "source": source_identity(root, m)}
            if not geometry.exists():
                raise ValueError("Geometry is stale or missing; rebuild geometry first")
            progress("Checking geometry")
            self.validate_geometry(geometry, m)
            hashes = json.loads((geometry / "hashes.json").read_text())
            if any(filehash(geometry / n) != h for n, h in hashes.items()):
                raise ValueError("Geometry integrity verification failed")
            executable = os.environ.get("ORCASLICER_PATH", "orca-slicer")
            slicer_version = self.version(executable)
            project3mf = orca.OrcaProject(confined(root, m.spec.slicer.project))
            profiles = [confined(root, p) for p in m.spec.slicer.profiles]
            fingerprint = project3mf.fingerprint(
                plate, geometry, profiles, slicer_version, m.spec.source.parameters
            )
            with self.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO desired VALUES(?,?,?)",
                    (project, plate_name, fingerprint),
                )
            with self.connect() as db:
                cached = db.execute(
                    "SELECT id FROM artifacts WHERE project=? AND plate=? "
                    "AND json_extract(receipt, '$.fingerprint')=? ORDER BY created DESC LIMIT 1",
                    (project, plate_name, fingerprint),
                ).fetchone()
            if cached:
                row = self.artifact(cached["id"])
                # Geometry-equivalent source edits retain the immutable G-code; create a
                # fresh provenance revision instead of mutating its original receipt.
                if self.fresh(row):
                    progress("Verifying cached artifact")
                    self.verified(row["id"])
                    if action in {"slice-stage", "build-slice-stage"}:
                        progress("Staging verified artifact")
                        self.stage(row["id"])
                    return {"artifact": row["id"], "cached": True, "status": "ready"}
                # Old source fingerprint is equivalent; verify bytes and reuse G-code below.
                old = confined(self.storage, row["path"])
                old_receipt = json.loads(row["receipt"])
                if any(filehash(old / n) != h for n, h in old_receipt["files"].items()):
                    raise ValueError("Cached artifact corrupted")
            else:
                old = None
            artifact_id = uuid.uuid4().hex
            destination = self.storage / "artifacts" / project / artifact_id
            destination.mkdir(parents=True)
            try:
                progress("Updating slicer project")
                reconciled = destination / "project.3mf"
                changed = project3mf.reconcile(plate, geometry, reconciled)
                if old:
                    shutil.copyfile(old / plate.output, destination / plate.output)
                else:
                    index, _ = project3mf.plate(plate)
                    progress("Slicing plate")
                    gcode = self.slicer(executable, reconciled, index, destination, profiles)
                    if gcode != destination / plate.output:
                        gcode.rename(destination / plate.output)
                for obj in plate.objects:
                    shutil.copyfile(geometry / obj.export, destination / obj.export)
                validation = self.validate_geometry(geometry, m)
                (destination / "validation.json").write_text(json.dumps(validation, indent=2))
                if before != self.inputs(root, m):
                    raise ValueError("Inputs changed during slicing; artifact not registered")
                import re

                from .preview import embed_png, render

                progress("Generating preview and checksums")
                render(project3mf, plate, geometry, destination / "preview.png")
                embed_png(destination / plate.output, destination / "preview.png")
                from ..gcode_thumbnail import extract_gcode_thumbnail_details
                from ..local_gcode_index import _header_metadata

                with (destination / plate.output).open("rb") as stream:
                    header = stream.read(256 * 1024)
                    stream.seek(max(0, (destination / plate.output).stat().st_size - 256 * 1024))
                    header += stream.read(256 * 1024)
                thumbnail = extract_gcode_thumbnail_details(header)
                previews = {
                    "embedded": thumbnail is not None,
                    "tft": "markers_present_unverified"
                    if re.search(rb"(?mi)^;?(?:simage|gimage):", header)
                    else "missing",
                }
                if thumbnail:
                    previews.update(
                        {
                            "width": thumbnail.width,
                            "height": thumbnail.height,
                            "mime": thumbnail.mime_type,
                        }
                    )
                checksum = filehash(destination / plate.output)
                receipt = {
                    "inputs": before,
                    "source": source_identity(root, m),
                    "parameters": m.spec.source.parameters,
                    "slicer": slicer_version,
                    "fingerprint": fingerprint,
                    "changedMeshEntries": changed,
                    "output": plate.output,
                    "manifest": m.model_dump(),
                    "reusedFrom": row["id"] if old else None,
                    "previews": previews,
                    "slicingMetadata": _header_metadata(header),
                    "files": {p.name: filehash(p) for p in destination.iterdir() if p.is_file()},
                }
                (destination / "provenance.json").write_text(json.dumps(receipt, indent=2))
                (destination / (plate.output + ".sha256")).write_text(
                    checksum + "  " + plate.output + "\n"
                )
                with self.connect() as db:
                    revision = db.execute(
                        "SELECT COALESCE(MAX(revision),0)+1 FROM artifacts WHERE project=?",
                        (project,),
                    ).fetchone()[0]
                    # Multiple provenance revisions can share a geometry fingerprint.
                    db.execute(
                        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            artifact_id,
                            project,
                            plate_name,
                            digest([fingerprint, before]),
                            revision,
                            str(destination.relative_to(self.storage)),
                            checksum,
                            json.dumps(receipt),
                            0,
                            now(),
                        ),
                    )
                if action in {"slice-stage", "build-slice-stage"}:
                    progress("Staging verified artifact")
                    self.stage(artifact_id)
                return {"artifact": artifact_id, "cached": bool(old), "status": "ready"}
            except Exception:
                shutil.rmtree(destination)
                raise

    def enqueue(self, project, plate, action):
        _, manifest = self.project(project)
        self.plate(manifest, plate)
        if action not in {
            "build",
            "slice",
            "build-slice",
            "slice-stage",
            "build-slice-stage",
            "reconcile",
        }:
            raise ValueError("Unsupported job action")
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                (identifier, project, plate, action, "queued", None, now()),
            )
        return identifier

    def jobs(self):
        with self.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")
            ]

    def work_once(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
        try:

            def progress(phase):
                with self.connect() as status_db:
                    status_db.execute(
                        "UPDATE jobs SET result=? WHERE id=?",
                        (json.dumps({"phase": phase}), row["id"]),
                    )

            result = self.execute(row["project"], row["plate"], row["action"], progress=progress)
            status = "succeeded"
        except Exception as exc:
            result, status = {"error": str(exc)}, "failed"
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=? WHERE id=?",
                (status, json.dumps(result), row["id"]),
            )
        return True

    def heartbeat(self, state="watching"):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO worker VALUES(1,?,?)", (now(), state))

    def worker_status(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM worker WHERE id=1").fetchone()
        if row is None:
            return {"online": False, "state": "not_connected", "last_seen": None}
        age = (datetime.now(UTC) - datetime.fromisoformat(row["last_seen"])).total_seconds()
        return {
            "online": 0 <= age < 20 and row["state"] != "stopped",
            "state": row["state"],
            "last_seen": row["last_seen"],
        }
