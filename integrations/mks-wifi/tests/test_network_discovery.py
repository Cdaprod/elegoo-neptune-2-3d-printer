"""Unit tests for safe network discovery; never contact a real printer."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "discover_mks.py"
spec = importlib.util.spec_from_file_location("discover_mks", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_discover_unique_mks_from_local_candidates():
    with patch.object(module, "neighbor_ips", return_value={"192.168.1.2"}), patch.object(
        module, "local_subnets", return_value={"192.168.1.3"}
    ), patch.object(module, "mks_http_identity", side_effect=lambda ip: ip == "192.168.1.3"):
        assert module.discover() == "192.168.1.3"


def test_discover_refuses_ambiguous_printers():
    with (
        patch.object(module, "neighbor_ips", return_value={"192.168.1.2", "192.168.1.3"}),
        patch.object(module, "local_subnets", return_value=set()),
        patch.object(module, "mks_http_identity", return_value=True),
    ):
        try:
            module.discover()
        except RuntimeError as exc:
            assert "found 2" in str(exc)
        else:
            raise AssertionError("ambiguous printer selection must fail")


def test_http_fingerprint_does_not_contact_raw_tcp():
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, count):
            return b"<html>update_sketch update_spiffs update_cfg</html>"

    with patch.object(module.urllib.request, "urlopen", return_value=Response()) as urlopen:
        assert module.mks_http_identity("192.168.1.3")
        assert urlopen.call_args.args[0].full_url == "http://192.168.1.3/"
