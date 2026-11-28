"""Scoped, archive-only OctoPrint-protocol intake for OrcaSlicer. No printer backend."""

import asyncio
import hmac
import os
import shutil
from urllib.parse import quote, urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from .fabrication_ingestion import ArtifactSubmission, FabricationIngestion
from .octoprint_local_import import MAX_IMPORT_BYTES


def install(app, archive):
    gate = asyncio.Semaphore(2)

    async def authorize(request: Request):
        expected = os.getenv("NEPTUNE_INGESTION_TOKEN", "")
        if not expected:
            raise HTTPException(503, "Archive-only slicer intake is not configured")
        supplied = request.headers.get("x-api-key", "")
        if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, "Slicer ingestion authentication required")
        origin = request.headers.get("origin")
        if origin and (
            urlsplit(origin).scheme not in {"http", "https"}
            or urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
        ):
            raise HTTPException(403, "Request origin is not allowed")

    @app.get("/api/version", dependencies=[Depends(authorize)])
    def version():
        # Orca accepts an absent optional "text" field; do not impersonate an OctoPrint server.
        return {
            "api": "0.1",
            "application": "Neptune Observatory",
            "protocol": "OctoPrint-compatible intake",
            "capabilities": {
                "upload": True,
                "select": False,
                "print": False,
                "archive_first": True,
            },
        }

    @app.post("/api/files/local", dependencies=[Depends(authorize)])
    async def upload(request: Request):
        try:
            length = int(request.headers.get("content-length", ""))
        except ValueError as error:
            raise HTTPException(411, "A bounded Content-Length is required") from error
        if not 0 < length <= MAX_IMPORT_BYTES + 131072:
            raise HTTPException(413, "Slicer submission exceeds 50 MiB plus bounded form metadata")
        reserve = max(0, int(os.getenv("NEPTUNE_ARCHIVE_MIN_FREE_BYTES", str(1024**3))))
        if shutil.disk_usage(archive.root).free < reserve + 4 * length:
            raise HTTPException(507, "Canonical archive storage reserve would be violated")
        receive = request.receive
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > length:
                    raise HTTPException(413, "Submission exceeds declared content length")
            return message

        request = Request(request.scope, receive=bounded_receive)
        async with gate:
            try:
                async with (
                    asyncio.timeout(300),
                    request.form(max_files=1, max_fields=8, max_part_size=16384) as form,
                ):
                    if str(form.get("print", "false")).lower() not in {"false", "0", ""} or str(
                        form.get("select", "false")
                    ).lower() not in {"false", "0", ""}:
                        raise HTTPException(
                            423, "Archive-only intake never selects, stages or starts a print"
                        )
                    file = form.get("file")
                    if not isinstance(file, UploadFile) or not file.filename:
                        raise HTTPException(422, "One named manufacturing file is required")
                    if file.size is None or not 0 < file.size <= MAX_IMPORT_BYTES:
                        raise HTTPException(413, "Manufacturing file exceeds archive intake bound")
                    source_path = str(form.get("path", ""))
                    if (
                        len(source_path) > 512
                        or "\\" in source_path
                        or source_path.startswith("/")
                        or ".." in source_path.split("/")
                    ):
                        raise HTTPException(422, "Invalid source path")
                    plate = str(form.get("plateindex", ""))
                    if plate and (not plate.isdecimal() or not 1 <= int(plate) <= 100):
                        raise HTTPException(422, "Invalid plate index")
                    kind = (
                        "slicer_project"
                        if file.filename.lower().endswith(".3mf")
                        else "execution_gcode"
                    )
                    submission = ArtifactSubmission(
                        file.filename,
                        "orca_slicer",
                        "orca-host:" + quote(source_path + "/" + file.filename, safe="/"),
                        artifact_kind=kind,
                        expected_sha256=request.headers.get("x-content-sha256"),
                    )
                    await file.seek(0)
                    sha = await asyncio.to_thread(
                        FabricationIngestion(archive).submit, submission, file.file
                    )
                    resource = "/api/fabrication/artifacts/" + sha + "/original"
                    return JSONResponse(
                        {
                            "done": True,
                            "archived": True,
                            "sha256": sha,
                            "selected": False,
                            "printing": False,
                            "staged": False,
                            "printer_command_sent": False,
                            "files": {
                                "local": {
                                    "name": file.filename,
                                    "origin": "local",
                                    "storage_provider": "canonical_archive",
                                    "path": sha,
                                    "refs": {"resource": resource, "download": resource},
                                }
                            },
                        },
                        status_code=201,
                    )
            except TimeoutError as error:
                raise HTTPException(
                    408, "Slicer intake timed out; retry is content-idempotent"
                ) from error
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
