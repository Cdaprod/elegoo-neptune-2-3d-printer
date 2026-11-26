"""Authenticated Print Bay API, mounted inside the existing Observatory application."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict

from .controller import Controller
from .deployment import export_sd, upload
from .project import confined
from .sync import BuildNode, snapshot


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plate: str
    action: Literal[
        "build", "slice", "build-slice", "slice-stage", "build-slice-stage", "reconcile"
    ]


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


def install(app, require_auth, require_mutation, printer):
    # Lazy creation avoids changing monitoring startup or tests when manufacturing is unused.
    def controller():
        if not hasattr(app.state, "manufacturing"):
            app.state.manufacturing = Controller(
                Path(os.getenv("NEPTUNE_MANUFACTURING_STORAGE", "/data/manufacturing")),
                Path(os.getenv("NEPTUNE_MANUFACTURING_PROJECTS", "/data/manufacturing/projects")),
            )
        return app.state.manufacturing

    router = APIRouter(
        prefix="/api/manufacturing", tags=["Print Bay"], dependencies=[Depends(require_auth)]
    )

    def error(exc):
        if isinstance(exc, (ValueError, KeyError, StopIteration, FileNotFoundError)):
            return HTTPException(409, str(exc) or "Plate not found")
        return HTTPException(503, "Manufacturing operation unavailable; inspect server logs")

    @app.get("/print-bay", response_class=HTMLResponse, include_in_schema=False)
    def page():
        return HTMLResponse(Path(__file__).parents[1].joinpath("static/print_bay.html").read_text())

    @router.get("/projects")
    def projects():
        return controller().discover()

    @router.get("/worker")
    def worker_status():
        state = controller().worker_status()
        if os.getenv("NEPTUNE_BUILD_NODE_URL"):
            try:
                state = BuildNode().request("/worker", timeout=3)
            except ValueError:
                state = {"online": False, "state": "build_node_unreachable", "last_seen": None}
        state["delivery"] = {
            "upload_enabled": os.getenv("NEPTUNE_MANUFACTURING_UPLOAD_ENABLED", "false") == "true"
            and printer.backend is not None,
            "sd_mounted": os.path.ismount(os.getenv("NEPTUNE_SD_MOUNT", "/Volumes/NEPTUNE")),
        }
        return state

    @router.get("/projects/{project}/snapshot")
    def project_snapshot(project: str):
        try:
            root, manifest = controller().project(project)
            return Response(snapshot(root, manifest), media_type="application/zip")
        except Exception as exc:
            raise error(exc) from exc

    @router.get("/projects/{project}/geometry/{filename}")
    def geometry_file(project: str, filename: str):
        try:
            c = controller()
            root, manifest = c.project(project)
            names = {o.export for plate in manifest.spec.plates for o in plate.objects}
            if filename not in names:
                raise ValueError("Unknown geometry export")
            geometry = c.geometry(root, manifest)
            c.validate_geometry(geometry, manifest)
            import json

            from .project import filehash

            hashes = json.loads((geometry / "hashes.json").read_text())
            path = confined(geometry, filename)
            if filehash(path) != hashes[filename]:
                raise ValueError("Geometry integrity verification failed")
            return FileResponse(path)
        except Exception as exc:
            raise error(exc) from exc

    @router.get("/inventory")
    def inventory():
        roots = os.getenv("NEPTUNE_MANUFACTURING_INVENTORY", "").split(os.pathsep)
        result = []
        for entry in filter(None, roots):
            root = Path(entry).expanduser().resolve()
            if not root.is_dir():
                continue
            for directory, children, files in os.walk(root, followlinks=False):
                children[:] = sorted(n for n in children if not n.startswith("."))
                if len(Path(directory).relative_to(root).parts) >= 4:
                    children[:] = []
                for name in sorted(files):
                    path = Path(directory) / name
                    if path.suffix.lower() in {".fcstd", ".3mf"} and not path.is_symlink():
                        result.append(
                            {
                                "file": str(path.relative_to(root)),
                                "root": root.name,
                                "bytes": path.stat().st_size,
                                "status": "candidate; not imported",
                            }
                        )
                        if len(result) >= 250:
                            return result
        return result

    @router.get("/artifacts")
    def artifacts(project: str | None = None):
        return controller().list_artifacts(project)

    @router.get("/jobs")
    def jobs():
        if os.getenv("NEPTUNE_BUILD_NODE_URL"):
            try:
                return BuildNode().request("/jobs", timeout=3)
            except ValueError:
                return controller().jobs()
        return controller().jobs()

    @router.post("/projects/{project}/jobs", dependencies=[Depends(require_mutation)])
    def enqueue(project: str, body: Action):
        try:
            if os.getenv("NEPTUNE_BUILD_NODE_URL"):
                from urllib.parse import quote

                return BuildNode().request(
                    "/projects/" + quote(project, safe="") + "/jobs", body.model_dump()
                )
            return {
                "job": controller().enqueue(project, body.plate, body.action),
                "status": "queued",
            }
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/artifacts/{identifier}/stage", dependencies=[Depends(require_mutation)])
    def stage(identifier: str):
        try:
            return controller().stage(identifier)
        except Exception as exc:
            raise error(exc) from exc

    @router.get("/artifacts/{identifier}/files/{filename}")
    def download(identifier: str, filename: str):
        try:
            row = controller().artifact(identifier)
            root = confined(controller().storage, row["path"])
            path = confined(root, filename)
            if path.name != filename or path.suffix not in {
                ".stl",
                ".gcode",
                ".json",
                ".sha256",
                ".3mf",
                ".log",
                ".png",
            }:
                raise ValueError("Unsupported artifact file")
            if not path.is_file():
                raise ValueError("Artifact file missing")
            return FileResponse(path)
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/artifacts/{identifier}/export-sd", dependencies=[Depends(require_mutation)])
    def sd(identifier: str, body: Approval):
        if body.confirmation != f"EXPORT {identifier}":
            raise HTTPException(422, "Explicit artifact export confirmation required")
        try:
            return export_sd(
                controller(), identifier, Path(os.getenv("NEPTUNE_SD_MOUNT", "/Volumes/NEPTUNE"))
            )
        except Exception as exc:
            raise error(exc) from exc

    @router.post("/artifacts/{identifier}/deploy", dependencies=[Depends(require_mutation)])
    async def deploy(identifier: str, body: Approval):
        if body.confirmation != f"UPLOAD {identifier}":
            raise HTTPException(422, "Explicit artifact upload confirmation required")
        if os.getenv("NEPTUNE_MANUFACTURING_UPLOAD_ENABLED", "false") != "true":
            raise HTTPException(423, "Print Bay uploads disabled")
        if printer.backend is None:
            raise HTTPException(409, "Configured OctoPrint backend required")
        async with app.state.octoprint_control_lock:
            snapshot = printer.status_payload()
            state = (snapshot.get("telemetry") or {}).get("printer_state", {}).get("value")
            if (
                snapshot.get("stale")
                or snapshot.get("data_age_seconds", 999) is None
                or (snapshot.get("data_age_seconds", 999) > 5 or state != "idle")
            ):
                raise HTTPException(409, "Fresh idle printer telemetry required")
            try:
                return await asyncio.to_thread(upload, controller(), identifier, printer.backend)
            except Exception as exc:
                raise error(exc) from exc

    app.include_router(router)
