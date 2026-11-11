"""Authenticated, read-only OctoPrint adapter for Neptune Observatory.

OctoPrint owns the single native MKS WiFi TCP session. This adapter never
connects to the printer, issues G-code, or triggers print/thermal actions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class OctoPrintError(RuntimeError):
    """Sanitized upstream failure; API keys and full URLs are never reported."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _field(value: Any, **extra: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "unsupported_or_not_reported", "value": None, **extra}
    return {"status": "available", "value": value, "source": "octoprint_api", **extra}


class OctoPrintBackend:
    """One OctoPrint API origin, server-side secret, bounded read-only requests."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 3.0) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not api_key.strip()
            or not 0 < timeout <= 30
        ):
            raise ValueError("Invalid OctoPrint URL, API key, or request timeout")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._opener = build_opener(_NoRedirects())

    def _get_sync(self, endpoint: str) -> dict[str, Any]:
        request = Request(
            self.base_url + endpoint,
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise OctoPrintError("OctoPrint returned an unexpected response status")
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise OctoPrintError("OctoPrint response exceeds the 1 MiB limit")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise OctoPrintError("OctoPrint returned an invalid JSON object")
            return payload
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise OctoPrintError("OctoPrint API authentication failed") from None
            raise OctoPrintError(f"OctoPrint API returned HTTP {exc.code}") from None
        except (OSError, URLError, TimeoutError) as exc:
            raise OctoPrintError("OctoPrint API is unreachable or timed out") from None
        except (ValueError, UnicodeError):
            raise OctoPrintError("OctoPrint returned malformed JSON") from None

    async def get(self, endpoint: str) -> dict[str, Any]:
        # Endpoints are fixed internal literals, never caller-controlled URLs.
        if endpoint not in {"/api/connection", "/api/printer", "/api/job", "/api/files"}:
            raise ValueError("Unrecognized read-only OctoPrint endpoint")
        return await asyncio.to_thread(self._get_sync, endpoint)

    async def read_state(self) -> dict[str, Any]:
        connection = await self.get("/api/connection")
        current = connection.get("current") or {}
        state = str(current.get("state") or "unknown").lower()
        if state in {"closed", "offline", "error", "unknown"}:
            raise OctoPrintError("OctoPrint is reachable but its printer is disconnected")

        printer = await self.get("/api/printer")
        job = await self.get("/api/job")
        state_info = printer.get("state") or {}
        flags = state_info.get("flags") or {}
        if flags.get("printing") is True:
            normalized = "printing"
        elif flags.get("paused") is True or flags.get("pausing") is True:
            normalized = "paused"
        elif flags.get("operational") is True and not flags.get("error"):
            normalized = "idle"
        else:
            normalized = None
        temperatures = printer.get("temperature") or {}
        nozzle = temperatures.get("tool0") or {}
        bed = temperatures.get("bed") or {}
        job_file = (job.get("job") or {}).get("file") or {}
        progress = job.get("progress") or {}
        active = normalized in {"printing", "paused"} if normalized is not None else None
        return {
            "nozzle_temperature": _field(
                nozzle.get("actual"), target_c=nozzle.get("target"), unit="°C"
            ),
            "bed_temperature": _field(
                bed.get("actual"), target_c=bed.get("target"), unit="°C"
            ),
            "printer_state": {
                "status": "available" if normalized is not None else "unsupported_or_not_reported",
                "value": normalized,
                "raw_value": state_info.get("text") or current.get("state"),
                "source": "octoprint_api",
            },
            "print_active": _field(active) if active is not None else _field(None),
            "firmware_identification": _field(None),
            "progress_percent": _field(progress.get("completion"), unit="%"),
            "current_file": _field(job_file.get("name")),
            "file_size_bytes": _field(job_file.get("size")),
            "elapsed_seconds": _field(progress.get("printTime")),
            "remaining_seconds": _field(progress.get("printTimeLeft")),
        }

    async def list_files(self) -> list[dict[str, Any]]:
        payload = await self.get("/api/files")
        entries: list[dict[str, Any]] = []
        for item in payload.get("files") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            # Only OctoPrint's local storage is represented here. Never
            # label uploaded files as firmware SD contents.
            if item.get("origin") != "local":
                continue
            kind = "directory" if item.get("type") == "folder" else "file"
            entries.append(
                {
                    "name": name,
                    "kind": kind,
                    "size_bytes": item.get("size"),
                    "source": "octoprint_local",
                }
            )
        return entries
