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
                INSERT OR IGNORE INTO fabrication_schema VALUES(3);
                INSERT OR IGNORE INTO fabrication_schema VALUES(4);
                CREATE TABLE IF NOT EXISTS execution_identity(
                    execution_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                    evidence TEXT NOT NULL, transport TEXT NOT NULL,
                    FOREIGN KEY(sha256) REFERENCES fabrication_artifacts(sha256));
                CREATE TABLE IF NOT EXISTS artifact_discoveries(
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, path TEXT NOT NULL,
                    filename TEXT NOT NULL, size INTEGER, first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL, available INTEGER NOT NULL, sha256 TEXT);
                CREATE TABLE IF NOT EXISTS artifact_derivatives(
                    sha256 TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
                    status TEXT NOT NULL, PRIMARY KEY(sha256,kind,version));
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
        try:
            initial = os.fstat(stream.fileno())
        except (AttributeError, OSError):
            initial = None
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
            if initial is not None:
                final = os.fstat(stream.fileno())
                if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                    raise ValueError("Source changed during ingestion")
            if size == 0:
                raise ValueError("Empty artifacts are not accepted")
            sha = digest.hexdigest()
            if submission.expected_sha256 is not None and sha != submission.expected_sha256:
                raise ValueError("Artifact failed expected SHA-256 verification")
            target = self.root / (sha + ".gcode")
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.verified(sha)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
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

    def link_execution(self, execution_id, sha, evidence, transport):
        """Trusted reconciler entry point; never called from name/size matching."""
        if evidence not in {
            "operator_supplied",
            "octoprint_verified_bytes",
            "native_sd_verified_snapshot",
        }:
            raise ValueError("Explicit content identity evidence required")
        if transport not in {"native_sd", "octoprint_usb", "unknown"}:
            raise ValueError("Unknown execution transport")
        self.verified(sha)
        with self.db() as db:
            if not db.execute(
                "SELECT 1 FROM print_sessions WHERE id=?", (execution_id,)
            ).fetchone():
                raise ValueError("Unknown execution")
            previous = db.execute(
                "SELECT sha256 FROM execution_identity WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if previous and previous[0] != sha:
                raise ValueError("Execution identity cannot be silently replaced")
            db.execute(
                "INSERT OR IGNORE INTO execution_identity VALUES(?,?,?,?)",
                (execution_id, sha, evidence, transport),
            )

    def observe_discovery(self, entries, provider="printer_sd", available=True):
        """Directory metadata is never content identity. Only verified snapshots may link bytes."""
        from .factory_usb import catalog_visible

        with self.db() as db:
            db.execute("UPDATE artifact_discoveries SET available=0 WHERE provider=?", (provider,))
            for entry in entries:
                path = entry.get("name", "")
                if entry.get("kind", "file") != "file" or not catalog_visible(path):
                    continue
                identity = hashlib.sha256((provider + "\0" + path).encode()).hexdigest()
                sha = None
                candidate = entry.get("verified_sha256")
                if entry.get("identity_evidence") == "verified_content_snapshot" and candidate:
                    try:
                        self.verified(candidate)
                        sha = candidate
                    except (OSError, ValueError):
                        pass
                db.execute(
                    "INSERT INTO artifact_discoveries VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET size=excluded.size, "
                    "last_seen=excluded.last_seen, "
                    "available=excluded.available,sha256=excluded.sha256",
                    (
                        identity,
                        provider,
                        path,
                        Path(path).name,
                        entry.get("size_bytes"),
                        now(),
                        now(),
                        int(available),
                        sha,
                    ),
                )

    def mark_derivative(self, sha, kind, status):
        from .gcode_fabrication import CACHE_VERSION

        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO artifact_derivatives VALUES(?,?,?,?)",
                (sha, kind, CACHE_VERSION, status),
            )

    def collection(self, kind, before=None, limit=40):
        if kind not in {"history", "unprinted", "discovered"} or not 1 <= limit <= 100:
            raise ValueError("Invalid collection or page size")
        # Keyset cursors include a unique identity; insertions cannot shift old pages.
        cursor = json.loads(before) if before else ["9999", "~"]
        if (
            not isinstance(cursor, list)
            or len(cursor) != 2
            or not all(isinstance(v, str) for v in cursor)
        ):
            raise ValueError("Invalid chronological cursor")
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO execution_artifacts "
                "SELECT p.id,g.sha256 FROM print_sessions p JOIN gcode_files g ON p.file_id=g.id"
            )
            if kind == "discovered":
                rows = db.execute(
                    "SELECT *,first_seen AS ingested_at FROM artifact_discoveries "
                    "WHERE (first_seen,id)<(?,?) ORDER BY first_seen DESC,id DESC LIMIT ?",
                    (*cursor, limit + 1),
                ).fetchall()
            elif kind == "history":
                rows = db.execute(
                    """SELECT p.*, e.sha256, e.evidence AS identity_evidence,
                    e.transport AS execution_transport, a.filename AS archived_filename
                    FROM print_sessions p LEFT JOIN execution_identity e ON p.id=e.execution_id
                    LEFT JOIN fabrication_artifacts a ON a.sha256=e.sha256
                    WHERE (p.started_at,p.id)<(?,?) ORDER BY p.started_at DESC,p.id DESC LIMIT ?""",
                    (*cursor, limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT a.*, a.sha256 AS id FROM fabrication_artifacts a
                    WHERE NOT EXISTS(SELECT 1 FROM execution_identity e WHERE e.sha256=a.sha256)
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
            sha = item.get("sha256")
            with self.db() as db:
                derivative = db.execute(
                    "SELECT status FROM artifact_derivatives WHERE sha256=? AND kind='geometry'",
                    (sha,),
                ).fetchone()
                stages = db.execute(
                    "SELECT status FROM artifact_stages WHERE sha256=?", (sha,)
                ).fetchall()
            item["lifecycle"] = {
                "discovery": "unavailable" if item.get("available") == 0 else "discovered",
                "archive": "archived" if item["bytes_available"] else "metadata_only",
                "archive_integrity": "verified_at_ingestion_recheck_on_access"
                if item["bytes_available"]
                else "unavailable",
                "preview": derivative[0]
                if derivative
                else "pending"
                if item["bytes_available"]
                else "unavailable",
                "delivery": stages[-1][0] if stages else "not_staged",
                "execution": item.get("state", "not_started"),
                "quality": "unassessed",
                "identity": item.get("identity_evidence")
                or ("verified_sha256" if sha else "discovered_not_archived"),
                "delivery_capabilities": {
                    "octoprint_usb": "requires_readiness_check",
                    "native_sd": "unsupported_unverified_write_transport",
                },
            }
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
                "SELECT e.sha256 FROM print_sessions p "
                "LEFT JOIN execution_identity e ON e.execution_id=p.id WHERE p.id=?",
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
