"""Model-centric declarations in the existing fabrication database; no source execution."""

import hashlib
import json
import uuid

from .fabrication_archive import now

ROLES = {
    "native_design",
    "parameters",
    "manufacturing_geometry",
    "slicer_configuration",
    "executable_gcode",
    "as_built",
    "presentation",
}


class ModelRegistry:
    def __init__(self, archive):
        self.archive = archive
        with archive.db() as db:
            db.executescript("""
                INSERT OR IGNORE INTO fabrication_schema VALUES(6);
                CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at
                TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_revisions(id TEXT PRIMARY KEY,model_id TEXT
                NOT NULL
                REFERENCES models(id),label TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revision_parents(revision_id TEXT REFERENCES
                model_revisions(id),parent_id TEXT REFERENCES model_revisions(id),PRIMARY
                KEY(revision_id,parent_id));
                CREATE TABLE IF NOT EXISTS model_representations(revision_id TEXT REFERENCES
                model_revisions(id),sha256 TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT
                NULL,PRIMARY KEY(revision_id,sha256,role));
                CREATE TABLE IF NOT EXISTS model_authority(id TEXT PRIMARY KEY,revision_id TEXT
                REFERENCES model_revisions(id),role TEXT NOT NULL,sha256 TEXT NOT NULL,state
                TEXT NOT
                NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_components(assembly_revision_id TEXT REFERENCES
                model_revisions(id),component_revision_id TEXT REFERENCES model_revisions(id),label
                TEXT NOT NULL,PRIMARY KEY(assembly_revision_id,component_revision_id,label));
                CREATE TABLE IF NOT EXISTS model_contexts(model_id TEXT REFERENCES
                models(id),context
                TEXT NOT NULL,PRIMARY KEY(model_id,context));
                CREATE TABLE IF NOT EXISTS fabrication_baselines(id TEXT PRIMARY
                KEY,revision_id TEXT
                REFERENCES model_revisions(id),manifest TEXT NOT NULL,approved_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS registry_audit(id TEXT PRIMARY KEY,action TEXT NOT
                NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            """)

    @staticmethod
    def text(value, limit=160):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Invalid declaration text")
        return value.strip()

    @staticmethod
    def digest(value):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("Invalid artifact digest")
        return value

    @staticmethod
    def audit(db, action, payload):
        db.execute(
            "INSERT INTO registry_audit VALUES(?,?,?,?)",
            (uuid.uuid4().hex, action, json.dumps(payload, sort_keys=True), now()),
        )

    def create(self, name, contexts=()):
        name = self.text(name)
        if not isinstance(contexts, (list, tuple)):
            raise ValueError("Contexts must be an optional list")
        contexts = [self.text(c, 2048) for c in contexts]
        if len(contexts) > 30:
            raise ValueError("Too many organizational contexts")
        identity = uuid.uuid4().hex
        with self.archive.db() as db:
            db.execute("INSERT INTO models VALUES(?,?,?)", (identity, name, now()))
            for context in contexts:
                db.execute("INSERT OR IGNORE INTO model_contexts VALUES(?,?)", (identity, context))
            self.audit(db, "create_model", {"model_id": identity})
        return {"id": identity, "name": name}

    def revision(self, model_id, label, parents=()):
        label = self.text(label)
        if not isinstance(parents, (list, tuple)):
            raise ValueError("Parents must be a revision list")
        if len(parents) > 8:
            raise ValueError("Too many parent revisions")
        identity = uuid.uuid4().hex
        with self.archive.db() as db:
            for parent in parents:
                row = db.execute(
                    "SELECT model_id FROM model_revisions WHERE id=?", (parent,)
                ).fetchone()
                if row is None or row[0] != model_id:
                    raise ValueError("Revision parents must belong to the same model")
            db.execute(
                "INSERT INTO model_revisions VALUES(?,?,?,?)", (identity, model_id, label, now())
            )
            for parent in set(parents):
                db.execute("INSERT INTO revision_parents VALUES(?,?)", (identity, parent))
            self.audit(db, "create_revision", {"revision_id": identity, "model_id": model_id})
        return {"id": identity, "model_id": model_id, "label": label}

    def representation(self, revision_id, sha, role, active=True):
        self.digest(sha)
        if role not in ROLES or type(active) is not bool:
            raise ValueError("Invalid representation role/state")
        with self.archive.db() as db:
            if (
                not active
                and db.execute(
                    "SELECT 1 FROM model_authority WHERE revision_id=? AND sha256=? AND "
                    "role=? AND state='approved'",
                    (revision_id, sha, role),
                ).fetchone()
            ):
                raise ValueError("Supersede approved authority before detaching its representation")
            db.execute(
                "INSERT INTO model_representations VALUES(?,?,?,?) ON "
                "CONFLICT(revision_id,sha256,role) DO UPDATE SET active=excluded.active",
                (revision_id, sha, role, int(active)),
            )
            self.audit(
                db,
                "representation",
                {"revision_id": revision_id, "sha256": sha, "role": role, "active": active},
            )
        return {"sha256": sha, "role": role, "active": active}

    def authority(self, revision_id, sha, role, approve=False):
        self.digest(sha)
        if role not in ROLES or type(approve) is not bool:
            raise ValueError("Invalid authority declaration")
        if approve:
            with self.archive.db() as db:
                if not db.execute(
                    "SELECT 1 FROM fabrication_artifacts WHERE sha256=?", (sha,)
                ).fetchone():
                    raise ValueError("Approve only a fingerprinted artifact declaration")
        identity = uuid.uuid4().hex
        with self.archive.db() as db:
            if not db.execute(
                "SELECT 1 FROM model_representations WHERE revision_id=? AND sha256=? "
                "AND role=? AND active=1",
                (revision_id, sha, role),
            ).fetchone():
                raise ValueError("Declare an active representation first")
            if approve:
                db.execute(
                    "UPDATE model_authority SET state='superseded' WHERE revision_id=? AND "
                    "role=? AND state='approved'",
                    (revision_id, role),
                )
            db.execute(
                "INSERT INTO model_authority VALUES(?,?,?,?,?,?)",
                (identity, revision_id, role, sha, "approved" if approve else "candidate", now()),
            )
            self.audit(
                db,
                "authority",
                {"id": identity, "approved": approve, "source": "operator_supplied"},
            )
        return {"id": identity, "state": "approved" if approve else "candidate"}

    def component(self, assembly, component, label):
        label = self.text(label)
        with self.archive.db() as db:
            cycle = db.execute(
                "WITH RECURSIVE children(id) AS (SELECT component_revision_id FROM "
                "model_components WHERE assembly_revision_id=? UNION SELECT "
                "c.component_revision_id FROM model_components c JOIN children x ON "
                "c.assembly_revision_id=x.id) SELECT 1 FROM children WHERE id=?",
                (component, assembly),
            ).fetchone()
            if assembly == component or cycle:
                raise ValueError("Component cycle rejected")
            db.execute(
                "INSERT OR IGNORE INTO model_components VALUES(?,?,?)", (assembly, component, label)
            )
            self.audit(
                db, "component", {"assembly": assembly, "component": component, "label": label}
            )
        return {"component_revision_id": component, "label": label}

    def manifest(self, revision_id):
        with self.archive.db() as db:
            revision = db.execute(
                "SELECT * FROM model_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            if revision is None:
                raise ValueError("Unknown model revision")
            model = dict(
                db.execute("SELECT * FROM models WHERE id=?", (revision["model_id"],)).fetchone()
            )
            result = {
                "schema_version": 1,
                "kind": "ModelManifest",
                "model": model,
                "revision": dict(revision),
            }
            for key, sql in {
                "parents": "SELECT parent_id FROM revision_parents WHERE revision_id=?",
                "representations": "SELECT sha256,role,active FROM model_representations WHERE "
                "revision_id=?",
                "authorities": "SELECT * FROM model_authority WHERE revision_id=?",
                "components": "SELECT component_revision_id,label FROM model_components WHERE "
                "assembly_revision_id=?",
            }.items():
                result[key] = [dict(row) for row in db.execute(sql, (revision_id,))]
        return result

    def import_manifest(self, manifest, approve=False):
        """Portable declarations, atomically imported. Approval is a separate explicit choice."""
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "ModelManifest"
            or type(approve) is not bool
        ):
            raise ValueError("Unsupported model manifest")
        model = manifest["model"]
        revision = manifest["revision"]
        model_id = uuid.UUID(model["id"]).hex
        revision_id = uuid.UUID(revision["id"]).hex
        name = self.text(model["name"])
        label = self.text(revision["label"])
        if uuid.UUID(revision["model_id"]).hex != model_id:
            raise ValueError("Revision/model identity mismatch")
        with self.archive.db() as db:
            existing = db.execute("SELECT name FROM models WHERE id=?", (model_id,)).fetchone()
            if existing and existing[0] != name:
                raise ValueError("Existing model name conflicts; reconcile explicitly")
            db.execute("INSERT OR IGNORE INTO models VALUES(?,?,?)", (model_id, name, now()))
            existing = db.execute(
                "SELECT model_id,label FROM model_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            if existing and tuple(existing) != (model_id, label):
                raise ValueError("Existing revision declaration conflicts")
            db.execute(
                "INSERT OR IGNORE INTO model_revisions VALUES(?,?,?,?)",
                (revision_id, model_id, label, now()),
            )
            for parent in manifest.get("parents", []):
                parent_id = uuid.UUID(parent["parent_id"]).hex
                row = db.execute(
                    "SELECT model_id FROM model_revisions WHERE id=?", (parent_id,)
                ).fetchone()
                if row is None or row[0] != model_id or parent_id == revision_id:
                    raise ValueError("Unresolved or invalid revision parent")
                cycle = db.execute(
                    "WITH RECURSIVE ancestors(id) AS (SELECT parent_id FROM "
                    "revision_parents WHERE revision_id=? UNION SELECT p.parent_id "
                    "FROM revision_parents p JOIN ancestors a ON p.revision_id=a.id) "
                    "SELECT 1 FROM ancestors WHERE id=?",
                    (parent_id, revision_id),
                ).fetchone()
                if cycle:
                    raise ValueError("Revision cycle rejected")
                db.execute(
                    "INSERT OR IGNORE INTO revision_parents VALUES(?,?)", (revision_id, parent_id)
                )
            for representation in manifest.get("representations", []):
                sha = self.digest(representation["sha256"])
                role = representation["role"]
                if role not in ROLES or representation.get("active", 1) not in (0, 1, False, True):
                    raise ValueError("Invalid representation")
                db.execute(
                    "INSERT OR IGNORE INTO model_representations VALUES(?,?,?,?)",
                    (revision_id, sha, role, int(representation.get("active", 1))),
                )
            approved_roles = set()
            for authority in manifest.get("authorities", []):
                sha = self.digest(authority["sha256"])
                role = authority["role"]
                state = authority["state"]
                if role not in ROLES or state not in {"candidate", "approved", "superseded"}:
                    raise ValueError("Invalid scoped authority")
                if state == "approved":
                    if not approve:
                        state = "candidate"
                    elif role in approved_roles:
                        raise ValueError("Competing approved authorities")
                    else:
                        approved_roles.add(role)
                        if not db.execute(
                            "SELECT 1 FROM fabrication_artifacts WHERE sha256=?", (sha,)
                        ).fetchone():
                            raise ValueError("Fingerprint the referenced authority artifact first")
                        db.execute(
                            "UPDATE model_authority SET state='superseded' WHERE revision_id=? "
                            "AND role=? AND state='approved'",
                            (revision_id, role),
                        )
                representation = db.execute(
                    "SELECT active FROM model_representations "
                    "WHERE revision_id=? AND sha256=? AND role=?",
                    (revision_id, sha, role),
                ).fetchone()
                if representation is None or (state == "approved" and not representation[0]):
                    raise ValueError("Authority has no eligible representation")
                identity = uuid.UUID(authority["id"]).hex
                old = db.execute(
                    "SELECT revision_id,role,sha256 FROM model_authority WHERE id=?", (identity,)
                ).fetchone()
                if old and tuple(old) != (revision_id, role, sha):
                    raise ValueError("Authority identity conflicts")
                db.execute(
                    "INSERT OR IGNORE INTO model_authority VALUES(?,?,?,?,?,?)",
                    (identity, revision_id, role, sha, state, now()),
                )
                if approve and state == "approved":
                    db.execute(
                        "UPDATE model_authority SET state='approved' WHERE id=?", (identity,)
                    )
            for component in manifest.get("components", []):
                component_id = uuid.UUID(component["component_revision_id"]).hex
                cycle = db.execute(
                    "WITH RECURSIVE children(id) AS (SELECT component_revision_id FROM "
                    "model_components WHERE assembly_revision_id=? UNION SELECT "
                    "c.component_revision_id FROM model_components c JOIN children x "
                    "ON c.assembly_revision_id=x.id) SELECT 1 FROM children WHERE id=?",
                    (component_id, revision_id),
                ).fetchone()
                if component_id == revision_id or cycle:
                    raise ValueError("Component cycle rejected")
                db.execute(
                    "INSERT OR IGNORE INTO model_components VALUES(?,?,?)",
                    (revision_id, component_id, self.text(component["label"])),
                )
            self.audit(
                db,
                "import_manifest",
                {"model_id": model_id, "revision_id": revision_id, "approval_requested": approve},
            )
        return {"model_id": model_id, "revision_id": revision_id, "approved": approve}

    def baseline(self, revision_id):
        manifest = self.manifest(revision_id)
        approved = [row for row in manifest["authorities"] if row["state"] == "approved"]
        if not any(row["role"] == "executable_gcode" for row in approved):
            raise ValueError("Approve exact executable G-code authority first")
        for row in approved:
            with self.archive.db() as db:
                if not db.execute(
                    "SELECT 1 FROM fabrication_artifacts WHERE sha256=?", (row["sha256"],)
                ).fetchone():
                    raise ValueError("Baseline authority has no fingerprinted artifact declaration")
        component_inputs = []
        pending = list(manifest["components"])
        seen = set()
        while pending:
            component = pending.pop()
            identity = component["component_revision_id"]
            if identity in seen:
                continue
            seen.add(identity)
            child = self.manifest(identity)
            authorities = [row for row in child["authorities"] if row["state"] == "approved"]
            if not authorities:
                raise ValueError("Resolve component authority before approving baseline")
            component_inputs.append({"revision_id": identity, "authorities": authorities})
            pending.extend(child["components"])
        payload = {
            "schema_version": 1,
            "model_revision_id": revision_id,
            "authorities": approved,
            "components": manifest["components"],
            "component_inputs": sorted(component_inputs, key=lambda item: item["revision_id"]),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        with self.archive.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO fabrication_baselines VALUES(?,?,?,?)",
                (identity, revision_id, encoded, now()),
            )
            self.audit(db, "approve_baseline", {"id": identity})
        return {"id": identity, "manifest": payload}

    def library(self):
        with self.archive.db() as db:
            models = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM models ORDER BY created_at DESC,id DESC LIMIT 200"
                )
            ]
            for model in models:
                preview = db.execute(
                    "SELECT r.id AS revision_id,r.label,p.sha256,p.role,a.filename, "
                    "c.archived,c.artifact_kind, "
                    "EXISTS(SELECT 1 FROM model_authority x WHERE x.revision_id=r.id "
                    "AND x.sha256=p.sha256 AND x.role=p.role "
                    "AND x.state='approved') AS approved "
                    "FROM model_revisions r JOIN model_representations p ON p.revision_id=r.id "
                    "LEFT JOIN fabrication_artifacts a ON a.sha256=p.sha256 "
                    "LEFT JOIN artifact_custody c ON c.sha256=p.sha256 "
                    "WHERE r.model_id=? AND p.active=1 "
                    "ORDER BY (lower(a.filename) LIKE '%.stl' OR "
                    "c.artifact_kind='execution_gcode') DESC,approved DESC, "
                    "r.created_at DESC LIMIT 1",
                    (model["id"],),
                ).fetchone()
                model["preview"] = dict(preview) if preview else None
        return models

    def import_inventory(self, report):
        """Index observation metadata only. Does not open/copy/hydrate any source."""
        if report.get("schema_version") != 1 or report.get("read_only") is not True:
            raise ValueError("Expected a read-only engineering inventory")
        count = 0
        with self.archive.db() as db:
            for item in report.get("files", []):
                sha = item.get("sha256")
                if not sha or item.get("context") == "test_or_generated":
                    continue
                self.digest(sha)
                path = self.text(item["path"], 2048)
                kind = self.text(item["kind"], 80)
                size = item["size_bytes"]
                if not isinstance(size, int) or size < 0:
                    raise ValueError("Invalid observed artifact size")
                inserted = db.execute(
                    "INSERT OR IGNORE INTO fabrication_artifacts VALUES(?,?,?,?,?)",
                    (
                        sha,
                        path.rsplit("/", 1)[-1],
                        size,
                        now(),
                        json.dumps(
                            {
                                "artifact_kind": kind,
                                "identity_evidence": "inventory_sha256",
                                "authority": "unresolved",
                            }
                        ),
                    ),
                ).rowcount
                if inserted:
                    db.execute("INSERT INTO artifact_custody VALUES(?,?,?)", (sha, 0, kind))
                db.execute(
                    "INSERT OR REPLACE INTO artifact_locations VALUES(?,?,?,?)",
                    ("engineering_inventory", path, sha, now()),
                )
                db.execute(
                    "INSERT INTO artifact_provenance VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(sha256,provider,source_uri,filename) "
                    "DO UPDATE SET last_seen=excluded.last_seen",
                    (
                        sha,
                        "engineering_inventory",
                        path,
                        path.rsplit("/", 1)[-1],
                        kind,
                        now(),
                        now(),
                    ),
                )
                count += 1
            self.audit(db, "inventory_metadata", {"observations": count, "hydrated": False})
        return {"observations": count, "hydrated": False, "models_created": 0}

    def artifact_candidates(self, unassigned=False):
        with self.archive.db() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT a.*,c.archived,c.artifact_kind FROM fabrication_artifacts a "
                    "LEFT JOIN artifact_custody c ON c.sha256=a.sha256 "
                    + (
                        "WHERE NOT EXISTS(SELECT 1 FROM model_representations r "
                        "WHERE r.sha256=a.sha256 AND r.active=1) "
                        if unassigned
                        else ""
                    )
                    + "ORDER BY a.ingested_at DESC,a.sha256 DESC LIMIT 200"
                )
            ]
            for row in rows:
                row["locations"] = [
                    dict(value)
                    for value in db.execute(
                        "SELECT provider,path,observed_at FROM artifact_locations WHERE sha256=?",
                        (row["sha256"],),
                    )
                ]
                row["metadata"] = json.loads(row["metadata"])
        return rows

    def artifact_metadata(self, sha):
        self.digest(sha)
        with self.archive.db() as db:
            row = db.execute(
                "SELECT a.*,c.archived,c.artifact_kind FROM fabrication_artifacts a "
                "LEFT JOIN artifact_custody c ON c.sha256=a.sha256 WHERE a.sha256=?",
                (sha,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown artifact")
            result = dict(row)
            result["metadata"] = json.loads(result["metadata"])
            result["locations"] = [
                dict(item)
                for item in db.execute(
                    "SELECT provider,path,observed_at FROM artifact_locations WHERE sha256=?",
                    (sha,),
                )
            ]
        return result

    def hydrate(self, sha):
        from pathlib import Path

        from .fabrication_ingestion import ArtifactSubmission, FabricationIngestion

        artifact = self.artifact_metadata(sha)
        aliases = {
            "freecad": "design_freecad",
            "freecad_backup": "design_freecad",
            "stl": "manufacturing_geometry",
            "step": "manufacturing_geometry",
            "obj": "manufacturing_geometry",
            "blender": "design_blender",
        }
        kind = aliases.get(artifact["artifact_kind"], artifact["artifact_kind"])
        for location in artifact["locations"]:
            if location["provider"] != "engineering_inventory":
                continue
            path = Path(location["path"])
            if not path.is_file() or path.is_symlink():
                continue
            submission = ArtifactSubmission(
                path.name,
                "engineering_inventory",
                str(path),
                artifact_kind=kind,
                expected_sha256=sha,
            )
            actual = FabricationIngestion(self.archive).submit_path(path, submission)
            return {"sha256": actual, "archived": True, "source_modified": False}
        raise ValueError("Registered source unavailable; no hydration performed")

    def retire_authority(self, identity):
        with self.archive.db() as db:
            changed = db.execute(
                "UPDATE model_authority SET state='superseded' WHERE id=?", (identity,)
            ).rowcount
            if not changed:
                raise ValueError("Unknown authority declaration")
            self.audit(db, "retire_authority", {"id": identity})
        return {"id": identity, "state": "superseded"}

    def model(self, identity):
        with self.archive.db() as db:
            row = db.execute("SELECT * FROM models WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Unknown model")
            revisions = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM model_revisions WHERE model_id=? ORDER BY created_at,id",
                    (identity,),
                )
            ]
            contexts = [
                item[0]
                for item in db.execute(
                    "SELECT context FROM model_contexts WHERE model_id=?", (identity,)
                )
            ]
        return {
            "model": dict(row),
            "contexts": contexts,
            "revisions": [self.manifest(item["id"]) for item in revisions],
        }


