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


def test_wireless_http_to_completed_to_canonical_archive(tmp_path, monkeypatch):
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
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_results = list(pool.map(lambda _: send(), range(2)))
        result = first_results[0]
        assert result["status"] == "received_pending_ingestion" and not result["printing"]
        inode = list(server.completed.iterdir())[0].stat().st_ino
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        assert all(item["sha256"] == sha for item in results)
        assert list(server.completed.iterdir())[0].stat().st_ino == inode
        assert len(list(server.completed.iterdir())) == 1
        assert not list(server.partial.iterdir())
        assert list(server.completed.iterdir())[0].stat().st_mode & 0o777 == 0o640
        archive = FabricationArchive(SimpleNamespace(path=str(tmp_path / "ledger.sqlite")))
        adapter = CompletedDirectoryAdapter(archive, server.completed, settle_seconds=0)
        adapter.scan()
        adapter.scan()
        assert archive.verified(sha).read_bytes() == data
        target = list(server.completed.iterdir())[0]
        target.write_bytes(b"corrupt")
        with pytest.raises(HTTPError) as error:
            send()
        assert error.value.code == 409
        assert target.read_bytes() == b"corrupt"
        monkeypatch.setattr(receiver.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        with pytest.raises(HTTPError) as error:
            send()
        assert error.value.code == 507
        assert target.read_bytes() == b"corrupt"

        with archive.db() as db:
            row = db.execute("SELECT * FROM artifact_provenance").fetchone()
            assert row["filename"] == "part.gcode" and row["provider"] == "lilbitdrop"
            assert db.execute("SELECT count(*) FROM artifact_stages").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ssh_sender_bridge_to_real_loopback_receiver(tmp_path):
    """SSH executable is simulated; remote Python/HTTP/files are actual local execution."""
    import shlex
    import subprocess

    sender_path = SOURCE.with_name("send.py")
    sender_spec = importlib.util.spec_from_file_location("lilbitdrop_sender_test", sender_path)
    sender = importlib.util.module_from_spec(sender_spec)
    sender_spec.loader.exec_module(sender)
    private = tmp_path / "receiver-token"
    private.write_text("private-test-token-" + "x" * 32)
    private.chmod(0o600)
    server = receiver.Receiver(("127.0.0.1", 0), tmp_path / "handoff", private.read_text())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = tmp_path / "sender.gcode"
    source.write_bytes(b"G90\nG1 X10 E1\n")

    def ssh_runner(command, **kwargs):
        assert command[:2] == ["ssh", "-T"]
        assert "StrictHostKeyChecking=yes" in command and "BatchMode=yes" in command
        assert private.read_text() not in command[-1]
        return subprocess.run(shlex.split(command[-1]), **kwargs)

    try:
        result = sender.send(
            source, token_file=str(private), port=server.server_port, runner=ssh_runner
        )
        assert result["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert not result["printing"]
        assert len(list(server.completed.iterdir())) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_incomplete_bodies_admission_timeout_and_startup_orphans(tmp_path):
    import socket
    import time

    token = "bounded-receiver-test-" + "x" * 32
    root = tmp_path / "incoming"
    (root / "partial").mkdir(parents=True)
    orphan = root / "partial/receive-old.part"
    orphan.write_bytes(b"unfinished from prior process")
    server = receiver.Receiver(("127.0.0.1", 0), root, token)
    server.read_timeout = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sockets = []
    header = (
        "POST /receive?filename=slow.gcode HTTP/1.0\r\n"
        f"Authorization: Bearer {token}\r\nContent-Length: 10\r\n"
        "Content-Type: application/octet-stream\r\n"
        f"X-Content-SHA256: {'a' * 64}\r\n\r\n"
    ).encode()
    try:
        for _ in range(2):
            client = socket.create_connection(server.server_address, timeout=3)
            sockets.append(client)
            client.sendall(header + b"G")
        deadline = time.monotonic() + 2
        while len(list(server.partial.glob("*.part"))) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        busy = socket.create_connection(server.server_address, timeout=3)
        with busy:
            busy.sendall(header)
            assert b"429" in busy.recv(4096)
        for client in sockets:
            client.shutdown(socket.SHUT_WR)
            assert b"400" in client.recv(4096)
            client.close()
        deadline = time.monotonic() + 2
        while len(list(server.partial.glob("*.part"))) > 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert list(server.partial.glob("*.part")) == [orphan]
        assert not list(server.completed.iterdir())
        server.read_timeout = 0.1
        with socket.create_connection(server.server_address, timeout=3) as client:
            client.sendall(header)
            assert b"400" in client.recv(4096)
        assert not list(server.completed.iterdir())
    finally:
        for client in sockets:
            client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
