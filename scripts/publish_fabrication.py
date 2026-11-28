#!/usr/bin/env python3
"""Resumable private publication through the canonical archive; never stages or prints."""

import argparse
import base64
import hashlib
import json
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path

BRIDGE = r"""
import http.client,json,shlex,sys
from pathlib import Path
try:
 config=json.loads(sys.argv[1]);token=None
 for line in Path('/home/cdaprod/elegoo-neptune-2-3d-printer/.env').read_text().splitlines():
  if line.startswith('NEPTUNE_OBSERVATORY_TOKEN='):
   token=shlex.split(line.split('=',1)[1],comments=True)[0]
 if not token:raise ValueError('Application credential unavailable')
 headers={'Authorization':'Bearer '+token,'Content-Length':str(config['length']),
          **config['headers']}
 connection=http.client.HTTPConnection('127.0.0.1',8000,timeout=300)
 connection.request(config['method'],config['path'],
                    body=sys.stdin.buffer if config['length'] else None,headers=headers)
 response=connection.getresponse();raw=response.read(4*1024*1024+1)
 if response.status>=400:
  raise ValueError('Application HTTP '+str(response.status)+': '+raw[:500].decode(errors='replace'))
 if len(raw)>4*1024*1024:raise ValueError('Response exceeds bound')
 if config.get('binary'):
  import base64,hashlib
  print(json.dumps({'base64':base64.b64encode(raw).decode(),
      'sha256':hashlib.sha256(raw).hexdigest(),
      'content_type':response.getheader('Content-Type')}))
 else:print(raw.decode())
except (OSError, ValueError, KeyError, IndexError, http.client.HTTPException) as error:
 print(json.dumps({'publication_error':str(error)}));sys.exit(1)
"""


class SshAPI:
    def __init__(self, host="neptune"):
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
            raise ValueError("Use an SSH alias")
        self.host = host

    def request(self, method, path, stream=None, length=0, headers=None, binary=False):
        if not path.startswith("/api/"):
            raise ValueError("Application API path required")
        config = {
            "method": method,
            "path": path,
            "length": length,
            "headers": headers or {},
            "binary": binary,
        }
        command = "python3 -c " + shlex.quote(BRIDGE) + " " + shlex.quote(json.dumps(config))
        result = subprocess.run(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=60",
                "-o",
                "ControlPath=~/.ssh/neptune-publication-%C",
                self.host,
                command,
            ],
            stdin=stream,
            capture_output=True,
            check=False,
            timeout=360,
        )
        if result.returncode:
            raise ValueError(
                "SSH publication failed: " + result.stdout.decode(errors="replace")[-700:]
            )
        return json.loads(result.stdout)

    def json(self, method, path, data=None):
        raw = json.dumps(data).encode() if data is not None else b""
        with __import__("tempfile").TemporaryFile() as stream:
            stream.write(raw)
            stream.seek(0)
            return self.request(
                method, path, stream, len(raw), {"Content-Type": "application/json"}
            )

    def download(self, path, destination):
        value = self.request("GET", path, binary=True)
        raw = base64.b64decode(value["base64"])
        if hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("Download integrity failure")
        Path(destination).write_bytes(raw)
        return {key: value[key] for key in ("sha256", "content_type")}

    def upload(self, source, kind, uri=None, filename=None):
        from urllib.parse import quote, urlencode

        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError("Regular source required")
        with source.open("rb") as stream:
            before = source.stat()
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            result = self.request(
                "POST",
                "/api/fabrication/ingest?"
                + urlencode({"filename": filename or source.name, "artifact_kind": kind}),
                stream,
                before.st_size,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Content-SHA256": sha,
                    "X-Artifact-Provider": "orca_slicer",
                    "X-Artifact-Source": quote(
                        uri or "macbook:" + source.absolute().as_uri(), safe=":/#@%"
                    ),
                },
            )
            after = source.stat()
        if result.get("sha256") != sha or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        ) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("Source or receipt integrity failure")
        return result


