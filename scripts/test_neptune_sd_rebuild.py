"""Hardware-free macOS Neptune SD rebuild regression tests (stdlib unittest)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

script = Path(__file__).with_name("neptune_sd_rebuild.py")
spec = importlib.util.spec_from_file_location("neptune_sd_rebuild", script)
assert spec is not None and spec.loader is not None
sd = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sd
spec.loader.exec_module(sd)

CARD = {
    "WholeDisk": True,
    "DeviceIdentifier": "disk6",
    "Removable": True,
    "RemovableMedia": True,
    "OSInternalMedia": False,
    "BusProtocol": "Secure Digital",
    "Internal": True,  # Actual built-in macOS card reader can report this.
    "TotalSize": 30992760832,
    "MediaName": "SD Card",
    "VirtualOrPhysical": "Physical",
}


class FakeCard:
    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source"
        self.destination = root / "formatted"
        self.source.mkdir()
        self.destination.mkdir()
        self.mount = self.source
        self.name = "NEPTUNE"
        self.erase_failed = False
        self.erase_count = 0
        self.confirm_count = 0
        self.ejected = False

    def mount_reader(self, _info):
        return self.mount, self.name

    def confirm(self, _info, label, backup):
        self.confirm_count += 1
        assert label == "NEPTUNE"
        assert backup.is_dir()

    def command(self, *args):
        if args[:2] == ("diskutil", "eraseDisk"):
            self.erase_count += 1
            if self.erase_failed:
                raise subprocess.CalledProcessError(1, args)
            self.mount = self.destination
            self.name = args[3]
        if args[:2] == ("diskutil", "eject"):
            self.ejected = True
        return subprocess.CompletedProcess(args, 0)

    def rebuild(self):
        return sd.run_workflow(
            CARD, self.root / "backups",
            confirm=self.confirm,
            runner=self.command,
            mount_reader=self.mount_reader,
            check_card=lambda *_: None,
            verify_layout=lambda *_: None,
        )


class TestNeptuneSdRebuild(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.card = FakeCard(self.root)
        (self.card.source / "Very long enclosure.gcode").write_bytes(b"G1 X5\n")
        (self.card.source / "Very long enclosure.gco").write_bytes(b"G1 X6\n")
        (self.card.source / "config.txt").write_bytes(b"config\n")
        (self.card.source / ".DS_Store").write_bytes(b"macOS metadata")
        (self.card.source / "models").mkdir()
        (self.card.source / "models" / "another.gcode").write_bytes(b"G1 Y3\n")

    def pending(self):
        return next((self.root / "backups").glob("neptune-sd-*"))

    def test_sd_reader_internal_true_is_allowed_but_internal_media_is_not(self):
        self.assertTrue(sd.is_sd_card(CARD))
        for altered in (
            {"Removable": False},
            {"RemovableMedia": False},
            {"OSInternalMedia": True},
            {"OSInternalMedia": None},
            {"BusProtocol": "USB"},
            {"WholeDisk": False},
            {"VirtualOrPhysical": "Virtual"},
            {"VirtualOrPhysical": None},
        ):
            self.assertFalse(sd.is_sd_card({**CARD, **altered}))

    def test_complete_rebuild_retains_original_label_and_verifies_contents(self):
        backup = self.card.rebuild()
        manifest = sd.load_manifest(backup)
        self.assertEqual(manifest["stage"], "complete")
        self.assertEqual(manifest["original_label"], "NEPTUNE")
        self.assertEqual(self.card.erase_count, 1)
        self.assertEqual(self.card.confirm_count, 1)
        self.assertTrue(self.card.ejected)
        names = list(manifest["name_map"].values())
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)
        for name in names:
            self.assertLessEqual(len(Path(name).stem), 8)
            self.assertTrue(name.endswith(".GCO"))
        self.assertEqual(sd.inventory(backup / "original"), manifest["source_files"])
        self.assertIn(".DS_Store", manifest["backup_all_files"])
        self.assertEqual(
            sd.inventory(backup / "original", include_metadata=True),
            manifest["backup_all_files"],
        )
        self.assertNotIn(".DS_Store", sd.inventory(self.card.destination))
        self.assertEqual(sd.inventory(self.card.destination), sd.expected_files(manifest))

    def test_backup_metadata_hash_mismatch_is_detected(self):
        self.card.erase_failed = True
        with self.assertRaises(sd.RebuildError):
            self.card.rebuild()
        backup = self.pending()
        manifest = sd.load_manifest(backup)
        self.assertTrue(sd.verified_backup(backup, manifest))
        (backup / "original" / ".DS_Store").write_bytes(b"tampered")
        self.assertFalse(sd.verified_backup(backup, manifest))

    def test_failed_erase_keeps_verified_backup_and_retries_without_recopied_files(self):
        self.card.erase_failed = True
        with self.assertRaisesRegex(sd.RebuildError, "eraseDisk failed"):
            self.card.rebuild()
        backup = self.pending()
        manifest = sd.load_manifest(backup)
        self.assertEqual(manifest["stage"], "backed_up")
        self.assertTrue(sd.verified_backup(backup, manifest))
        self.card.erase_failed = False
        self.assertEqual(self.card.rebuild(), backup)
        self.assertEqual(self.card.erase_count, 2)
        self.assertEqual(len(list((self.root / "backups").glob("neptune-sd-*"))), 1)

    def test_crash_after_erase_before_manifest_update_is_resumable(self):
        self.card.erase_failed = True
        with self.assertRaises(sd.RebuildError):
            self.card.rebuild()
        # Emulate a successful erase before the process could update its manifest.
        self.card.erase_failed = False
        self.card.mount = self.card.destination
        self.card.rebuild()
        self.assertEqual(self.card.erase_count, 1)
        self.assertEqual(sd.load_manifest(self.pending())["stage"], "complete")

    def test_partial_restore_is_resumable_without_second_format(self):
        self.card.erase_failed = True
        with self.assertRaises(sd.RebuildError):
            self.card.rebuild()
        backup = self.pending()
        manifest = sd.load_manifest(backup)
        manifest["stage"] = "restoring"
        sd.save_manifest(backup, manifest)
        self.card.mount = self.card.destination
        name = next(iter(sd.expected_files(manifest)))
        path = self.card.destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial")
        self.card.rebuild()
        self.assertEqual(self.card.erase_count, 1)
        self.assertEqual(sd.inventory(self.card.destination), sd.expected_files(manifest))

    def test_unexpected_files_block_restore(self):
        self.card.erase_failed = True
        with self.assertRaises(sd.RebuildError):
            self.card.rebuild()
        backup = self.pending()
        manifest = sd.load_manifest(backup)
        manifest["stage"] = "formatted"
        sd.save_manifest(backup, manifest)
        self.card.mount = self.card.destination
        (self.card.destination / "unknown.txt").write_text("other user's data")
        with self.assertRaisesRegex(sd.RebuildError, "Unexpected data"):
            self.card.rebuild()

    def test_unrepresentable_volume_name_never_erases(self):
        self.card.name = "original label too long"
        with self.assertRaisesRegex(sd.RebuildError, "cannot be preserved"):
            self.card.rebuild()
        self.assertEqual(self.card.erase_count, 0)

    def test_failed_eject_keeps_verified_manifest(self):
        original_command = self.card.command

        def fail_eject(*args):
            if args[:2] == ("diskutil", "eject"):
                raise subprocess.CalledProcessError(1, args)
            return original_command(*args)

        self.card.command = fail_eject
        with self.assertRaisesRegex(sd.RebuildError, "eject failed"):
            self.card.rebuild()
        self.assertEqual(sd.load_manifest(self.pending())["stage"], "verified")


if __name__ == "__main__":
    unittest.main()
