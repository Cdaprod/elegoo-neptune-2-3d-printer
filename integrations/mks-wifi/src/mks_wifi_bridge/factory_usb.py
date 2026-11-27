"""Factory USB observations and guarded queries through the existing OctoPrint owner."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from urllib.request import Request

from fastapi import Depends, HTTPException
from fastapi import Request as WebRequest

from .motion import build_motion_sample, parse_m114
from .octoprint_backend import OctoPrintError

READ_OPERATIONS = {
    "firmware",
    "configuration",
    "temperature",
    "coordinates",
    "list",
    "list_long",
    "status",
}
OPERATIONS = READ_OPERATIONS | {"select", "initialize"}
SYSTEM_NAMES = {"system volume information", "$recycle.bin", "lost+found"}


def catalog_visible(name: str) -> bool:
    return bool(name) and all(
        part and not part.startswith(".") and part.casefold() not in SYSTEM_NAMES
        for part in name.replace("\\", "/").strip("/").split("/")
    )


async def query(backend, payload: dict):
    if payload.get("operation") not in OPERATIONS:
        raise ValueError("Unsupported factory USB operation")
    request = Request(
        backend.base_url + "/api/plugin/neptune_sd_read",
        data=json.dumps({"command": "factory", **payload}).encode(),
        headers={"X-Api-Key": backend.api_key, "Content-Type": "application/json"},
        method="POST",
    )

    def send():
        try:
            with backend._opener.open(request, timeout=10) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise OctoPrintError("Factory snapshot exceeds limit")
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
                raise OctoPrintError("Malformed factory response")
            return result
        except (OSError, ValueError) as exc:
            raise OctoPrintError("Factory USB query rejected, disconnected or timed out") from exc

    return await asyncio.to_thread(send)


def install(app, read_auth, write_auth, printer):
    @app.get("/api/factory-usb", dependencies=[Depends(read_auth)])
    async def snapshot():
        if printer.backend is None:
            return {
                "available": False,
                "note": "OctoPrint USB owner is required",
                "observations": {},
            }
        try:
            payload = await printer.backend.get("/api/plugin/neptune_sd_read")
            factory = payload.get("factory_usb") or {}
            return {
                **factory,
                "selection_enabled": os.getenv(
                    "NEPTUNE_FACTORY_USB_SELECTION_ENABLED", "false"
                ).lower()
                == "true",
                "content_retrieval": bool(payload.get("can_read")),
                "source": "octoprint_usb_owner",
            }
        except OctoPrintError:
            return {
                "available": False,
                "observations": {},
                "note": "USB observation bridge unavailable",
            }

    @app.post("/api/factory-usb/query", dependencies=[Depends(write_auth)])
    async def inspect(request: WebRequest):
        raw = bytearray()
        async for block in request.stream():
            if len(raw) + len(block) > 1024:
                raise HTTPException(413, "Request exceeds limit")
            raw.extend(block)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(422, "Malformed request") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("operation") not in OPERATIONS
            or set(payload)
            - {"operation", "filename", "confirmation", "attended", "printer_idle_confirmed"}
        ):
            raise HTTPException(422, "Unsupported factory USB query")
        if printer.backend is None:
            raise HTTPException(423, "OctoPrint USB owner required")
        if (
            payload["operation"] not in READ_OPERATIONS
            and os.getenv("NEPTUNE_FACTORY_USB_SELECTION_ENABLED", "false").lower() != "true"
        ):
            raise HTTPException(423, "Factory SD selection/initialization gate is disabled")
        try:
            async with printer.io_lock:
                result = await query(printer.backend, payload)
                observed = result.get("observed_at")
                if payload["operation"] == "coordinates":
                    stamp = datetime.fromtimestamp(observed, UTC)
                    sample = build_motion_sample(
                        parse_m114([result["data"]["raw_line"]]),
                        observed_at=stamp.isoformat(),
                        printer_state="idle",
                        previous=printer.motion_latest,
                    )
                    sample["source"] = "factory_usb_M114"
                    printer.motion_latest = sample
                    printer.motion_last_observed_at = stamp
                    printer.motion_history.append(sample)
                    printer.motion_status = sample["status"]
                if payload["operation"] in {"list", "list_long", "initialize"}:
                    printer.sd_entries = result["data"].get("entries", [])
                    printer.sd_status = "available"
                    printer.sd_refreshed_at = datetime.fromtimestamp(observed, UTC)
                    printer.sd_error = None
                return result
        except (OctoPrintError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc
