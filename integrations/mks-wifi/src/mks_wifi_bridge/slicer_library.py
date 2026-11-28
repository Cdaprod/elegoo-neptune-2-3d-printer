"""Read-only, bounded 3MF body extraction. No slicer invocation or embedded code execution."""

import hashlib
import json
import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from .manufacturing.orca import CORE, PROD, metadata
from .manufacturing.preview import transform

VERSION = "3mf-bodies-v1"
NS = {"c": CORE}
IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"
UNITS = {
    "micron": 0.001,
    "millimeter": 1,
    "centimeter": 10,
    "meter": 1000,
    "inch": 25.4,
    "foot": 304.8,
}


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Document type/entity declarations are not accepted")
    return ET.fromstring(data)


def safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(p in {"..", "."} for p in name.split("/")):
        raise ValueError("Unsafe package member")
    return name


def write_stl(path, vertices, triangles):
    with path.open("wb") as output:
        output.write(b"Neptune Observatory geometry derivative".ljust(80, b"\0"))
        output.write(struct.pack("<I", len(triangles)))
        for triangle in triangles:
            values = [value for index in triangle for value in vertices[index]]
            output.write(struct.pack("<12fH", 0, 0, 0, *values, 0))


def display_mesh(vertices, triangles, limit=120000):
    """Vertex clustering collapses edges; never creates the holes of triangle sampling."""
    if len(triangles) <= limit:
        return vertices, triangles, {"method": "full_mesh", "source_triangles": len(triangles)}
    low = [min(v[a] for v in vertices) for a in range(3)]
    extent = max(max(v[a] for v in vertices) - low[a] for a in range(3)) or 1
    resolution = 256
    while resolution >= 2:
        cell = extent / resolution
        groups, mapping, sums = {}, [], []
        for vertex in vertices:
            key = tuple(int((vertex[a] - low[a]) / cell) for a in range(3))
            index = groups.get(key)
            if index is None:
                index = len(sums)
                groups[key] = index
                sums.append([0.0, 0.0, 0.0, 0])
            mapping.append(index)
            for a in range(3):
                sums[index][a] += vertex[a]
            sums[index][3] += 1
        reduced, seen = [], set()
        for triangle in triangles:
            tri = tuple(mapping[index] for index in triangle)
            key = tuple(sorted(tri))
            if len(set(tri)) == 3 and key not in seen:
                reduced.append(tri)
                seen.add(key)
        if len(reduced) <= limit and reduced:
            points = [tuple(v[a] / v[3] for a in range(3)) for v in sums]
            return (
                points,
                reduced,
                {
                    "method": "vertex_clustering",
                    "cell_mm": cell,
                    "source_triangles": len(triangles),
                },
            )
        resolution = int(resolution * 0.7)
    raise ValueError("Mesh cannot fit the display budget")


def extract_project(source, output):
    source, output = Path(source), Path(output)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 50 * 1024**2:
        raise ValueError("Supply a regular 3MF project no larger than 50 MiB")
    with source.open("rb") as stream:
        before = source.stat()
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    cached = output / "manifest.json"
    if cached.exists():
        value = json.loads(cached.read_text())
        if value.get("source_sha256") == sha and value.get("processor") == VERSION:
            if all((output / b["geometry_file"]).is_file() for b in value["bodies"]):
                return value
    with ZipFile(source) as package:
        entries = package.infolist()
        if len(entries) > 3000 or len({e.filename for e in entries}) != len(entries):
            raise ValueError("Excessive or duplicate package members")
        if sum(e.file_size for e in entries) > 512 * 1024**2 or any(
            e.file_size > 128 * 1024**2 for e in entries
        ):
            raise ValueError("Package expansion budget exceeded")
        names = {safe_member(e.filename): e for e in entries if not e.is_dir()}
        models = {
            name: xml(package.read(name)) for name in names if name.lower().endswith(".model")
        }
        main = "3D/3dmodel.model"
        if main not in models:
            raise ValueError("3MF has no main model")
        unit = models[main].get("unit", "millimeter")
        if unit not in UNITS:
            raise ValueError("Unsupported 3MF unit")
        factor = UNITS[unit]
        settings = (
            xml(package.read("Metadata/model_settings.config"))
            if "Metadata/model_settings.config" in names
            else ET.Element("config")
        )
        labels, subtypes = {}, {}
        for obj in settings.findall("object"):
            for part in obj.findall("part"):
                values = metadata(part)
                labels[part.get("id")] = values.get("name")
                subtypes[part.get("id")] = part.get("subtype", "normal_part")
        objects, meshes, bodies = {}, {}, []
        for member, root in models.items():
            if root.get("unit", unit) != unit:
                raise ValueError("Mixed member units require explicit conversion")
            for obj in root.findall("c:resources/c:object", NS):
                key = (member, obj.get("id"))
                if key in objects:
                    raise ValueError("Duplicate resource object identifier")
                objects[key] = obj
                mesh = obj.find("c:mesh", NS)
                if mesh is None:
                    continue
                vertices = [
                    tuple(float(v.get(a)) for a in ("x", "y", "z"))
                    for v in mesh.findall("c:vertices/c:vertex", NS)
                ]
                triangles = [
                    tuple(int(t.get(a)) for a in ("v1", "v2", "v3"))
                    for t in mesh.findall("c:triangles/c:triangle", NS)
                ]
                if not vertices or not triangles or len(triangles) > 2000000:
                    raise ValueError("Empty or excessive mesh")
                if any(not math.isfinite(v) or abs(v) > 1e7 for point in vertices for v in point):
                    raise ValueError("Invalid coordinate")
                if any(index < 0 or index >= len(vertices) for tri in triangles for index in tri):
                    raise ValueError("Invalid triangle index")
                meshes[key] = (vertices, triangles)
                points = [tuple(value * factor for value in point) for point in vertices]
                fingerprint = hashlib.sha256()
                for point in points:
                    fingerprint.update(struct.pack("<3d", *point))
                for tri in triangles:
                    fingerprint.update(struct.pack("<3I", *tri))
                display, faces, lod = display_mesh(points, triangles)
                identifier = hashlib.sha256((member + "#" + key[1]).encode()).hexdigest()[:20]
                filename = "body-" + identifier + ".stl"
                write_stl(output / filename, display, faces)
                bodies.append(
                    {
                        "resource": member,
                        "object_id": key[1],
                        "name": labels.get(key[1]) or obj.get("name") or PurePosixPath(member).stem,
                        "subtype": subtypes.get(key[1], "normal_part"),
                        "geometry_file": filename,
                        "mesh_fingerprint": fingerprint.hexdigest(),
                        "fingerprint_method": "ordered_indexed_mesh_v1",
                        "display": lod,
                        "rendered_triangles": len(faces),
                        "instances": [],
                    }
                )
        by_key = {(b["resource"], b["object_id"]): b for b in bodies}

        def leaves(key, chain, seen):
            if key in seen or len(seen) > 32:
                raise ValueError("Cyclic or excessive component graph")
            obj = objects.get(key)
            if obj is None:
                raise ValueError("Missing component reference")
            if key in meshes:
                yield key, chain
            for comp in obj.findall("c:components/c:component", NS):
                member = comp.get("{" + PROD + "}path", key[0]).lstrip("/")
                safe_member(member)
                matrix = comp.get("transform", IDENTITY)
                transform((0, 0, 0), matrix)
                yield from leaves((member, comp.get("objectid")), chain + [matrix], seen | {key})

        build = []
        for index, item in enumerate(models[main].findall("c:build/c:item", NS)):
            matrix = item.get("transform", IDENTITY)
            transform((0, 0, 0), matrix)
            record = {
                "object_id": item.get("objectid"),
                "instance": index,
                "transform": matrix,
                "bodies": [],
            }
            for key, chain in leaves((main, item.get("objectid")), [matrix], set()):
                record["bodies"].append(
                    {"resource": key[0], "object_id": key[1], "transforms": chain}
                )
                by_key[key]["instances"].append(
                    {"build_index": index, "object_id": item.get("objectid"), "transforms": chain}
                )
            build.append(record)
        plates = []
        for plate in settings.findall("plate"):
            values = metadata(plate)
            plates.append(
                {
                    "id": values.get("plater_id"),
                    "name": values.get("plater_name") or "Plate",
                    "instances": [
                        metadata(instance) for instance in plate.findall("model_instance")
                    ],
                }
            )
        if not plates:
            plates = [
                {
                    "id": "1",
                    "name": "Build plate",
                    "instances": [{"object_id": row["object_id"]} for row in build],
                }
            ]
        for plate in plates:
            ids = {item.get("object_id") for item in plate["instances"]}
            points = []
            triangles = []
            for item in build:
                if item["object_id"] not in ids:
                    continue
                for ref in item["bodies"]:
                    vertices, faces = meshes[(ref["resource"], ref["object_id"])]
                    offset = len(points)
                    for point in vertices:
                        for matrix in reversed(ref["transforms"]):
                            point = transform(point, matrix)
                        points.append(tuple(v * factor for v in point))
                    triangles.extend(tuple(offset + i for i in face) for face in faces)
            if points and triangles:
                points, triangles, lod = display_mesh(points, triangles)
                filename = (
                    "plate-" + hashlib.sha256(str(plate["id"]).encode()).hexdigest()[:12] + ".stl"
                )
                write_stl(output / filename, points, triangles)
                plate.update(geometry_file=filename, display=lod)
        project_settings = {}
        if "Metadata/project_settings.config" in names:
            raw = json.loads(package.read("Metadata/project_settings.config"))
            project_settings = {
                k: v
                for k, v in raw.items()
                if not any(
                    word in k.lower()
                    for word in (
                        "password",
                        "token",
                        "apikey",
                        "api_key",
                        "secret",
                        "print_host",
                        "printhost",
                        "wifi",
                    )
                )
            }
        result = {
            "schema_version": 1,
            "processor": VERSION,
            "source_sha256": sha,
            "source_name": source.name,
            "units": "mm",
            "original_unit": unit,
            "bodies": bodies,
            "build": build,
            "plates": plates,
            "settings": project_settings,
            "embedded_previews": [
                name for name in names if name.lower().endswith((".png", ".jpg", ".jpeg"))
            ],
            "identity": "body observations; logical model equivalence and authority unapproved",
        }
    after = source.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError("Source changed during extraction")
    temporary = output / "manifest.partial"
    temporary.write_text(json.dumps(result))
    temporary.replace(cached)
    return result


