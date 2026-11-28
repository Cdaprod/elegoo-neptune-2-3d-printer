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
            self.archive.verified(sha)
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

    def baseline(self, revision_id):
        manifest = self.manifest(revision_id)
        approved = [row for row in manifest["authorities"] if row["state"] == "approved"]
        if not any(row["role"] == "executable_gcode" for row in approved):
            raise ValueError("Approve exact executable G-code authority first")
        for row in approved:
            self.archive.verified(row["sha256"])
        payload = {
            "schema_version": 1,
            "model_revision_id": revision_id,
            "authorities": approved,
            "components": manifest["components"],
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
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM models ORDER BY created_at DESC,id DESC LIMIT 200"
                )
            ]

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
    import sqlite3

    from fastapi import Depends, HTTPException, Request

    registry = ModelRegistry(archive)

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
