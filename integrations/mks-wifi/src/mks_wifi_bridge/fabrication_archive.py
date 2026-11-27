"""Durable artifact identities adjoining the existing execution ledger.

No printer transport. Original bytes are never evicted with preview caches.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .gcode_fabrication import MAX_SOURCE_BYTES, FabricationPreviewService


def now():
    return datetime.now(UTC).isoformat()


class FabricationArchive:
    def __init__(self, timeline):
        self.timeline = timeline
        self.root = Path(timeline.path).parent / "fabrication-archive"
        self.root.mkdir(parents=True, exist_ok=True)
        self.previews = FabricationPreviewService(self.root, self.root / "derivatives")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS execution_artifacts(
                    execution_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fabrication_schema(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO fabrication_schema VALUES(1);
                CREATE TABLE IF NOT EXISTS fabrication_artifacts(
                    sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                    ingested_at TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_locations(
                    provider TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    observed_at TEXT NOT NULL, PRIMARY KEY(provider,path,sha256),
                    FOREIGN KEY(sha256) REFERENCES fabrication_artifacts(sha256));
                CREATE TABLE IF NOT EXISTS artifact_provenance(
                    sha256 TEXT NOT NULL, provider TEXT NOT NULL, source_uri TEXT NOT NULL,
                    filename TEXT NOT NULL, artifact_kind TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(sha256,provider,source_uri,filename),
                    FOREIGN KEY(sha256) REFERENCES fabrication_artifacts(sha256));
                INSERT OR IGNORE INTO fabrication_schema VALUES(2);
                CREATE TABLE IF NOT EXISTS artifact_relationships(
                    parent_sha256 TEXT NOT NULL REFERENCES fabrication_artifacts(sha256),
                    child_sha256 TEXT NOT NULL REFERENCES fabrication_artifacts(sha256),
                    relationship TEXT NOT NULL CHECK(relationship IN
                    ('sliced_from','derived_from','observes','configured_by')),
                    PRIMARY KEY(parent_sha256,child_sha256,relationship));
                CREATE TABLE IF NOT EXISTS artifact_stages(
                    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    source_execution_id TEXT, status TEXT NOT NULL DEFAULT 'staged',
                    FOREIGN KEY(sha256) REFERENCES fabrication_artifacts(sha256));
            """)

    def reconcile(self, indexer, limit=4):
        """Bounded automatic ingestion; missing sources never delete the archive."""
        root = indexer.source_dir
        if root is None:
            return 0
        index = indexer.scan()
        count = 0
        for item in index.get("files", []):
            if count >= limit:
                break
            if not item.get("source_present"):
                continue
            path = (root / item["relative_path"]).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                continue
            signature = str(path.stat().st_mtime_ns) + ":" + str(path.stat().st_size)
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS archive_ingestion "
                    "(path TEXT PRIMARY KEY, signature TEXT NOT NULL)"
                )
                previous = db.execute(
                    "SELECT signature FROM archive_ingestion WHERE path=?", (str(path),)
                ).fetchone()
            if previous and previous[0] == signature:
                continue
            self.ingest(path)
            with self.db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO archive_ingestion VALUES(?,?)", (str(path), signature)
                )
            count += 1
        return count

    def db(self):
        db = sqlite3.connect(self.timeline.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def ingest(self, source, provider="local", original_filename=None):
        from .fabrication_ingestion import ArtifactSubmission, FabricationIngestion

        source = Path(source)
        return FabricationIngestion(self).submit_path(
            source, ArtifactSubmission(original_filename or source.name, provider, str(source))
        )

    def persist_stream(self, stream, submission):
        digest = hashlib.sha256()
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".ingest-")
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := stream.read(65536):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise ValueError("Artifact exceeds limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise ValueError("Empty artifacts are not accepted")
            sha = digest.hexdigest()
            target = self.root / (sha + ".gcode")
            os.replace(temporary, target)
            with self.db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO fabrication_artifacts VALUES(?,?,?,?,?)",
                    (
                        sha,
                        submission.filename,
                        size,
                        now(),
                        json.dumps({"provenance": submission.provider}),
                    ),
                )
                db.execute(
                    "INSERT OR REPLACE INTO artifact_locations VALUES(?,?,?,?)",
                    (submission.provider, submission.source_uri, sha, now()),
                )
                db.execute(
                    "INSERT INTO artifact_provenance VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(sha256,provider,source_uri,filename) DO UPDATE SET "
                    "last_seen=excluded.last_seen",
                    (
                        sha,
                        submission.provider,
                        submission.source_uri,
                        submission.filename,
                        submission.artifact_kind,
                        now(),
                        now(),
                    ),
                )
            return sha
        finally:
            Path(temporary).unlink(missing_ok=True)

    def verified(self, sha):
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid artifact identity")
        path = self.root / (sha + ".gcode")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                raise ValueError("Archived artifact failed integrity verification")
        return path

    def collection(self, kind, before=None, limit=40):
        if kind not in {"history", "unprinted"} or not 1 <= limit <= 100:
            raise ValueError("Invalid collection or page size")
        # Keyset cursors include a unique identity; insertions cannot shift old pages.
        cursor = json.loads(before) if before else ["9999", "~"]
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO execution_artifacts "
                "SELECT p.id,g.sha256 FROM print_sessions p JOIN gcode_files g ON p.file_id=g.id"
            )
            if kind == "history":
                rows = db.execute(
                    """SELECT p.*, e.sha256, a.filename AS archived_filename
                    FROM print_sessions p LEFT JOIN execution_artifacts e ON p.id=e.execution_id
                    LEFT JOIN fabrication_artifacts a ON a.sha256=e.sha256
                    WHERE (p.started_at,p.id)<(?,?) ORDER BY p.started_at DESC,p.id DESC LIMIT ?""",
                    (*cursor, limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT a.*, a.sha256 AS id FROM fabrication_artifacts a
                    WHERE NOT EXISTS(SELECT 1 FROM execution_artifacts e WHERE e.sha256=a.sha256)
                    AND (a.ingested_at,a.sha256)<(?,?)
                    ORDER BY a.ingested_at DESC,a.sha256 DESC LIMIT ?""",
                    (*cursor, limit + 1),
                ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            item["bytes_available"] = bool(
                item.get("sha256") and (self.root / (item["sha256"] + ".gcode")).is_file()
            )
            item["geometry_provenance"] = (
                "approximate_deposited_filament" if item["bytes_available"] else "unavailable"
            )
        last = items[-1] if items else None
        return {
            "schema_version": 1,
            "items": items,
            "next": json.dumps([last.get("started_at") or last["ingested_at"], last["id"]])
            if last and len(rows) > limit
            else None,
        }

    def stage_again(self, execution_id):
        with self.db() as db:
            row = db.execute(
                "SELECT COALESCE(e.sha256,g.sha256) FROM print_sessions p "
                "LEFT JOIN execution_artifacts e ON e.execution_id=p.id "
                "LEFT JOIN gcode_files g ON p.file_id=g.id WHERE p.id=?",
                (execution_id,),
            ).fetchone()
            if row is None or row[0] is None:
                raise ValueError("This execution has no verified artifact association")
            self.verified(row[0])
            identity = uuid.uuid4().hex
            db.execute(
                "INSERT INTO artifact_stages VALUES(?,?,?,?,?)",
                (
                    identity,
                    row[0],
                    now(),
                    execution_id,
                    "staged_requires_compatibility_confirmation",
                ),
            )
            return {
                "id": identity,
                "sha256": row[0],
                "status": "staged_requires_compatibility_confirmation",
                "physical_execution": False,
            }
