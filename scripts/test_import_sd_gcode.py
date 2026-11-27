#!/usr/bin/env python3
"""Offline, deterministic tests for physical SD -> OctoPrint local import."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_sd_gcode as sd


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status


class FakeOctoPrint:
    def __init__(self, files=None):
        self.files = list(files or [])
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append(request)
        assert request.full_url == "http://127.0.0.1:5001/api/files/local"
        assert request.get_header("X-api-key") == "fake-test-key"
        if request.get_method() == "GET":
            return Response(json.dumps({"files": self.files}).encode())
        assert request.get_method() == "POST"
        body = request.data
        assert b'name="select"\r\n\r\nfalse\r\n' in body
        assert b'name="print"\r\n\r\nfalse\r\n' in body
        assert b'name="file"; filename="' in body
        assert b'Content-Disposition: form-data; name="command"' not in body
        return Response(b'{"done":true}', status=201)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mount = mock.patch.object(sd, "verify_mount", return_value=self.root)
        self.mount.start()
        self.addCleanup(self.mount.stop)
        self.env = {"OCTOPRINT_API_KEY": "fake-test-key", "OCTOPRINT_HOST_PORT": "5001"}

    def test_dry_run_requires_no_keys_or_backend(self):
        (self.root / "TEST.GCO").write_text("G1 X10 E2\n")
        with mock.patch.object(sd, "ENV", self.root / "nonexistent.env"):
            summary = sd.import_card(self.root, apply=False)
        self.assertEqual(summary, {"discovered": 1, "imported": 0, "skipped": 0, "dry_run": True})

    def test_reads_existing_env_file_as_text_and_uses_pi_api_port(self):
        (self.root / "ENVTEST.GCO").write_text("G1 X14 E1\n")
        env_file = self.root / ".env"
        env_file.write_text(
            "OCTOPRINT_API_KEY=fake-test-key\nOCTOPRINT_HOST_PORT=5001\n",
            encoding="utf-8",
        )
        server = FakeOctoPrint()
        with mock.patch.object(sd, "ENV", env_file):
            result = sd.import_card(self.root, apply=True, opener=server)
        self.assertEqual(result["imported"], 1)
        self.assertEqual([r.get_method() for r in server.requests], ["GET", "POST"])

    def test_real_octoprint_upload_disables_selection_and_print(self):
        (self.root / "BENCH.GCO").write_text("G1 X12 E1\n")
        server = FakeOctoPrint()
        result = sd.import_card(self.root, apply=True, opener=server, env=self.env)
        self.assertEqual(result["imported"], 1)
        self.assertEqual([r.get_method() for r in server.requests], ["GET", "POST"])
        self.assertIn(b'filename="BENCH.GCO"', server.requests[-1].data)

    def test_no_overwrite_for_same_name_different_contents(self):
        (self.root / "CASE.GCO").write_text("G1 X12 E1\n")
        existing = [{"name": "CASE.GCO", "hash": "0" * 40}]
        server = FakeOctoPrint(files=existing)
        result = sd.import_card(self.root, apply=True, opener=server, env=self.env)
        self.assertEqual(result["imported"], 1)
        self.assertNotIn(b'filename="CASE.GCO"', server.requests[-1].data)
        self.assertIn(b'filename="CASE-', server.requests[-1].data)

    def test_skip_identical_file_when_backend_hash_matches(self):
        data = b"G1 X12 E1\n"
        import hashlib
        (self.root / "CASE.GCO").write_bytes(data)
        server = FakeOctoPrint(files=[{"name": "CASE.GCO", "hash": hashlib.sha1(data).hexdigest()}])
        result = sd.import_card(self.root, apply=True, opener=server, env=self.env)
        self.assertEqual((result["imported"], result["skipped"]), (0, 1))
        self.assertEqual(len(server.requests), 1)

    def test_nested_names_and_symlinks_do_not_collide(self):
        for directory in ("left", "right"):
            (self.root / directory).mkdir()
            (self.root / directory / "test.gco").write_text("G1 X2 E1\n")
        (self.root / "link.gco").symlink_to(self.root / "left" / "test.gco")
        (self.root / "jump").symlink_to(self.root / "right", target_is_directory=True)
        files = list(sd.candidates(self.root))
        self.assertEqual(len(files), 2)
        names = [sd.filename_for(p, self.root, sd.read_source(p, self.root), {})[0] for p in files]
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all(n.endswith(".gco") for n in names))

    def test_oversized_file_is_skipped(self):
        with mock.patch.object(sd, "MAX_BYTES", 10):
            (self.root / "big.gco").write_bytes(b"G1 X0 E1\n" * 10)
            server = FakeOctoPrint()
            result = sd.import_card(self.root, apply=True, opener=server, env=self.env)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(server.requests), 1)

    def test_usb_mount_detection_propagates_removability(self):
        payload = {'blockdevices': [{'name':'sda','type':'disk','tran':'usb','rm':0,'children':[{'name':'sda1','type':'part','mountpoints':[str(self.root)]}]}, {'name':'mmcblk0','type':'disk','rm':0,'children':[{'type':'part','mountpoints':['/boot/firmware']}]}]}
        fake = mock.Mock(stdout=json.dumps(payload))
        with mock.patch.object(sd.sys, 'platform', 'linux'), mock.patch.object(sd.subprocess, 'run', return_value=fake), mock.patch.object(sd.os.path, 'ismount', return_value=True):
            # Candidate must be under a removable-media mount prefix; temp directories do not qualify.
            self.assertEqual(sd.detect_mounts(), [])

    def test_auto_detect_usb_card_without_touching_os_volume(self):
        payload = {'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'tran': 'usb', 'rm': 0,
             'children': [{'name':'sda1','type':'part','mountpoints':['/media/cdaprod/NEPTUNE']}]},
            {'name': 'mmcblk0', 'type': 'disk', 'rm': 0,
             'children': [{'name':'mmcblk0p1','type':'part','mountpoints':['/boot/firmware']}]},
        ]}
        fake = mock.Mock(stdout=json.dumps(payload))
        with mock.patch.object(sd.sys,'platform','linux'), mock.patch.object(sd.subprocess,'run',return_value=fake), mock.patch.object(sd.Path,'is_dir',return_value=True), mock.patch.object(sd.Path,'is_symlink',return_value=False), mock.patch.object(sd.os.path,'ismount',return_value=True):
            self.assertEqual(sd.detect_mounts(), [Path('/media/cdaprod/NEPTUNE')])

    def test_mount_is_required_even_in_dry_run(self):
        self.mount.stop()
        with self.assertRaises(sd.ImportFailure):
            sd.verify_mount(self.root)


if __name__ == "__main__":
    unittest.main()