def install(app, read_auth, write_auth, archive):
    import asyncio
    import sqlite3

    from fastapi import Depends, HTTPException, Request

    registry = ModelRegistry(archive)
    preview_slots = asyncio.Semaphore(2)

    @app.get("/api/model-registry/artifacts/{sha}/metadata", dependencies=[Depends(read_auth)])
    def metadata(sha: str):
        try:
            return registry.artifact_metadata(sha)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/model-registry/artifacts/{sha}/hydrate", dependencies=[Depends(write_auth)])
    async def hydrate(sha: str, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024:
                raise HTTPException(413, "Confirmation exceeds limit")
        try:
            if json.loads(raw) != {"confirmation": "ARCHIVE " + sha}:
                raise ValueError("Explicit archival approval required")
            return await asyncio.to_thread(registry.hydrate, sha)
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/model-registry/artifacts/{sha}/geometry", dependencies=[Depends(read_auth)])
    async def geometry(sha: str):
        from .engineering_preview import EngineeringPreview

        try:
            registry.digest(sha)
            async with preview_slots:
                return await asyncio.to_thread(EngineeringPreview(archive).geometry, sha)
        except (OSError, ValueError) as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/model-registry/artifacts/{sha}/thumbnail", dependencies=[Depends(read_auth)])
    async def thumbnail(sha: str):
        from fastapi.responses import Response

        from .engineering_preview import EngineeringPreview

        try:
            registry.digest(sha)
            async with preview_slots:
                data = await asyncio.to_thread(EngineeringPreview(archive).thumbnail, sha)
            return Response(
                data,
                media_type="image/png",
                headers={"Cache-Control": "private, max-age=86400"},
            )
        except (OSError, ValueError) as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/model-registry/artifacts", dependencies=[Depends(read_auth)])
    def artifacts(unassigned: bool = False):
        return {"schema_version": 1, "items": registry.artifact_candidates(unassigned)}

    @app.get("/api/model-registry/models", dependencies=[Depends(read_auth)])
    def models():
        return {"schema_version": 1, "items": registry.library()}

    @app.get("/api/model-registry/models/{identity}", dependencies=[Depends(read_auth)])
    def model(identity: str):
        try:
            return registry.model(identity)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/model-registry/revisions/{identity}/manifest", dependencies=[Depends(read_auth)])
    def manifest(identity: str):
        try:
            return registry.manifest(identity)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/model-registry/actions", dependencies=[Depends(write_auth)])
    async def action(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "Registry declaration exceeds limit")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Expected a declaration object")
            operation = data.pop("operation", None)
            operations = {
                "create_model": registry.create,
                "create_revision": registry.revision,
                "representation": registry.representation,
                "authority": registry.authority,
                "component": registry.component,
                "approve_baseline": registry.baseline,
                "retire_authority": registry.retire_authority,
                "import_manifest": registry.import_manifest,
            }
            if operation not in operations:
                raise ValueError("Unknown registry operation")
            return operations[operation](**data)
        except (ValueError, TypeError, OSError) as error:
            raise HTTPException(422, str(error)) from error
        except sqlite3.Error as error:
            raise HTTPException(
                409, "Declaration references conflict or are unavailable"
            ) from error

    return registry
