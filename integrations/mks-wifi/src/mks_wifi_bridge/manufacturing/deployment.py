"""Explicit deployment transports. Upload never selects or starts a print."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.request import Request

from ..octoprint_backend import OctoPrintError
from .project import filehash


def export_sd(controller, identifier, mount: Path):
    row, path = controller.verified(identifier, staged=True)
    _, manifest = controller.project(row["project"])
    if (
        not mount.is_dir()
        or mount.is_symlink()
        or not os.path.ismount(mount)
        or mount.name != manifest.spec.fallback.volume
    ):
        raise ValueError("Configured removable volume is not mounted; no directory was created")
    source = (
        path
        / manifest.spec.plates[[p.name for p in manifest.spec.plates].index(row["plate"])].output
    )
    destination = mount / source.name
    if destination.exists():
        if destination.is_symlink() or filehash(destination) != row["checksum"]:
            raise ValueError("SD filename already exists with different content; preserve it")
        return {"status": "already_present", "filename": destination.name}
    # Exclusive creation protects existing card contents; fsync and reread before success.
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        if filehash(destination) != row["checksum"]:
            raise ValueError("SD readback checksum mismatch")
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return {"status": "exported", "filename": destination.name, "checksum": row["checksum"]}


def upload(controller, identifier, backend):
    row, path = controller.verified(identifier, staged=True)
    receipt = __import__("json").loads(row["receipt"])
    configured = os.getenv("NEPTUNE_MANUFACTURING_PRINTER", "neptune-2")
    if receipt["manifest"]["spec"]["target"]["printer"] != configured:
        raise ValueError("Artifact target does not match the configured printer identity")
    filename = identifier + ".gcode"
    boundary = "neptune" + uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
    ).encode()
    body += (path / receipt["output"]).read_bytes()
    for key in ("select", "print"):
        body += (
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\nfalse'
        ).encode()
    body += f"\r\n--{boundary}--\r\n".encode()
    request = Request(
        backend.base_url + "/api/files/local",
        data=body,
        method="POST",
        headers={
            "X-Api-Key": backend.api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with backend._opener.open(request, timeout=backend.timeout) as response:
            if response.status != 201:
                raise OctoPrintError("Upload was not acknowledged")
    except Exception:
        # Never retry an ambiguous transfer automatically.
        raise OctoPrintError(
            "Upload outcome unknown or rejected; inspect OctoPrint before retry"
        ) from None
    return {
        "status": "transport_acknowledged",
        "filename": filename,
        "selected": False,
        "print_started": False,
        "physical_completion_confirmed": False,
    }
