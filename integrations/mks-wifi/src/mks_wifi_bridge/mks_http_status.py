"""Read-only MKS WiFi HTTP status, separate from OctoPrint's raw TCP client."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class MksHttpStatusError(RuntimeError):
    """Sanitized error reading the MKS HTTP status cache."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _number(value: Any, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def normalize_mks_http_status(printer: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Report what MKS firmware says; do not infer actual motor movement."""
    state_data = printer.get("state") or {}
    flags = state_data.get("flags") if isinstance(state_data, dict) else {}
    flags = flags if isinstance(flags, dict) else {}
    if flags.get("printing") is True:
        state = "printing"
    elif flags.get("paused") is True:
        state = "paused"
    elif (
        flags.get("operational") is True
        and flags.get("printing") is False
        and flags.get("paused") is False
    ):
        state = "idle"
    else:
        state = "unknown"
    progress = job.get("progress") or {}
    job_data = job.get("job") or {}
    file_info = job_data.get("file") if isinstance(job_data, dict) else {}
    progress = progress if isinstance(progress, dict) else {}
    file_info = file_info if isinstance(file_info, dict) else {}
    filename = file_info.get("name")
    filename = filename.strip()[:240] if isinstance(filename, str) and filename.strip() else None
    active = state in {"printing", "paused"}
    elapsed = _number(progress.get("printTime"), 315360000)
    return {
        "status": "available" if state != "unknown" else "unknown",
        "state": state,
        "active": active if state != "unknown" else None,
        "progress_percent": _number(progress.get("completion"), 100) if active else None,
        "filename": filename if active else None,
        "elapsed_seconds": int(elapsed) if elapsed is not None and active else None,
        "source": "mks_wifi_http",
        "confidence": "firmware_cached_state_not_motor_feedback",
        "observed_at": datetime.now(UTC).isoformat(),
    }


class MksHttpStatusReader:
    """Two fixed GET requests; never creates an MKS port-8080 serial session."""

    def __init__(self, host: str, port: int = 80, timeout: float = 1.5) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("MKS HTTP monitoring needs a literal LAN IP") from exc
        if not (address.is_private or address.is_link_local) or not 1 <= port <= 65535:
            raise ValueError("MKS HTTP monitoring requires a private IP and valid port")
        if not 0 < timeout <= 10:
            raise ValueError("MKS HTTP timeout must be in 0..10 seconds")
        literal = f"[{host}]" if address.version == 6 else host
        self.base_url = f"http://{literal}:{port}"
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _get_sync(self, path: str) -> dict[str, Any]:
        if path not in {"/api/printer", "/api/job"}:
            raise ValueError("Only fixed MKS status endpoints are allowed")
        request = Request(self.base_url + path, headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise MksHttpStatusError("Unexpected MKS HTTP status")
                raw = response.read(65537)
            if len(raw) > 65536:
                raise MksHttpStatusError("MKS status response exceeds 64 KiB")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Invalid JSON")
            return payload
        except HTTPError as exc:
            raise MksHttpStatusError(f"MKS status returned HTTP {exc.code}") from None
        except (OSError, URLError, TimeoutError):
            raise MksHttpStatusError("MKS status endpoint unreachable or timed out") from None
        except (ValueError, UnicodeError):
            raise MksHttpStatusError("MKS status returned malformed JSON") from None

    async def read_state(self) -> dict[str, Any]:
        printer = await asyncio.to_thread(self._get_sync, "/api/printer")
        job = await asyncio.to_thread(self._get_sync, "/api/job")
        return normalize_mks_http_status(printer, job)
