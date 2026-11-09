#!/usr/bin/env python3
"""Hardware-free regression checks for the OctoPrint s6 startup initializer."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "integrations/octoprint/90-install-octoprint-plugins"


class OctoprintPluginStartupTests(unittest.TestCase):
    def run_initializer(self, missing: bool = False) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            python = root / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  -m) printf 'pip:%s\\n' \"$*\" >> \"$CALL_LOG\";;\n"
                "  -) printf 'verify:%s\\n' \"$*\" >> \"$CALL_LOG\";;\n"
                "  *) exit 23;;\n"
                "esac\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            log = root / "calls"
            env = os.environ.copy()
            env.update({"PATH": "/usr/bin:/bin", "CALL_LOG": str(log)})
            # Do not modify the system interpreter. Substitute the absolute path
            # only in a temporary copy, retaining all other startup logic.
            body = SCRIPT.read_text(encoding="utf-8")
            self.assertIn("PYTHON=/usr/local/bin/python3", body)
            patched = root / "initializer"
            patched.write_text(
                body.replace(
                    "PYTHON=/usr/local/bin/python3",
                    f"PYTHON={root / ('missing-python' if missing else 'python3')}",
                    1,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/sh", str(patched)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
                timeout=15,
            )
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            return result, calls

    def test_startup_uses_absolute_python_and_offline_pip(self) -> None:
        result, calls = self.run_initializer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pip:-m pip install --no-index", calls)
        self.assertIn("--find-links=/opt/octoprint-plugin-wheels", calls)
        self.assertIn("OctoPrint-Remote_connection==0.1.1", calls)
        self.assertIn("Themeify==1.2.2", calls)
        self.assertIn("verify:-", calls)
        self.assertEqual(calls.count("pip:"), 1)
        self.assertEqual(calls.count("verify:"), 1)

    def test_missing_interpreter_fails_closed_before_install(self) -> None:
        result, calls = self.run_initializer(missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected OctoPrint Python executable missing", result.stderr)
        self.assertEqual(calls, "")

    def test_entry_point_assertion_remains_in_initializer(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('entry_points(group="octoprint.plugin")', body)
        self.assertIn('required = {"remote_connection", "themeify"}', body)
        self.assertIn("raise SystemExit", body)

    def test_plugin_entry_points_are_enforced(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        heredoc = body.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        class EntryPoint:
            def __init__(self, name: str) -> None:
                self.name = name

        for names, should_fail in [
            (["remote_connection", "themeify"], False),
            (["remote_connection"], True),
        ]:
            namespace = {"entry_points": lambda group, names=names: [EntryPoint(n) for n in names]}
            with self.subTest(names=names):
                if should_fail:
                    with self.assertRaises(SystemExit):
                        exec(compile(heredoc, str(SCRIPT), "exec"), namespace)
                else:
                    exec(compile(heredoc, str(SCRIPT), "exec"), namespace)


if __name__ == "__main__":
    unittest.main()
