"""Pi deployment-agent cache. Pull immutable artifacts; never invoke CAD or a printer."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener
from zipfile import ZIP_DEFLATED, ZipFile

from ..octoprint_backend import _NoRedirects
from .controller import Controller
from .project import Manifest, confined, filehash, load

MAX_BUNDLE = 512 * 1024 * 1024


def snapshot(root, manifest):
    names = {
        "manufacturing.yaml",
        manifest.spec.source.document,
        manifest.spec.slicer.project,
        *manifest.spec.source.dependencies,
        *manifest.spec.slicer.profiles,
    }
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in sorted(names):
            archive.write(confined(root, name), name)
    return output.getvalue()


def unpack_snapshot(raw, target):
    with ZipFile(io.BytesIO(raw)) as archive:
        if sum(i.file_size for i in archive.infolist()) > MAX_BUNDLE:
            raise ValueError("Project snapshot exceeds limit")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate source snapshot entries")
        manifest = Manifest.model_validate(
            __import__("yaml").safe_load(archive.read("manufacturing.yaml"))
        )
        expected = {
            "manufacturing.yaml",
            manifest.spec.source.document,
            manifest.spec.slicer.project,
            *manifest.spec.source.dependencies,
            *manifest.spec.slicer.profiles,
        }
        if set(names) != expected:
            raise ValueError("Snapshot contains undeclared inputs")
        target.mkdir(parents=True)
        for name in names:
            path = confined(target, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))
    load(target)
    return manifest


class BuildNode:
    def __init__(self, url=None, token=None):
        self.url = (url or os.environ["NEPTUNE_BUILD_NODE_URL"]).rstrip("/")
        self.token = token or os.environ["NEPTUNE_BUILD_NODE_TOKEN"]
        parts = urlsplit(self.url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError("Invalid build-node origin")
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ValueError("Build-node URL must be an origin")
        self.opener = build_opener(_NoRedirects())

    def request(self, path, body=None, *, binary=False, timeout=30):
        request = Request(
            self.url + "/api/manufacturing" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BUNDLE + 1)
            if len(raw) > MAX_BUNDLE:
                raise ValueError("Build-node response exceeds limit")
            return raw if binary else json.loads(raw)
        except Exception:
            raise ValueError(
                "Build node unavailable or rejected request; cached artifacts retained"
            ) from None


def sync(controller, upstream):
    projects = upstream.request("/projects")
    for item in projects:
        if item.get("error"):
            continue
        project = item["name"]
        if not __import__("re").fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", project):
            raise ValueError("Invalid upstream project name")
        raw = upstream.request("/projects/" + quote(project, safe="") + "/snapshot", binary=True)
        temporary = controller.projects / (".snapshot-" + uuid.uuid4().hex)
        try:
            manifest = unpack_snapshot(raw, temporary)
            if manifest.metadata.name != project:
                raise ValueError("Project snapshot identity mismatch")
            final = controller.projects / project
            prior = controller.projects / (".previous-" + uuid.uuid4().hex)
            if final.exists():
                final.rename(prior)
            temporary.rename(final)
            if prior.exists():
                shutil.rmtree(prior)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    rows = upstream.request("/artifacts")
    desired = {}
    for row in rows:
        key = (row["project"], row["plate"])
        if row["fresh"]:
            desired[key] = row["receipt"]["fingerprint"]
        else:
            desired.setdefault(key, "upstream-invalidated")
    with controller.connect() as db:
        for (project, plate), fingerprint in desired.items():
            db.execute(
                "INSERT OR REPLACE INTO desired VALUES(?,?,?)", (project, plate, fingerprint)
            )
    count = 0
    for row in reversed(rows):
        if not row["staged"] or not row["fresh"]:
            continue
        project = row["project"]
        root, m = controller.project(project)
        if controller.inputs(root, m) != row["receipt"]["inputs"]:
            # Source may have changed between list and snapshot; never import it as fresh.
            continue
        identifier = row["id"]
        if not identifier.isalnum() or len(identifier) != 32:
            raise ValueError("Invalid upstream artifact ID")
        destination = controller.storage / "artifacts" / project / identifier
        if not destination.exists():
            temporary = destination.with_name("." + uuid.uuid4().hex)
            temporary.mkdir(parents=True)
            try:
                for name, checksum in row["receipt"]["files"].items():
                    path = confined(temporary, name)
                    if path.name != name:
                        raise ValueError("Artifact filename must be plain")
                    path.write_bytes(
                        upstream.request(
                            f"/artifacts/{identifier}/files/" + quote(name, safe=""), binary=True
                        )
                    )
                    if filehash(path) != checksum:
                        raise ValueError("Artifact download checksum mismatch")
                (temporary / "provenance.json").write_text(json.dumps(row["receipt"], indent=2))
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        revision = int(row["revision"][1:])
        with controller.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO desired VALUES(?,?,?)",
                (project, row["plate"], row["receipt"]["fingerprint"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    project,
                    row["plate"],
                    row["fingerprint"],
                    revision,
                    str(destination.relative_to(controller.storage)),
                    row["checksum"],
                    json.dumps(row["receipt"]),
                    1,
                    row["created"],
                ),
            )
        controller.verified(identifier, staged=True)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    storage = Path(os.environ.get("NEPTUNE_MANUFACTURING_STORAGE", "/data/manufacturing"))
    # Agent-owned source mirrors, never authoritative CAD workspaces.
    projects = Path(
        os.environ.get("NEPTUNE_MANUFACTURING_PROJECTS", "/data/manufacturing/projects")
    )
    projects.mkdir(parents=True, exist_ok=True)
    controller = Controller(storage, projects)
    upstream = BuildNode()
    while True:
        try:
            print(json.dumps({"cached": sync(controller, upstream)}), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "offline_or_failed", "error": str(exc)}), flush=True)
        if args.once:
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
