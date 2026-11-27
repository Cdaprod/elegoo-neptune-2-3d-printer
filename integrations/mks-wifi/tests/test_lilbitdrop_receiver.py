"""Actual local HTTP transfer to actual canonical ingestion; no printer or BLE."""

import hashlib
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mks_wifi_bridge.completed_ingestion import CompletedDirectoryAdapter
from mks_wifi_bridge.fabrication_archive import FabricationArchive

SOURCE = Path(__file__).parents[3] / "integrations/lilbitdrop/receiver.py"
spec = importlib.util.spec_from_file_location("lilbitdrop_receiver_test", SOURCE)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


def test_wireless_http_to_completed_to_canonical_archive(tmp_path):
    token = "local-test-only-" + "x" * 32
    server = receiver.Receiver(("127.0.0.1", 0), tmp_path / "incoming", token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = b"G90\nM82\nG1 X0 Y0 Z0.2\nG1 X20 E1\n"
    sha = hashlib.sha256(data).hexdigest()
    base = f"http://127.0.0.1:{server.server_port}"

    def send(name="part.gcode", content=data, digest=sha, authorization=token):
        request = Request(
            base + "/receive?filename=" + name,
            data=content,
            headers={
                "Authorization": "Bearer " + authorization,
                "Content-Type": "application/octet-stream",
                "X-Content-SHA256": digest,
            },
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        for bad, code in [
            ({"authorization": "invalid"}, 401),
            ({"name": "../bad.gcode"}, 422),
            ({"digest": "0" * 64}, 422),
        ]:
            with pytest.raises(HTTPError) as error:
                send(**bad)
            assert error.value.code == code
        assert not list(server.completed.iterdir())
        result = send()
        assert result["status"] == "received_pending_ingestion" and not result["printing"]
        send()
        assert len(list(server.completed.iterdir())) == 1
        assert not list(server.partial.iterdir())
        assert list(server.completed.iterdir())[0].stat().st_mode & 0o777 == 0o640
        archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "ledger.sqlite")))
        adapter = CompletedDirectoryAdapter(archive, server.completed, settle_seconds=0)
        adapter.scan()
        adapter.scan()
        assert archive.verified(sha).read_bytes() == data
        with archive.db() as db:
            row = db.execute("SELECT * FROM artifact_provenance").fetchone()
            assert row["filename"] == "part.gcode" and row["provider"] == "lilbitdrop"
            assert db.execute("SELECT count(*) FROM artifact_stages").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
