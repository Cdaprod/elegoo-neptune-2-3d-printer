"""Authenticated native SD routes installed into the shared Observatory backend."""

import asyncio
import json
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .native_sd import NativeSdStorage, OctoPrintSdTransport
from .robin_sd_read import SdReadError


def install(app, read_auth, write_auth, printer, preview_slots=None, archive=None):
    storage = None

    def service():
        nonlocal storage
        if os.getenv("NEPTUNE_NATIVE_SD_READ_ENABLED", "false").lower() != "true":
            raise SdReadError(
                "Native SD read disabled; enable only after RSD/1 firmware commissioning"
            )
        if printer.backend is None:
            raise SdReadError("Native SD read requires the existing OctoPrint USB connection owner")
        if storage is None:
            storage = NativeSdStorage(
                Path(os.getenv("NEPTUNE_NATIVE_SD_CACHE", "/data/native-sd")),
                OctoPrintSdTransport(printer.backend),
                preview_slots=preview_slots,
            )
            storage.archive = archive
            app.state.native_sd_storage = storage
        return storage

    def available():
        try:
            return service()
        except SdReadError as exc:
            raise HTTPException(423, str(exc)) from exc

    @app.get("/api/native-sd", dependencies=[Depends(read_auth)])
    async def directory():
        try:
            current = service()
            entries = await current.listing()
            if archive is not None:
                await asyncio.to_thread(
                    archive.observe_discovery,
                    [
                        {
                            "name": entry["name"],
                            "kind": "file",
                            "size_bytes": entry.get("size"),
                            "verified_sha256": entry.get("cached_sha256"),
                            "identity_evidence": "verified_content_snapshot"
                            if entry.get("cached_sha256")
                            else None,
                        }
                        for entry in entries
                    ],
                )
            return {
                "status": "available",
                "provider": "printer_sd",
                "entries": entries,
                "assets": list(current.assets.values()),
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "provider": "printer_sd",
                "entries": [],
                "assets": list(storage.assets.values()) if storage else [],
                "note": str(exc)[:200],
            }

    @app.post("/api/native-sd/transfers", dependencies=[Depends(write_auth)])
    async def retrieve(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 512:
                raise HTTPException(413, "Transfer request exceeds limit")
            data.extend(chunk)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, "Malformed transfer request") from exc
        if not isinstance(payload, dict) or set(payload) != {"filename"}:
            raise HTTPException(422, "Specify only the SD directory alias filename")
        try:
            return available().start(payload["filename"])
        except SdReadError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/native-sd/transfers/{identifier}", dependencies=[Depends(read_auth)])
    async def transfer_status(identifier: str):
        current = available()
        if identifier not in current.jobs:
            raise HTTPException(404, "Transfer not found")
        return current.jobs[identifier]

    @app.delete("/api/native-sd/transfers/{identifier}", dependencies=[Depends(write_auth)])
    async def cancel_transfer(identifier: str):
        current = available()
        task = current.tasks.get(identifier)
        if task is None:
            raise HTTPException(404, "Transfer not found")
        task.cancel()
        return {"status": "cancellation_requested"}

    @app.get("/api/native-sd/assets/{digest}/{representation}", dependencies=[Depends(read_auth)])
    async def asset(digest: str, representation: str):
        current = available()
        try:
            async with current.preview_slots:
                path = await asyncio.to_thread(current.cached, digest)
                if representation == "gcode":
                    return FileResponse(path, media_type="text/plain", filename=path.name)
                if representation == "geometry":
                    return await asyncio.to_thread(current.previews.geometry, path.name)
                if representation == "thumbnail":
                    return Response(
                        await asyncio.to_thread(current.previews.preview, path.name),
                        media_type="image/svg+xml",
                        headers={
                            "Content-Security-Policy": (
                                "default-src 'none'; style-src 'unsafe-inline'"
                            )
                        },
                    )
                raise HTTPException(404, "Representation not found")
        except (SdReadError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/native-sd/assets/{digest}/import-local", dependencies=[Depends(write_auth)])
    async def import_local(digest: str):
        from .octoprint_local_import import LibraryImportError, upload_local_gcode

        if not getattr(app.state, "hmi_import_enabled", False):
            raise HTTPException(423, "Enable HMI local import before copying verified files")
        current = available()
        try:
            path = await asyncio.to_thread(current.cached, digest)
            records = [item for item in current.assets.values() if item["sha256"] == digest]
            if not records:
                raise SdReadError("Asset provenance unavailable")
            data = await asyncio.to_thread(path.read_bytes)
            result = await asyncio.to_thread(
                upload_local_gcode,
                printer.backend.base_url,
                printer.backend.api_key,
                records[-1]["original_filename"],
                data,
            )
            return {
                **result,
                "source_provider": "printer_sd",
                "destination_provider": "octoprint_local",
            }
        except (SdReadError, LibraryImportError) as exc:
            raise HTTPException(422, str(exc)) from exc

    return service
