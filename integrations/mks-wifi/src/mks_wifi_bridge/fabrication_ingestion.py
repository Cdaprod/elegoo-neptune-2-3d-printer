"""Source-neutral submission contract. Adapters never stage or print artifacts."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .artifact_policy import ARTIFACT_EXTENSIONS
from .octoprint_local_import import validate_gcode_filename


@dataclass(frozen=True)
class ArtifactSubmission:
    filename: str
    provider: str
    source_uri: str
    artifact_kind: str = "execution_gcode"
    schema_version: int = 1
    expected_sha256: str | None = None

    def validate(self):
        allowed = ARTIFACT_EXTENSIONS
        if self.schema_version != 1 or self.artifact_kind not in allowed:
            raise ValueError("Unsupported submission schema or artifact type")
        if not self.provider or len(self.provider) > 80 or len(self.source_uri) > 2048:
            raise ValueError("Invalid source provenance")
        if any(ord(c) < 32 for c in self.provider + self.source_uri):
            raise ValueError("Invalid source provenance")
        if self.expected_sha256 is not None and not re.fullmatch(
            r"[a-f0-9]{64}", self.expected_sha256
        ):
            raise ValueError("Invalid expected artifact digest")
        if self.artifact_kind == "execution_gcode":
            validate_gcode_filename(self.filename, b"x")
        elif (
            not isinstance(self.filename, str)
            or not self.filename
            or len(self.filename) > 240
            or "/" in self.filename
            or "\\" in self.filename
            or ".." in self.filename
            or self.filename.startswith(".")
            or any(ord(c) < 32 for c in self.filename)
            or Path(self.filename).suffix.lower() not in allowed[self.artifact_kind]
        ):
            raise ValueError("Filename does not match the declared artifact type")


class FabricationIngestion:
    def __init__(self, archive):
        self.archive = archive

    def submit(self, submission: ArtifactSubmission, stream: BinaryIO):
        submission.validate()
        return self.archive.persist_stream(stream, submission)

    def submit_path(self, path, submission: ArtifactSubmission):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Source must be a regular file")
        with path.open("rb") as stream:
            before = path.stat()
            result = self.submit(submission, stream)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError("Source changed during ingestion; rescan required")
            return result
