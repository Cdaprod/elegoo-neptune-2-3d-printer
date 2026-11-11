"""Hardware-free tests of the OctoPrint Application Keys approval flow."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from urllib.error import URLError
from unittest.mock import patch

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "authorize_octoprint.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("neptune_authorize_octoprint", SCRIPT)
assert spec and spec.loader
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class FakeOpener:
    pass


KEY = "abcdef1234567890abcdef1234567890"
BASE = "http://127.0.0.1:5001"


def test_application_key_is_issued_only_after_user_approval():
    calls = []
    windows = []

    def fake_request(_opener, url, *, method="GET", payload=None, key=None):
        calls.append((url, method, payload, key))
        if url.endswith("/probe"):
            return 204, "", {}
        if url.endswith("/plugin/appkeys/request") and method == "POST":
            return (
                201,
                "/plugin/appkeys/request/pending123",
                {
                    "app_token": "pending123",
                    "auth_dialog": "/plugin/appkeys/auth/pending123",
                },
            )
        if url.endswith("/plugin/appkeys/request/pending123"):
            count = len([call for call in calls if call[0] == url])
            return (202, "", {}) if count == 1 else (200, "", {"api_key": KEY})
        if url.endswith("/api/connection"):
            assert key == KEY
            return 200, "", {"current": {"state": "Closed"}}
        raise AssertionError(f"Unexpected OctoPrint request: {url}")

    with patch.object(auth, "request", side_effect=fake_request):
        result = auth.authorize(
            FakeOpener(),
            BASE,
            sleep=lambda _delay: None,
            open_browser=windows.append,
        )

    assert result == KEY
    assert windows == [BASE + "/plugin/appkeys/auth/pending123"]
    assert any(call[2] == {"app": "Neptune Observatory"} for call in calls)


def test_authorization_rejects_denial_and_does_not_invent_key():
    replies = iter(
        [
            (204, "", {}),
            (
                201,
                "/plugin/appkeys/request/abc123",
                {
                    "app_token": "abc123",
                    "auth_dialog": "/plugin/appkeys/auth/abc123",
                },
            ),
            (404, "", {}),
        ]
    )
    with patch.object(auth, "request", side_effect=lambda *args, **kwargs: next(replies)):
        with pytest.raises(RuntimeError, match="denied or expired"):
            auth.authorize(
                FakeOpener(), BASE, sleep=lambda _delay: None, open_browser=lambda _: None
            )


def test_authorization_rejects_cross_origin_urls():
    with pytest.raises(ValueError, match="unexpected application-key URL"):
        auth.safe_plugin_url(BASE, "http://example.invalid/plugin/appkeys/auth/abc123")


def test_existing_key_is_never_rotated_on_validation_failure(tmp_path):
    local_env = tmp_path / ".env"
    local_env.write_text("OCTOPRINT_API_KEY=invalid-key\nOCTOPRINT_HOST_PORT=5001\n")

    def save_test(value):
        local_env.write_text(value)
        local_env.chmod(0o600)

    with (
        patch.object(auth, "ENV", local_env),
        patch.object(auth, "save", side_effect=save_test),
        patch.object(auth, "valid_key", return_value=False),
        patch.object(auth, "authorize", return_value=KEY) as approve,
        patch.object(sys, "argv", ["authorize_octoprint.py"]),
    ):
        assert auth.main() == 1
    assert approve.call_count == 0
    assert auth.parse_env(local_env.read_text())["OCTOPRINT_API_KEY"] == "invalid-key"


def test_check_config_rejects_placeholder(tmp_path):
    local_env = tmp_path / ".env"
    local_env.write_text("OCTOPRINT_API_KEY=replace-with-octoprint-application-api-key\n")
    with (
        patch.object(auth, "ENV", local_env),
        patch.object(sys, "argv", ["authorize_octoprint.py", "--check-config"]),
    ):
        with pytest.raises(SystemExit) as error:
            auth.main()
    assert error.value.code == 2


def test_explicit_reauthorization_replaces_revoked_key(tmp_path):
    local_env = tmp_path / ".env"
    local_env.write_text("OCTOPRINT_API_KEY=old-revoked-key-12345\nOCTOPRINT_HOST_PORT=5001\n")

    def save_test(value):
        local_env.write_text(value)
        local_env.chmod(0o600)

    with (
        patch.object(auth, "ENV", local_env),
        patch.object(auth, "save", side_effect=save_test),
        patch.object(auth, "authorize", return_value=KEY) as approve,
        patch.object(sys, "argv", ["authorize_octoprint.py", "--reauthorize"]),
    ):
        assert auth.main() == 0
    assert approve.call_count == 1
    assert auth.parse_env(local_env.read_text())["OCTOPRINT_API_KEY"] == KEY


def test_saved_key_http_403_never_triggers_approval(tmp_path):
    local_env = tmp_path / ".env"
    local_env.write_text(f"OCTOPRINT_API_KEY={KEY}\nOCTOPRINT_HOST_PORT=5001\n")
    with (
        patch.object(auth, "ENV", local_env),
        patch.object(auth, "request", return_value=(403, "", {})),
        patch.object(auth, "authorize") as approve,
        patch.object(sys, "argv", ["authorize_octoprint.py"]),
    ):
        assert auth.main() == 1
    approve.assert_not_called()
    assert auth.parse_env(local_env.read_text())["OCTOPRINT_API_KEY"] == KEY


def test_wait_for_api_retries_until_octoprint_responds():
    attempts = []
    sleeps = []

    def fake_request(_opener, url, **kwargs):
        assert url == BASE + "/api/version"
        attempts.append(url)
        if len(attempts) < 3:
            raise URLError("connection refused")
        return 200, "", {"api": "0.1"}

    with patch.object(auth, "request", side_effect=fake_request):
        auth.wait_for_api(FakeOpener(), BASE, sleep=sleeps.append, attempts=4)
    assert len(attempts) == 3
    assert sleeps == [1, 1]


def test_wait_for_api_timeout_preserves_key_and_reports_startup_failure():
    with patch.object(auth, "request", side_effect=URLError("connection refused")):
        with pytest.raises(RuntimeError, match="did not become ready"):
            auth.wait_for_api(FakeOpener(), BASE, sleep=lambda _: None, attempts=2)