class SlicerLibrary:
    """3MF observations share canonical originals, model registry and transactions."""

    def __init__(self, archive):
        self.archive = archive
        with archive.db() as db:
            db.executescript("""
                INSERT OR IGNORE INTO fabrication_schema VALUES(9);
                CREATE TABLE IF NOT EXISTS slicer_projects(
                    sha256 TEXT PRIMARY KEY REFERENCES fabrication_artifacts(sha256),
                    processor TEXT NOT NULL,manifest TEXT NOT NULL,registered_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slicer_bodies(
                    source_sha256 TEXT REFERENCES slicer_projects(sha256),resource TEXT NOT NULL,
                    object_id TEXT NOT NULL,mesh_sha256 TEXT NOT NULL,
                    model_id TEXT REFERENCES models(id),
                    revision_id TEXT REFERENCES model_revisions(id),
                    mesh_fingerprint TEXT NOT NULL,PRIMARY KEY(source_sha256,resource,object_id));
                CREATE TABLE IF NOT EXISTS source_file_observations(
                    discovery_id TEXT NOT NULL,sha256 TEXT REFERENCES fabrication_artifacts(sha256),
                    source_host TEXT NOT NULL,path TEXT NOT NULL,metadata TEXT NOT NULL,
                    PRIMARY KEY(discovery_id,sha256));
                CREATE INDEX IF NOT EXISTS slicer_mesh_candidates
                    ON slicer_bodies(mesh_fingerprint);
                CREATE TABLE IF NOT EXISTS model_identity_status(
                    model_id TEXT PRIMARY KEY REFERENCES models(id),state TEXT NOT NULL,
                    evidence TEXT NOT NULL);
            """)

    def register(self, sha, manifest):
        import uuid

        from .fabrication_archive import now
        from .model_registry import ModelRegistry

        ModelRegistry.digest(sha)
        self.archive.verified(sha)
        if (
            manifest.get("source_sha256") != sha
            or manifest.get("schema_version") != 1
            or manifest.get("processor") != VERSION
        ):
            raise ValueError("Source identity and supported processing version required")
        bodies = manifest.get("bodies")
        plates = manifest.get("plates", [])
        if (
            not isinstance(bodies, list)
            or len(bodies) > 1000
            or not isinstance(plates, list)
            or len(plates) > 100
        ):
            raise ValueError("Project body or plate budget exceeded")
        # Check package object references without recomputing CAD on the Pi.
        with ZipFile(self.archive.verified(sha)) as package:
            references = {}
            for body in bodies:
                member = safe_member(body["resource"])
                if member not in references:
                    info = package.getinfo(member)
                    if info.file_size > 128 * 1024**2:
                        raise ValueError("Resource exceeds XML budget")
                    references[member] = {
                        obj.get("id")
                        for obj in xml(package.read(member)).findall("c:resources/c:object", NS)
                        if obj.find("c:mesh", NS) is not None
                    }
                if body["object_id"] not in references[member]:
                    raise ValueError("Body is absent from source package")
        for item in [*bodies, *[p for p in plates if p.get("sha256")]]:
            ModelRegistry.digest(item["sha256"])
            self.archive.verified(item["sha256"])
            if item in bodies:
                ModelRegistry.text(item["name"], 200)
                ModelRegistry.digest(item["mesh_fingerprint"])
        result = []
        with self.archive.db() as db:
            observation = manifest.get("source_observation")
            if observation and isinstance(observation, dict) and observation.get("path"):
                source_host = str(observation.get("source_host", "publisher"))
                path = str(observation["path"])
                discovery_id = hashlib.sha256((source_host + "\0" + path).encode()).hexdigest()
                db.execute(
                    "INSERT OR REPLACE INTO source_file_observations VALUES(?,?,?,?,?)",
                    (discovery_id, sha, source_host, path, json.dumps(observation)),
                )
            kind = db.execute(
                "SELECT artifact_kind FROM artifact_custody WHERE sha256=?", (sha,)
            ).fetchone()
            if not kind or kind[0] != "slicer_project":
                raise ValueError("Source must be a typed 3MF project")
            db.execute(
                "INSERT OR IGNORE INTO slicer_projects VALUES(?,?,?,?)",
                (sha, VERSION, json.dumps(manifest), now()),
            )
            for body in bodies:
                key = (sha, body["resource"], body["object_id"])
                old = db.execute(
                    "SELECT model_id,revision_id,mesh_sha256 FROM slicer_bodies "
                    "WHERE source_sha256=? AND resource=? AND object_id=?",
                    key,
                ).fetchone()
                if old:
                    if old["mesh_sha256"] != body["sha256"]:
                        raise ValueError(
                            "Existing extraction differs; a versioned derivative is required"
                        )
                    result.append({"model_id": old["model_id"], "revision_id": old["revision_id"]})
                    continue
                custody = db.execute(
                    "SELECT artifact_kind FROM artifact_custody WHERE sha256=?", (body["sha256"],)
                ).fetchone()
                if not custody or custody[0] != "manufacturing_geometry":
                    raise ValueError("Body must reference archived geometry")
                identity, revision = uuid.uuid4().hex, uuid.uuid4().hex
                label = body["name"]
                label = label[:-4] if label.lower().endswith(".stl") else label
                db.execute("INSERT INTO models VALUES(?,?,?)", (identity, label, now()))
                db.execute(
                    "INSERT INTO model_revisions VALUES(?,?,?,?)",
                    (revision, identity, "3MF body observation", now()),
                )
                db.execute(
                    "INSERT INTO model_identity_status VALUES(?,?,?)",
                    (
                        identity,
                        "candidate",
                        "Extracted body observation; model equivalence and authority unapproved",
                    ),
                )
                for artifact, role in [
                    (body["sha256"], "manufacturing_geometry"),
                    (sha, "slicer_configuration"),
                ]:
                    db.execute(
                        "INSERT INTO model_representations VALUES(?,?,?,1)",
                        (revision, artifact, role),
                    )
                db.execute(
                    "INSERT INTO model_contexts VALUES(?,?)",
                    (
                        identity,
                        "3mf-object:" + sha + "#" + body["resource"] + ":" + body["object_id"],
                    ),
                )
                db.execute(
                    "INSERT INTO slicer_bodies VALUES(?,?,?,?,?,?,?)",
                    (*key, body["sha256"], identity, revision, body["mesh_fingerprint"]),
                )
                db.execute(
                    "INSERT OR IGNORE INTO artifact_relationships VALUES(?,?,'derived_from')",
                    (body["sha256"], sha),
                )
                row = db.execute(
                    "SELECT metadata FROM fabrication_artifacts WHERE sha256=?", (body["sha256"],)
                ).fetchone()
                data = json.loads(row["metadata"])
                data.update(
                    source_3mf=sha,
                    processor=VERSION,
                    units="mm",
                    geometry_provenance="3MF mesh extraction; not a physical measurement",
                    display=body.get("display", {}),
                )
                db.execute(
                    "UPDATE fabrication_artifacts SET metadata=? WHERE sha256=?",
                    (json.dumps(data), body["sha256"]),
                )
                ModelRegistry.audit(
                    db,
                    "import_3mf_body",
                    {
                        "model_id": identity,
                        "source": sha,
                        "identity": "candidate",
                        "resource": body["resource"],
                        "object_id": body["object_id"],
                    },
                )
                result.append({"model_id": identity, "revision_id": revision})
            for plate in plates:
                if plate.get("sha256"):
                    db.execute(
                        "INSERT OR IGNORE INTO artifact_relationships VALUES(?,?,'derived_from')",
                        (plate["sha256"], sha),
                    )
        return {
            "source_sha256": sha,
            "bodies": result,
            "plates": len(plates),
            "identity": "candidate",
            "physical_execution": False,
        }

    def projects(self):
        with self.archive.db() as db:
            rows = db.execute(
                "SELECT p.sha256,p.registered_at,a.filename,p.manifest FROM slicer_projects p "
                "JOIN fabrication_artifacts a ON a.sha256=p.sha256 "
                "ORDER BY p.registered_at DESC,p.sha256 DESC LIMIT 200"
            ).fetchall()
        result = []
        for row in rows:
            data = json.loads(row["manifest"])
            preview = next((p.get("sha256") for p in data["plates"] if p.get("sha256")), None)
            result.append(
                {
                    "sha256": row["sha256"],
                    "filename": row["filename"],
                    "registered_at": row["registered_at"],
                    "body_count": len(data["bodies"]),
                    "plate_count": len(data["plates"]),
                    "preview_sha256": preview,
                }
            )
        return result

    def detail(self, sha):
        with self.archive.db() as db:
            row = db.execute(
                "SELECT manifest FROM slicer_projects WHERE sha256=?", (sha,)
            ).fetchone()
            if row is None:
                raise ValueError("Project has not been processed")
        return json.loads(row["manifest"])


def install(app, read_auth, write_auth, archive):
    import asyncio
    import sqlite3

    from fastapi import Depends, HTTPException, Request

    library = SlicerLibrary(archive)
    gate = asyncio.Semaphore(1)

    @app.get("/api/fabrication/slicer-projects", dependencies=[Depends(read_auth)])
    def projects():
        return {"schema_version": 1, "items": library.projects()}

    @app.get("/api/fabrication/slicer-projects/{sha}", dependencies=[Depends(read_auth)])
    def project(sha: str):
        try:
            return library.detail(sha)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/fabrication/slicer-projects/{sha}/register", dependencies=[Depends(write_auth)])
    async def register(sha: str, request: Request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 2 * 1024**2:
                raise HTTPException(413, "Project receipt exceeds metadata budget")
        try:
            manifest = json.loads(data)
            if not isinstance(manifest, dict):
                raise ValueError("Expected a project receipt")
            async with gate:
                return await asyncio.to_thread(library.register, sha, manifest)
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            sqlite3.Error,
            BadZipFile,
            ET.ParseError,
        ) as error:
            raise HTTPException(422, str(error)) from error

    return library
