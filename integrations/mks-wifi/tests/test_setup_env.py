"""Regression tests for explicit and static MKS WiFi setup addresses."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "setup_env.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("neptune_setup_env", SCRIPT)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_shell_override_skips_discovery(tmp_path):
    (tmp_path / ".env.example").write_text(
        "OCTOPRINT_MKS_WIFI_HOST=printer.local\n"
        "OCTOPRINT_API_KEY=existing_key\n"
        "NEPTUNE_OBSERVATORY_TOKEN=existing_token\n"
    )
    with (
        patch.object(setup, "ROOT", tmp_path),
        patch.object(setup, "ENV", tmp_path / ".env"),
        patch.object(setup, "EXAMPLE", tmp_path / ".env.example"),
        patch.object(setup, "discover", side_effect=AssertionError("discovery must not run")),
        patch.dict(setup.os.environ, {"OCTOPRINT_MKS_WIFI_HOST": "192.168.0.95"}),
        patch.object(sys, "argv", ["setup_env.py"]),
    ):
        assert setup.main() == 0
    assert setup.parse_env((tmp_path / ".env").read_text())[
        "OCTOPRINT_MKS_WIFI_HOST"
    ] == "192.168.0.95"


def test_static_default_skips_discovery(tmp_path):
    (tmp_path / ".env.example").write_text(
        "OCTOPRINT_MKS_WIFI_HOST=printer.local\n"
        "OCTOPRINT_API_KEY=existing_key\n"
        "NEPTUNE_OBSERVATORY_TOKEN=existing_token\n"
    )
    env = dict(setup.os.environ)
    env.pop("OCTOPRINT_MKS_WIFI_HOST", None)
    with (
        patch.object(setup, "ROOT", tmp_path),
        patch.object(setup, "ENV", tmp_path / ".env"),
        patch.object(setup, "EXAMPLE", tmp_path / ".env.example"),
        patch.object(setup, "discover", side_effect=AssertionError("discovery must not run")),
        patch.dict(setup.os.environ, env, clear=True),
        patch.object(sys, "argv", ["setup_env.py"]),
    ):
        assert setup.main() == 0
    assert setup.parse_env((tmp_path / ".env").read_text())[
        "OCTOPRINT_MKS_WIFI_HOST"
    ] == "192.168.0.95"
