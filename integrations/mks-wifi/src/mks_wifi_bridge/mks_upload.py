"""Guarded raw-body upload client for the Makerbase MKS WiFi HTTP endpoint."""

from __future__ import annotations

import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.(?:gcode|gco|gc)", re.I)


class UploadError(RuntimeError):
    """An MKS WiFi upload request failed or was rejected."""


def validate_upload(filename: str, content: bytes) -> None:
    if not SAFE_FILENAME.fullmatch(filename) or ".." in filename:
        raise ValueError("Use a simple .gcode, .gco, or .gc filename without directories.")
    if not content:
        raise ValueError("The selected G-code file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("The G-code file exceeds the 50 MB upload limit.")


def upload_to_mks(
    host: str, filename: str, content: bytes, timeout: float = 30.0
) -> dict[str, object]:
    """Send raw file bytes to the source-documented /upload?X-Filename route."""
    validate_upload(filename, content)
    url = f"http://{host}:80/upload?X-Filename={quote(filename, safe='')}"
    request = Request(
        url,
        data=content,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(content)),
            "Connection": "close",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(2000).decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise UploadError(f"MKS upload returned HTTP {response.status}: {body[:300]}")
            return {"http_status": response.status, "response": body}
    except HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise UploadError(f"MKS upload returned HTTP {exc.code}: {detail[:300]}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise UploadError(f"MKS upload transport failed: {exc}") from exc
