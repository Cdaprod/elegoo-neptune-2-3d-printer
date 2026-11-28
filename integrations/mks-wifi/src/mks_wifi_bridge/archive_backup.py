"""Consistent SQLite + immutable-original backup, with fail-closed restoration."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path


def archived_hashes(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_custody'").fetchone():
        return {
            row[0]
            for row in db.execute(
                "SELECT a.sha256 FROM fabrication_artifacts a LEFT JOIN artifact_custody c "
                "ON c.sha256=a.sha256 WHERE c.sha256 IS NULL OR c.archived=1"
            )
        }
    return {row[0] for row in db.execute("SELECT sha256 FROM fabrication_artifacts")}


def backup(database, output):
    database, output = Path(database), Path(output)
    if not database.is_file() or output.exists():
        raise ValueError("Existing database and new backup destination required")
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        root = Path(temporary)
        snapshot = root / "ledger.sqlite"
        with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        with sqlite3.connect(snapshot) as db:
            hashes = sorted(archived_hashes(db))
        manifest = {
            "schema_version": 2,
            "originals": hashes,
            "storage_names": {
                sha: sha
                + (
                    ".gcode"
                    if (database.parent / "fabrication-archive" / (sha + ".gcode")).exists()
                    else ".blob"
                )
                for sha in hashes
            },
            "database_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        }
        (root / "manifest.json").write_text(json.dumps(manifest))
        packed = root / "archive.tar.gz"
        with tarfile.open(packed, "w:gz") as tar:
            tar.add(snapshot, arcname="ledger.sqlite")
            tar.add(root / "manifest.json", arcname="manifest.json")
            for sha in hashes:
                if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                    raise ValueError("Invalid canonical identity in database")
                path = database.parent / "fabrication-archive" / manifest["storage_names"][sha]
                if path.is_symlink():
                    raise ValueError("Canonical originals cannot be symbolic links")
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                        raise ValueError("Original artifact failed integrity verification")
                tar.add(
                    path, arcname="originals/" + manifest["storage_names"][sha], recursive=False
                )
        packed.chmod(0o600)
        with packed.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(packed, output)  # No overwrite of existing backups.
    return manifest


def restore(backup_file, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Restore destination must not exist; never overwrite production")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        root = Path(temporary)
        staged = root / "restored"
        originals = staged / "fabrication-archive"
        originals.mkdir(parents=True)
        with tarfile.open(backup_file, "r:gz") as tar:
            manifest_member = tar.getmember("manifest.json")
            if not manifest_member.isfile() or manifest_member.size > 16 * 1024 * 1024:
                raise ValueError("Invalid or oversized backup manifest")
            manifest_file = tar.extractfile(manifest_member)
            if manifest_file is None:
                raise ValueError("Missing backup manifest")
            manifest = json.loads(manifest_file.read(16 * 1024 * 1024))
            if manifest.get("schema_version") not in {1, 2}:
                raise ValueError("Unsupported backup schema")
            hashes = manifest["originals"]
            if len(hashes) != len(set(hashes)):
                raise ValueError("Duplicate original identities")
            expected = {"manifest.json", "ledger.sqlite"}
            for sha in hashes:
                if (
                    not isinstance(sha, str)
                    or len(sha) != 64
                    or any(c not in "0123456789abcdef" for c in sha)
                ):
                    raise ValueError("Invalid original identity")
                name = manifest.get("storage_names", {}).get(sha, sha + ".gcode")
                if name not in {sha + ".gcode", sha + ".blob"}:
                    raise ValueError("Invalid canonical storage name")
                expected.add("originals/" + name)
            members = tar.getmembers()
            if {m.name for m in members} != expected or len(members) != len(expected):
                raise ValueError("Unexpected or duplicate backup members")
            required = sum(member.size for member in members)
            if required + 256 * 1024 * 1024 > shutil.disk_usage(destination.parent).free:
                raise ValueError("Insufficient restore storage reserve")
            for member in members:
                if member.name.startswith("originals/") and member.size > 50 * 1024 * 1024:
                    raise ValueError("Original exceeds supported size")
                if not member.isfile():
                    raise ValueError("Only regular backup members accepted")
                if member.name == "manifest.json":
                    continue
                target = (
                    staged / "ledger.sqlite"
                    if member.name == "ledger.sqlite"
                    else originals / Path(member.name).name
                )
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("Missing backup member")
                with target.open("xb") as stream:
                    shutil.copyfileobj(source, stream, 65536)
                    stream.flush()
                    os.fsync(stream.fileno())
                target.chmod(0o600)
            for sha in hashes:
                with (originals / manifest.get("storage_names", {}).get(sha, sha + ".gcode")).open(
                    "rb"
                ) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                        raise ValueError("Restored artifact checksum mismatch")
            ledger = staged / "ledger.sqlite"
            with ledger.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["database_sha256"]:
                    raise ValueError("Restored database checksum mismatch")
            with sqlite3.connect(ledger) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Restored database integrity failed")
                actual = archived_hashes(db)
                if actual != set(hashes):
                    raise ValueError("Restored catalog does not match originals")
        destination.mkdir(mode=0o700)  # Reserve a new destination; fail if another restore won.
        os.rename(staged, destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore"])
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    try:
        result = (backup if args.operation == "backup" else restore)(args.source, args.destination)
        print(
            json.dumps(
                {"schema_version": result["schema_version"], "originals": len(result["originals"])}
            )
        )
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        parser.exit(1, f"Archive operation failed: {error}\n")


if __name__ == "__main__":
    main()
