from __future__ import annotations

from urllib.error import HTTPError

import pytest

from mks_wifi_bridge import mks_upload
from mks_wifi_bridge.mks_upload import UploadError, upload_to_mks, validate_upload


def test_upload_builds_raw_post_with_filename_and_no_multipart(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"err":0}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(mks_upload, "urlopen", fake_urlopen)
    result = upload_to_mks("192.0.2.10", "test-file.gcode", b"G1 X1\n")
    request = captured["request"]
    assert request.full_url.endswith("/upload?X-Filename=test-file.gcode")
    assert request.data == b"G1 X1\n"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/octet-stream"
    assert captured["timeout"] == 30.0
    assert result["http_status"] == 200


@pytest.mark.parametrize("filename", ["../test.gcode", "/tmp/test.gcode", "name.txt", "a;rm.gcode"])
def test_upload_rejects_unsafe_filename(filename) -> None:
    with pytest.raises(ValueError):
        validate_upload(filename, b"G1 X1")


def test_upload_rejects_empty_and_large_content() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_upload("test.gcode", b"")
    with pytest.raises(ValueError, match="50 MB"):
        validate_upload("test.gcode", b"x" * (mks_upload.MAX_UPLOAD_BYTES + 1))


def test_upload_returns_firmware_error_without_retry(monkeypatch) -> None:
    def fake_urlopen(_request, timeout):
        raise HTTPError("http://printer/upload", 409, "busy", {}, None)

    monkeypatch.setattr(mks_upload, "urlopen", fake_urlopen)
    with pytest.raises(UploadError, match="HTTP 409"):
        upload_to_mks("192.0.2.10", "test.gcode", b"G1 X1")