def run(args):
    from mks_wifi_bridge.slicer_library import extract_project

    root = args.state.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    db = sqlite3.connect(root / "publication.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE IF NOT EXISTS submissions(
        path TEXT PRIMARY KEY,signature TEXT NOT NULL,sha256 TEXT,state TEXT NOT NULL,
        receipt TEXT,error TEXT,updated_at REAL NOT NULL);""")
    report = json.loads(args.inventory.read_text())
    files = [
        row
        for row in report["files"]
        if row.get("kind") == "slicer_project"
        and not any(
            part.lower() in {"tests", "test", "fixtures"} for part in Path(row["path"]).parts
        )
    ]
    files.sort(key=lambda row: ("display-mount" not in row["path"].lower(), row["path"]))
    if args.limit:
        files = files[: args.limit]
    api = SshAPI(args.host)
    completed = failed = 0
    for index, item in enumerate(files):
        source = Path(item["path"])
        signature = ""
        sha = None
        receipt = None
        try:
            info = source.stat()
            signature = json.dumps(
                [
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ]
            )
            old = db.execute("SELECT * FROM submissions WHERE path=?", (str(source),)).fetchone()
            if old and old["signature"] == signature and old["state"] == "complete":
                completed += 1
                continue
            if (
                old
                and old["signature"] == signature
                and old["state"] == "archived_pending_registration"
            ):
                if args.register:
                    manifest = json.loads(
                        (root / "derivatives" / old["sha256"] / "published.json").read_text()
                    )
                    manifest["source_observation"] = {
                        **item,
                        "source_host": "macbook",
                        "mtime_ns": info.st_mtime_ns,
                        "ctime_ns": info.st_ctime_ns,
                    }
                    registration = api.json(
                        "POST",
                        "/api/fabrication/slicer-projects/" + old["sha256"] + "/register",
                        manifest,
                    )
                    receipt = json.loads(old["receipt"])
                    receipt["registration"] = registration
                    db.execute(
                        "UPDATE submissions SET state='complete',receipt=?,updated_at=? "
                        "WHERE path=?",
                        (json.dumps(receipt), time.time(), str(source)),
                    )
                    db.commit()
                completed += 1
                continue
            if not args.apply:
                print(
                    json.dumps({"state": "dry_run", "path": str(source), "size": info.st_size}),
                    flush=True,
                )
                continue
            db.execute(
                "INSERT OR REPLACE INTO submissions VALUES(?,?,?,?,?,?,?)",
                (str(source), signature, None, "processing", None, None, time.time()),
            )
            db.commit()
            with source.open("rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            destination = root / "derivatives" / sha
            receipt = api.upload(source, "slicer_project")
            manifest = extract_project(source, destination)
            for body in manifest["bodies"]:
                file = destination / body["geometry_file"]
                name = Path(body["name"]).name.replace("..", "_")[:140]
                if not name.lower().endswith(".stl"):
                    name += ".stl"
                body["sha256"] = api.upload(
                    file,
                    "manufacturing_geometry",
                    "derived:" + sha + "#" + body["resource"] + ":" + body["object_id"],
                    filename=name,
                )["sha256"]
            for plate in manifest["plates"]:
                if plate.get("geometry_file"):
                    plate["sha256"] = api.upload(
                        destination / plate["geometry_file"],
                        "manufacturing_geometry",
                        "derived:" + sha + "#plate:" + str(plate["id"]),
                        filename="Plate-" + str(plate["id"]) + ".stl",
                    )["sha256"]
            manifest["source_observation"] = {
                **item,
                "source_host": "macbook",
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            }
            (destination / "published.json").write_text(json.dumps(manifest))
            if args.register:
                registration = api.json(
                    "POST",
                    "/api/fabrication/slicer-projects/" + sha + "/register",
                    manifest,
                )
                receipt["registration"] = registration
            db.execute(
                "UPDATE submissions SET sha256=?,state=?,receipt=?,error=NULL,updated_at=? "
                "WHERE path=?",
                (
                    sha,
                    "complete" if args.register else "archived_pending_registration",
                    json.dumps(receipt),
                    time.time(),
                    str(source),
                ),
            )
            db.commit()
            completed += 1
            print(
                json.dumps(
                    {
                        "index": index + 1,
                        "total": len(files),
                        "state": "complete" if args.register else "archived_pending_registration",
                        "file": source.name,
                        "sha256": sha,
                        "bodies": len(manifest["bodies"]),
                        "plates": len(manifest["plates"]),
                    }
                ),
                flush=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failed += 1
            db.execute(
                "INSERT OR REPLACE INTO submissions VALUES(?,?,?,?,?,?,?)",
                (
                    str(source),
                    signature,
                    sha,
                    "archived_preview_failed" if receipt else "failed",
                    json.dumps(receipt) if receipt else None,
                    str(error)[:1000],
                    time.time(),
                ),
            )
            db.commit()
            print(
                json.dumps(
                    {
                        "index": index + 1,
                        "state": "failed",
                        "file": source.name,
                        "error": str(error)[:400],
                    }
                ),
                flush=True,
            )
    summary = {
        "scope": "orca_3mf",
        "discovered": len(files),
        "processed": completed,
        "failed": failed,
        "apply": args.apply,
        "register": args.register,
    }
    (root / "progress.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home()
        / "Library/Application Support/NeptuneObservatory/commissioning/orca-publication",
    )
    parser.add_argument("--host", default="neptune")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
