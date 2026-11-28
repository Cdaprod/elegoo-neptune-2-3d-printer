"""Non-actuating iOS Share Sheet bridge-to-receiver integration checks."""

import hashlib
import importlib.util
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

import pytest

from mks_wifi_bridge.completed_ingestion import CompletedDirectoryAdapter
from mks_wifi_bridge.fabrication_archive import FabricationArchive

ROOT = Path(__file__).parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load("ios_share_bridge_test", "integrations/lilbitdrop/ios_share.py")
receiver = load("ios_share_receiver_test", "integrations/lilbitdrop/receiver.py")


def test_ios_share_handoff_to_completed(tmp_path):
    token = "test-only-token-" + "x" * 32
    server = receiver.Receiver(("127.0.0.1", 0), tmp_path / "incoming", token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    gateway = bridge.ShareServer(("127.0.0.1", 0), token, server.server_port)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    content = b"G90\nM82\nG1 X20 Y20 E1\n"
    digest = hashlib.sha256(content).hexdigest()

    def send(name="from-iphone.gcode", secret=token, data=content):
        req = Request(
            "http://127.0.0.1:%s/share?filename=%s" % (gateway.server_port, name),
            method="POST",
            data=data,
            headers={
                "Authorization": "Bearer " + secret,
                "Content-Type": "application/octet-stream",
            },
        )
        with urlopen(req, timeout=5) as res:
            return res.status, json.load(res)

    try:
        with pytest.raises(HTTPError) as exc:
            send(secret="wrong")
        assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            send(name="../escape.gcode")
        assert exc.value.code == 422
        status, receipt = send()
        assert status == 201
        assert receipt["sha256"] == digest
        assert receipt["printing"] is False
        published = list((server.completed / "ios-share").iterdir())
        assert len(published) == 1
        assert published[0].read_bytes() == content
        status, receipt = send()
        assert status == 201
        assert len(list((server.completed / "ios-share").iterdir())) == 1
    finally:
        gateway.shutdown()
        server.shutdown()
        gateway.server_close()
        server.server_close()
        gateway_thread.join(timeout=2)
        server_thread.join(timeout=2)


def test_ios_multiformat_to_canonical_archive(tmp_path):
    token = "test-only-token-" + "x" * 32
    server = receiver.Receiver(("127.0.0.1", 0), tmp_path / "incoming", token)
    gateway = bridge.ShareServer(("127.0.0.1", 0), token, server.server_port)
    threads = [
        threading.Thread(target=service.serve_forever, daemon=True)
        for service in (server, gateway)
    ]
    for thread in threads:
        thread.start()
    package = io.BytesIO()
    with ZipFile(package, "w") as document:
        document.writestr("[Content_Types].xml", "<Types/>")
        document.writestr("3D/3dmodel.model", "<model/>")
    samples = {
        "project.3mf": (package.getvalue(), "slicer_project"),
        "mesh.obj": (b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", "manufacturing_geometry"),
    }

    def send(name, content):
        request = Request(
            f"http://127.0.0.1:{gateway.server_port}/share?filename={name}",
            data=content,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        for name, content in (("script.py", b"print(1)"), ("fake.3mf", b"G90")):
            with pytest.raises(HTTPError) as error:
                send(name, content)
            assert error.value.code == 422
        for name, (content, kind) in samples.items():
            receipt = send(name, content)
            assert receipt["artifact_kind"] == kind
            assert receipt["sha256"] == hashlib.sha256(content).hexdigest()
            assert receipt["canonical_archive"] == "pending"
            assert receipt["processing"] == "not_confirmed"
            assert receipt["gallery"] == "not_confirmed"
            assert receipt["printing"] is False
        archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "ledger.sqlite")))
        adapter = CompletedDirectoryAdapter(archive, server.completed, settle_seconds=0)
        adapter.scan()
        adapter.scan()
        with archive.db() as db:
            rows = db.execute("SELECT * FROM artifact_provenance").fetchall()
            assert len(rows) == len(samples)
            for row in rows:
                content, kind = samples[row["filename"]]
                assert row["artifact_kind"] == kind
                assert "ios-share" in row["source_uri"]
                assert archive.verified(row["sha256"]).read_bytes() == content
            assert db.execute("SELECT count(*) FROM artifact_stages").fetchone()[0] == 0
    finally:
        for service in (gateway, server):
            service.shutdown()
            service.server_close()
        for thread in threads:
            thread.join(timeout=2)
