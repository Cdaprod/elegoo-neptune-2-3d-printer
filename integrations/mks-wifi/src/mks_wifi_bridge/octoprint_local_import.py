"""Guarded local-library G-code import through OctoPrint's authenticated HTTP API.

Never connects to firmware, chooses a file, or starts a print. No overwrite.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_IMPORT_BYTES = 50 * 1024 * 1024
SUPPORTED_SUFFIXES = {".gcode", ".gco", ".gc", ".g"}
NAME = re.compile(r"^(?=.{1,100}$)[A-Za-z0-9_-][A-Za-z0-9_. -]*\.(?:gcode|gco|gc|g)$", re.I)


class LibraryImportError(RuntimeError):
    """Safely report an upload or storage verification failure."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def validate_gcode_filename(filename: str, data: bytes) -> str:
    """Normalize common slicer filenames, but reject traversal and control characters."""
    if (
        not isinstance(filename, str)
        or len(filename) > 240
        or not filename
        or filename.startswith(".")
        or ".." in filename
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 for char in filename)
    ):
        raise LibraryImportError("Supply a G-code filename, not a path or hidden file")
    suffix = "." + filename.rsplit(".", 1)[-1].lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise LibraryImportError("Only .gcode, .gco, .gc and .g files can be imported")
    if not data or len(data) > MAX_IMPORT_BYTES:
        raise LibraryImportError("G-code must contain between 1 byte and 50 MiB")
    if NAME.fullmatch(filename):
        return filename
    stem = filename[:-len(suffix)]
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    safe_stem = re.sub(r"[^A-Za-z0-9_. -]+", "-", ascii_stem)
    safe_stem = re.sub(r"\\.{2,}", "-", safe_stem).strip(" ._-")[:65] or "model"
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:10]
    return f"{safe_stem}-{digest}{suffix}"


def multipart(filename: str, data: bytes) -> tuple[str, bytes]:
    filename = validate_gcode_filename(filename, data)
    boundary = "neptune-" + os.urandom(18).hex()
    body = b""
    for field in ("select", "print"):
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n'
            'Content-Type: text/plain\r\n\r\nfalse\r\n'
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        'Content-Type: application/octet-stream\r\n\r\n'
    ).encode() + data + b"\r\n"
    return boundary, body + f"--{boundary}--\r\n".encode()


def upload_local_gcode(
    base_url: str, api_key: str, filename: str, data: bytes, *, opener=None
) -> dict:
    """Copy one verified local file. Existing names are never overwritten."""
    filename = validate_gcode_filename(filename, data)
    if not api_key or not base_url.startswith(("http://", "https://")):
        raise LibraryImportError("OctoPrint local import is not configured")
    opener = opener or build_opener(NoRedirects())
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    expected = hashlib.sha1(data).hexdigest()
    digest = hashlib.sha256(data).hexdigest()
    try:
        request = Request(base_url.rstrip("/") + "/api/files/local", headers=headers)
        with opener.open(request, timeout=15) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise LibraryImportError("OctoPrint local listing is unexpectedly large")
        listing = json.loads(body)
        if not isinstance(listing, dict) or not isinstance(listing.get("files"), list):
            raise LibraryImportError("OctoPrint returned an invalid file listing")
        existing = {
            item["name"].casefold(): item
            for item in listing["files"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        found = existing.get(filename.casefold())
        if found is not None:
            if found.get("hash") == expected:
                return {
                    "status": "already_imported", "filename": filename,
                    "sha256": digest, "size_bytes": len(data),
                    "selected": False, "printing": False,
                }
            from pathlib import PurePosixPath

            suffix = PurePosixPath(filename).suffix.lower()
            stem = filename[: -len(suffix)][:48].rstrip(" ._-") or "model"
            filename = f"{stem}-{digest[:16]}{suffix}"
            duplicate = existing.get(filename.casefold())
            if duplicate is not None:
                if duplicate.get("hash") in (expected, None):
                    return {
                        "status": (
                            "existing_name_unverified"
                            if duplicate.get("hash") is None
                            else "already_imported"
                        ),
                        "filename": filename, "sha256": digest,
                        "size_bytes": len(data), "selected": False, "printing": False,
                    }
                raise LibraryImportError("Content-addressed filename is occupied")
        boundary, payload = multipart(filename, data)
        request = Request(
            base_url.rstrip("/") + "/api/files/local", data=payload,
            headers={
                **headers,
                "Content-Type": "multipart/form-data; boundary=" + boundary,
            },
            method="POST",
        )
        with opener.open(request, timeout=120) as response:
            if response.status not in {200, 201}:
                raise LibraryImportError("OctoPrint did not accept the local file")
            raw = response.read(4097)
        if len(raw) > 4096:
            raise LibraryImportError("OctoPrint returned an oversized import response")
        return {
            "status": "imported", "filename": filename,
            "sha256": digest, "size_bytes": len(data),
            "selected": False, "printing": False,
        }
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise LibraryImportError("OctoPrint refused the saved application key") from None
        raise LibraryImportError(f"OctoPrint file API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise LibraryImportError("OctoPrint local-file API is unavailable") from exc
    except (ValueError, UnicodeError) as exc:
        raise LibraryImportError("OctoPrint local-file response is invalid") from exc
