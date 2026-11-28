"""Completed-file handoff adapter; receiver must atomically publish completed files."""

import asyncio
import re
import time
from pathlib import Path

from .fabrication_ingestion import ArtifactSubmission, FabricationIngestion
from .octoprint_local_import import LibraryImportError


class CompletedDirectoryAdapter:
    def __init__(self, archive, directory, settle_seconds=2):
        self.archive = archive
        self.directory = Path(directory)
        self.service = FabricationIngestion(archive)
        self.settle_seconds = settle_seconds
        self.pending = {}
        self._paths = None
        self.status = {
            "provider": "lilbitdrop",
            "directory": str(self.directory),
            "state": "starting",
            "ingested": 0,
            "errors": [],
            "pending": 0,
        }
        with archive.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS completed_ingestion "
                "(provider TEXT, path TEXT, signature TEXT, sha256 TEXT, "
                "PRIMARY KEY(provider,path))"
            )

    def scan(self):
        if not self.directory.is_dir():
            self.pending.clear()
            self._paths = None
            self.status.update(state="unavailable", pending=0)
            return
        self.status["state"] = "watching"
        processed = 0
        candidates = 0
        root = self.directory.resolve()
        if self._paths is None:
            self._paths = self.directory.rglob("*")
        for path in self._paths:
            candidates += 1
            if candidates > 1000 or processed >= 4:
                break
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(self.directory)
            if any(part.startswith(".") or part.lower() == "partial" for part in relative.parts):
                continue
            if path.suffix.lower() not in {".gcode", ".gco", ".gc", ".g"}:
                continue
            if not path.resolve().is_relative_to(root):
                continue
            key = str(path)
            try:
                stat = path.stat()
                signature = f"{stat.st_size}:{stat.st_mtime_ns}"
                with self.archive.db() as db:
                    previous = db.execute(
                        "SELECT signature FROM completed_ingestion WHERE provider=? AND path=?",
                        ("lilbitdrop", key),
                    ).fetchone()
                if previous and previous[0] == signature:
                    self.pending.pop(key, None)
                    continue
                previous_signature, since = self.pending.get(key, (None, time.monotonic()))
                if signature != previous_signature:
                    self.pending[key] = (signature, time.monotonic())
                    continue
                if time.monotonic() - since < self.settle_seconds:
                    continue
                self.status.update(state="ingesting", current_file=str(relative))
                prefix = re.match(r"^([a-f0-9]{64})_", path.name)
                original = re.sub(r"^[a-f0-9]{64}_", "", path.name)
                sha = self.service.submit_path(
                    path,
                    ArtifactSubmission(
                        original,
                        "lilbitdrop",
                        "file:" + str(path),
                        expected_sha256=prefix[1] if prefix else None,
                    ),
                )
                with self.archive.db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO completed_ingestion VALUES(?,?,?,?)",
                        ("lilbitdrop", key, signature, sha),
                    )
                self.pending.pop(key, None)
                self.status["ingested"] += 1
                processed += 1
            except (OSError, ValueError, LibraryImportError) as exc:
                self.status["errors"] = (
                    self.status["errors"] + [{"file": str(relative), "error": str(exc)[:200]}]
                )[-20:]
                self.status["state"] = "degraded"
        else:
            self._paths = None
        self.pending = {key: value for key, value in self.pending.items() if Path(key).is_file()}
        self.status["pending"] = len(self.pending)
        if self.status["state"] == "ingesting":
            self.status["state"] = "watching"

    async def run(self):
        from watchfiles import awatch

        while True:
            try:
                await asyncio.to_thread(self.scan)
                if not self.directory.is_dir():
                    await asyncio.sleep(2)
                    continue
                # Timeout reconciliation handles startup, missed events and settling files.
                async for _ in awatch(self.directory, yield_on_timeout=True, rust_timeout=2000):
                    await asyncio.to_thread(self.scan)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.update(state="degraded", last_error=str(exc)[:200])
                await asyncio.sleep(2)
