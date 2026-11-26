"""Versioned, strict manufacturing declarations and reproducible content identities."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Metadata(Model):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    repository: str


class Source(Model):
    engine: Literal["freecad", "cadquery"]
    document: str
    parameters: dict[str, str | float | int] = Field(default_factory=dict)
    protectedParameters: dict[str, str | float | int] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)


class Build(Model):
    recompute: Literal[True] = True
    validate_geometry: Literal[True] = Field(default=True, alias="validate")
    outputDirectory: str = "artifacts/geometry"


class Slicer(Model):
    engine: Literal["orcaslicer"] = "orcaslicer"
    project: str
    preserveProjectSettings: Literal[True] = True
    profiles: list[str] = Field(default_factory=list)


class Object(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    export: str
    cadObject: str
    meshPath: str
    meshObjectId: str
    parameterKeys: list[str] = Field(default_factory=list)
    projectObjectId: str
    # Original source offsets, in mm, from model_settings.config, applied by adapter.


class Plate(Model):
    name: str = Field(min_length=1)
    objects: list[Object] = Field(min_length=1)
    output: str = Field(pattern=r"^[A-Za-z0-9_-]+\.gcode$")


class Target(Model):
    printer: str
    transport: Literal["octoprint"] = "octoprint"


class Staging(Model):
    enabled: bool = True
    versioned: Literal[True] = True
    generatePreview: Literal[True] = True
    generateChecksum: Literal[True] = True


class Deployment(Model):
    autoBuild: bool = True
    autoSlice: bool = True
    autoStage: bool = True
    autoPrint: Literal[False] = False
    requireApproval: Literal[True] = True


class Fallback(Model):
    transport: Literal["removable-storage"] = "removable-storage"
    volume: str = Field(default="NEPTUNE", pattern=r"^[A-Za-z0-9_-]+$")


class Spec(Model):
    source: Source
    build: Build = Field(default_factory=Build)
    slicer: Slicer
    plates: list[Plate] = Field(min_length=1)
    target: Target
    staging: Staging = Field(default_factory=Staging)
    deployment: Deployment = Field(default_factory=Deployment)
    fallback: Fallback = Field(default_factory=Fallback)


class Manifest(Model):
    apiVersion: Literal["cdaprod.dev/v1alpha1"]
    kind: Literal["ManufacturingProject"]
    metadata: Metadata
    spec: Spec

    @model_validator(mode="after")
    def unique(self):
        if self.spec.deployment.autoStage and not self.spec.staging.enabled:
            raise ValueError("Automatic staging requires staging.enabled")
        plates = self.spec.plates
        objects = [o for p in plates for o in p.objects]
        for values in (
            [p.name for p in plates],
            [p.output for p in plates],
            [o.id for o in objects],
            [o.export for o in objects],
            [(o.meshPath, o.meshObjectId) for o in objects],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate plate, output, object, export, or mesh mapping")
        for obj in objects:
            if Path(obj.export).name != obj.export or not obj.export.endswith(".stl"):
                raise ValueError("Exports must be plain STL filenames")
        return self


def confined(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes project: {name}")
    return path


def load(root: Path) -> Manifest:
    manifest = Manifest.model_validate(yaml.safe_load((root / "manufacturing.yaml").read_text()))
    for name in [
        manifest.spec.source.document,
        manifest.spec.slicer.project,
        *manifest.spec.source.dependencies,
        *manifest.spec.slicer.profiles,
    ]:
        if not confined(root, name).is_file():
            raise ValueError(f"Missing input: {name}")
    return manifest


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def filehash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def source_identity(root: Path, manifest: Manifest) -> str:
    source = manifest.spec.source
    return digest(
        {
            "source": source.model_dump(),
            "files": {
                name: filehash(confined(root, name))
                for name in [source.document, *source.dependencies]
            },
        }
    )
