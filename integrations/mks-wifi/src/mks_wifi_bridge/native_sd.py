"""Native SD transfer jobs and content-addressed cache; no independent printer I/O."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from threading import RLock
from urllib.request import Request

from .gcode_fabrication import FabricationPreviewService
from .local_gcode_index import MAX_METADATA_HEADER_BYTES, _header_metadata
from .robin_sd_protocol import CHUNK, ProtocolReader
from .robin_sd_read import MAX_SD_BYTES, SdReadError


class OctoPrintSdTransport:
    def __init__(self, backend):
        self.backend = backend

    async def capabilities(self):
        return await asyncio.to_thread(self.backend._get_sync, "/api/plugin/neptune_sd_read")

    async def exchange(self, fields):
        request = Request(
            self.backend.base_url + "/api/plugin/neptune_sd_read",
            data=json.dumps({"command": "exchange", **fields}).encode(),
            headers={"X-Api-Key": self.backend.api_key, "Content-Type": "application/json"},
            method="POST",
        )

        def send():
            try:
                with self.backend._opener.open(request, timeout=7) as response:
                    raw = response.read(2049)
                if len(raw) > 2048:
                    raise SdReadError("Oversized SD bridge response")
                reply = json.loads(raw).get("reply")
                if not isinstance(reply, str):
                    raise SdReadError("Missing SD bridge reply")
                return reply
            except (OSError, ValueError) as exc:
                raise SdReadError("Native SD bridge unavailable or request rejected") from exc

        return await asyncio.to_thread(send)


class NativeSdStorage:
    def __init__(
        self, root: Path, transport, *, quota: int = 256 * 1024 * 1024, preview_slots=None
    ):
        if quota < MAX_SD_BYTES:
            raise ValueError("Cache quota must hold one maximum-sized file")
        self.root, self.transport, self.quota = root, transport, quota
        self.metadata_lock = RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.blobs = root / "blobs"
        self.blobs.mkdir(exist_ok=True)
        for part in root.glob("*.part"):
            part.unlink()  # No unverified partial bytes survive a service restart.
        self.records = root / "assets.json"
        try:
            if self.records.stat().st_size > 1_048_576:
                raise ValueError("Asset manifest exceeds limit")
            self.assets = json.loads(self.records.read_text())
            if not isinstance(self.assets, dict):
                raise ValueError()
        except (OSError, ValueError):
            self.assets = {}
        self.directory = {}
        for asset in self.assets.values():
            digest = asset.get("sha256", "")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not (self.blobs / (digest + ".gcode")).is_file()
            ):
                asset.update(
                    cache_available=False, geometry_available=False, thumbnail_available=False
                )
        self.jobs = {}
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.preview_slots = preview_slots or asyncio.Semaphore(2)
        self.previews = FabricationPreviewService(self.blobs, root / "geometry")

    def persist(self):
        with self.metadata_lock:
            temporary = self.records.with_suffix(".part")
            with temporary.open("w") as output:
                json.dump(self.assets, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.records)

    def prune(self, incoming=0):
        with self.metadata_lock:
            files = sorted(self.blobs.glob("*.gcode"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            evicted = False
            for path in files:
                if total + incoming <= self.quota:
                    break
                total -= path.stat().st_size
                path.unlink()
                evicted = True
                for asset in self.assets.values():
                    if asset.get("cache_location") == path.name:
                        asset.update(
                            cache_available=False,
                            geometry_available=False,
                            thumbnail_available=False,
                        )
            if evicted:
                self.persist()

    def invalidate(self, digest):
        with self.metadata_lock:
            for asset in self.assets.values():
                if asset.get("sha256") == digest:
                    asset.update(
                        cache_available=False, geometry_available=False, thumbnail_available=False
                    )
            self.persist()

    def cached(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SdReadError("Invalid content identity")
        path = self.blobs / (digest + ".gcode")
        if not path.is_file():
            self.invalidate(digest)
            raise SdReadError("Cached file unavailable")
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                path.unlink(missing_ok=True)
                self.invalidate(digest)
                raise SdReadError("Cached content failed SHA-256 verification")
        os.utime(path, None)
        return path

    async def listing(self):
        if self.lock.locked():
            raise SdReadError("SD transfer active; directory refresh deferred")
        async with self.lock, asyncio.timeout(60):
            capabilities = await self.transport.capabilities()
            if not capabilities.get("can_read"):
                raise SdReadError(capabilities.get("note", "Native SD firmware unsupported"))
            reader = ProtocolReader(self.transport.exchange)
            await reader.capabilities()
            entries = await reader.listing()
            # A filename match alone never associates cached bytes with a live entry.
            for entry in entries:
                size, token = await reader.stat(entry["name"])
                entry.update(size=size, token=token, source_available=True, retrievable=True)
                for asset in self.assets.values():
                    if (
                        asset.get("original_path"),
                        asset.get("snapshot_token"),
                        asset.get("size"),
                    ) == (entry["name"], token, size):
                        entry["cached_sha256"] = asset["sha256"]
            self.directory = {entry["name"]: entry for entry in entries}
            return entries

    def start(self, filename):
        # Validate before allocating a task, even when the device is offline.
        from .robin_sd_protocol import command

        command("OPEN", sequence=1, filename=filename)
        if any(not task.done() for task in self.tasks.values()):
            raise SdReadError(
                "Another SD transfer is active; cancel it before selecting another file"
            )
        while len(self.jobs) >= 50:
            oldest = next(iter(self.jobs))
            self.jobs.pop(oldest)
            self.tasks.pop(oldest, None)
        identifier = uuid.uuid4().hex
        job = {
            "id": identifier,
            "filename": filename,
            "provider": "printer_sd",
            "status": "queued",
            "received": 0,
            "size": None,
        }
        self.jobs[identifier] = job
        self.tasks[identifier] = asyncio.create_task(self.retrieve(job))
        return job

    async def retrieve(self, job):
        reader = ProtocolReader(self.transport.exchange)
        temporary = self.root / (job["id"] + ".part")
        completed = False
        acquired = False
        try:
            await self.lock.acquire()
            acquired = True
            async with asyncio.timeout(3600):
                if not (await self.transport.capabilities()).get("can_read"):
                    raise SdReadError("Native SD retrieval not enabled or firmware unsupported")
                await reader.capabilities()
                size, token = await reader.open(job["filename"])
                job.update(size=size, status="receiving")
                digest = hashlib.sha256()
                self.prune(size)
                with temporary.open("xb") as output:
                    for offset in range(0, size, CHUNK):
                        block = await reader.read(
                            job["filename"], offset, min(CHUNK, size - offset), token
                        )
                        output.write(block)
                        digest.update(block)
                        job["received"] += len(block)
                    output.flush()
                    os.fsync(output.fileno())
                if await reader.stat(job["filename"]) != (size, token):
                    raise SdReadError("SD file changed during transfer")
                await reader.close(expected_sha256=digest.hexdigest())
                sha = digest.hexdigest()
                target = self.blobs / (sha + ".gcode")
                if target.exists():
                    try:
                        self.cached(sha)
                    except SdReadError:
                        os.replace(temporary, target)
                    else:
                        temporary.unlink()
                else:
                    os.replace(temporary, target)
                identity = hashlib.sha256(
                    ("printer_sd\0" + job["filename"] + "\0" + sha).encode()
                ).hexdigest()
                with target.open("rb") as metadata_source:
                    metadata = _header_metadata(metadata_source.read(MAX_METADATA_HEADER_BYTES))
                asset = {
                    "id": identity,
                    "original_filename": self.directory.get(job["filename"], {}).get(
                        "original_filename", job["filename"]
                    ),
                    "provider": "printer_sd",
                    "original_path": job["filename"],
                    "size": size,
                    "snapshot_token": token,
                    "modification_info": None,
                    "sha256": sha,
                    "retrieved_at": time.time(),
                    "cache_location": target.name,
                    "cache_available": True,
                    "source_available": "last_verified_at_retrieval",
                    "geometry_available": False,
                    "thumbnail_available": False,
                    "transfer_status": "verified",
                    "print_metadata": metadata or None,
                }
                with self.metadata_lock:
                    self.assets[identity] = asset
                    while len(self.assets) > 512:
                        oldest = min(
                            self.assets, key=lambda key: self.assets[key].get("retrieved_at", 0)
                        )
                        del self.assets[oldest]
                    self.persist()
                job.update(status="reconstructing", sha256=sha)
                try:
                    async with self.preview_slots:
                        geometry = await asyncio.to_thread(self.previews.geometry, target.name)
                        await asyncio.to_thread(self.previews.preview, target.name)
                    asset.update(
                        geometry_available=True,
                        thumbnail_available=True,
                        geometry_stats=geometry.get("stats"),
                    )
                except ValueError as exc:
                    asset["geometry_error"] = str(exc)
                self.persist()
                job.update(status="complete", asset=asset)
                completed = True
        except asyncio.CancelledError:
            job["status"] = "cancelled"
        except Exception as exc:
            job.update(status="failed", error=str(exc)[:200])
        finally:
            temporary.unlink(missing_ok=True)
            if not completed:
                try:
                    await asyncio.wait_for(reader.close(cancel=True), 8)
                except Exception:
                    pass  # Firmware expiry closes an unreachable session, no ambiguous retries.
            if acquired:
                self.lock.release()
