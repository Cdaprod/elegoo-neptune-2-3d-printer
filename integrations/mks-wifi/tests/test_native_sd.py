"""Simulated native transfer-to-real-geometry validation. Not hardware acceptance."""

import asyncio
import hashlib
import json

import pytest
from robin_sd_simulator import Firmware

from mks_wifi_bridge.native_sd import NativeSdStorage
from mks_wifi_bridge.robin_sd_protocol import CHUNK, ProtocolReader, command, parse_reply
from mks_wifi_bridge.robin_sd_read import SdReadError

GCODE = b"; fixture\nG90\nM83\nG1 X0 Y0 Z0.2\nG1 X10 Y0 E1\nG1 X10 Y10 E1\n"


@pytest.mark.parametrize(
    "name", ["../A.GCO", "A/B.GCO", "A.GCO\nM24", "longfilename.gcode", "", "A.BIN", "A\x00.GCO"]
)
def test_wire_rejects_paths_and_injection(name):
    with pytest.raises(SdReadError):
        command("OPEN", sequence=1, filename=name)


def test_golden_commands_and_packets():
    assert (
        command("OPEN", sequence=2, filename="PART.GCO")
        == "M794 V1 O3 Q2 S0 A0 B0 P080065082084046071067079"
    )
    frame = 'RSD1 {"v":1,"op":"CAPABILITIES","q":1,"s":0,"chunk":192,"max_bytes":52428800}'
    assert parse_reply(frame, operation="CAPABILITIES", sequence=1)["chunk"] == CHUNK
    for bad in (
        frame[:-1],
        frame.replace('"v":1', '"v":2'),
        frame.replace('"q":1', '"q":2'),
        frame.replace('"v":1', '"v":1,"v":1'),
        "RSD1 " + "x" * 769,
    ):
        with pytest.raises(SdReadError):
            parse_reply(bad, operation="CAPABILITIES", sequence=1)


def test_complete_transfer_dedup_geometry_and_restart(tmp_path):
    async def run():
        firmware = Firmware({"PART.GCO": GCODE, "COPY.GCO": GCODE})
        storage = NativeSdStorage(tmp_path, firmware)
        entries = await storage.listing()
        assert len(entries) == 2 and not entries[0]["geometry_available"]
        for name in ("PART.GCO", "COPY.GCO"):
            job = storage.start(name)
            await storage.tasks[job["id"]]
            assert job["status"] == "complete", job
            assert job["asset"]["geometry_available"]
            assert job["sha256"] == hashlib.sha256(GCODE).hexdigest()
            assert storage.cached(job["sha256"]).read_bytes() == GCODE
        assert len(list(storage.blobs.glob("*.gcode"))) == 1
        assert len(storage.assets) == 2
        assert not firmware.sessions
        assert all(wire.startswith("M794 ") for wire in firmware.history)
        assert not list(tmp_path.glob("*.part"))
        reloaded = NativeSdStorage(tmp_path, firmware)
        assert len(reloaded.assets) == 2
        geometry = reloaded.previews.geometry(job["sha256"] + ".gcode")
        assert geometry["stats"]["extrusion_segments"] == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["crc", "removed", "changed", "printing", "disconnect", "truncated", "sha256"]
)
def test_failed_transfer_never_publishes_bytes(tmp_path, failure):
    async def run():
        firmware = Firmware({"PART.GCO": GCODE * 30})
        original = firmware.exchange

        async def exchange(request):
            if request["operation"] == "READ":
                if failure == "crc":
                    firmware.corrupt = True
                if failure == "removed":
                    firmware.mounted = False
                if failure == "changed":
                    firmware.epoch += 1
                if failure == "printing":
                    firmware.state = "printing"
                if failure == "disconnect":
                    raise OSError("disconnected")
                if failure == "truncated":
                    return 'RSD1 {"v":'
            reply = await original(request)
            if failure == "sha256" and request["operation"] == "CLOSE":
                frame = json.loads(reply[5:])
                frame["sha256"] = "0" * 64
                return "RSD1 " + json.dumps(frame)
            return reply

        firmware.exchange = exchange
        storage = NativeSdStorage(tmp_path, firmware)
        job = storage.start("PART.GCO")
        await storage.tasks[job["id"]]
        assert job["status"] == "failed", job
        assert not storage.assets and not list(storage.blobs.iterdir())
        assert not list(tmp_path.glob("*.part"))

    asyncio.run(run())


def test_cancel_recovery_and_cache_corruption(tmp_path):
    async def run():
        firmware = Firmware({"PART.GCO": GCODE * 100})
        original = firmware.exchange
        waiting = asyncio.Event()

        async def exchange(request):
            if request["operation"] == "READ":
                waiting.set()
                await asyncio.sleep(10)
            return await original(request)

        firmware.exchange = exchange
        storage = NativeSdStorage(tmp_path, firmware)
        job = storage.start("PART.GCO")
        await waiting.wait()
        storage.tasks[job["id"]].cancel()
        await storage.tasks[job["id"]]
        assert job["status"] == "cancelled" and not firmware.sessions
        firmware.exchange = original
        job = storage.start("PART.GCO")
        await storage.tasks[job["id"]]
        assert job["status"] == "complete"
        storage.cached(job["sha256"]).write_bytes(b"tampered")
        with pytest.raises(SdReadError, match="SHA-256"):
            storage.cached(job["sha256"])

    asyncio.run(run())


def test_sequence_session_and_offset_guards():
    async def run():
        firmware = Firmware({"PART.GCO": GCODE})
        reader = ProtocolReader(firmware.exchange)
        await reader.capabilities()
        size, token = await reader.open("PART.GCO")
        for offset, snapshot in ((1, token), (0, "bad")):
            with pytest.raises(SdReadError):
                await reader.read("PART.GCO", offset, size, snapshot)
        frame = json.loads(
            (
                await firmware.exchange(
                    {
                        "operation": "READ",
                        "sequence": 1,
                        "session": reader.session,
                        "offset": 0,
                        "count": 1,
                    }
                )
            )[5:]
        )
        assert frame["error"] == "SEQUENCE"
        await reader.close(cancel=True)

    asyncio.run(run())


def test_authenticated_api_transfer_to_geometry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from mks_wifi_bridge import native_sd_api

    firmware = Firmware({"PART.GCO": GCODE})
    monkeypatch.setenv("NEPTUNE_NATIVE_SD_READ_ENABLED", "true")
    monkeypatch.setenv("NEPTUNE_NATIVE_SD_CACHE", str(tmp_path))
    monkeypatch.setattr(native_sd_api, "OctoPrintSdTransport", lambda backend: firmware)
    app = FastAPI()

    async def auth(request: Request):
        if request.headers.get("authorization") != "Bearer fixture":
            raise HTTPException(401)

    async def write_auth(request: Request):
        await auth(request)
        if request.headers.get("origin") != "http://testserver":
            raise HTTPException(403)

    native_sd_api.install(app, auth, write_auth, SimpleNamespace(backend=object()))
    with TestClient(app) as client:
        assert client.get("/api/native-sd").status_code == 401
        client.headers.update({"authorization": "Bearer fixture"})
        assert (
            client.post("/api/native-sd/transfers", json={"filename": "PART.GCO"}).status_code
            == 403
        )
        client.headers.update({"origin": "http://testserver"})
        assert client.get("/api/native-sd").json()["entries"][0]["name"] == "PART.GCO"
        assert (
            client.post("/api/native-sd/transfers", json={"filename": "../A.GCO"}).status_code
            == 409
        )
        job = client.post("/api/native-sd/transfers", json={"filename": "PART.GCO"}).json()
        import time

        for _ in range(200):
            progress = client.get("/api/native-sd/transfers/" + job["id"]).json()
            if progress["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        assert progress["status"] == "complete", progress
        root = "/api/native-sd/assets/" + progress["sha256"]
        assert client.get(root + "/gcode").content == GCODE
        assert client.get(root + "/geometry").json()["stats"]["extrusion_segments"] == 2
        assert client.get(root + "/thumbnail").content.startswith(b"<svg")
        assert client.get("/api/native-sd/assets/not-a-hash/gcode").status_code == 422
        assert client.delete("/api/native-sd/transfers/missing").status_code == 404


def test_disabled_bridge_does_not_issue_commands(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mks_wifi_bridge.native_sd_api import install

    monkeypatch.setenv("NEPTUNE_NATIVE_SD_READ_ENABLED", "false")
    app = FastAPI()

    async def auth():
        pass

    install(app, auth, auth, SimpleNamespace(backend=None))
    with TestClient(app) as client:
        assert client.get("/api/native-sd").json()["status"] == "unavailable"
        assert (
            client.post("/api/native-sd/transfers", json={"filename": "PART.GCO"}).status_code
            == 423
        )


def test_octoprint_plugin_uses_owner_and_rejects_unsafe_states(monkeypatch):
    import importlib.util
    import sys
    import types
    from pathlib import Path

    plugin_module = types.ModuleType("octoprint.plugin")
    for name in ("SimpleApiPlugin", "SettingsPlugin", "EventHandlerPlugin"):
        setattr(plugin_module, name, type(name, (), {}))
    octoprint = types.ModuleType("octoprint")
    octoprint.plugin = plugin_module
    permissions = types.ModuleType("octoprint.access.permissions")
    allowed = {"monitor": True, "control": True}
    permissions.Permissions = types.SimpleNamespace(
        MONITOR=types.SimpleNamespace(can=lambda: allowed["monitor"]),
        CONTROL=types.SimpleNamespace(can=lambda: allowed["control"]),
    )
    flask = types.ModuleType("flask")
    flask.jsonify = lambda body: body

    def abort(status, *args):
        raise ValueError(status)

    flask.abort = abort
    for name, module in {
        "octoprint": octoprint,
        "octoprint.plugin": plugin_module,
        "octoprint.access": types.ModuleType("octoprint.access"),
        "octoprint.access.permissions": permissions,
        "flask": flask,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).parents[2] / "octoprint/neptune_sd_read.py"
    spec = importlib.util.spec_from_file_location("_native_sd_plugin_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__plugin_load__()
    plugin = module.__plugin_implementation__
    configured = {"enabled": False, "state": "OPERATIONAL", "port": "/dev/ttyUSB0"}
    plugin._settings = types.SimpleNamespace(get_boolean=lambda key: configured["enabled"])
    sent = []

    def commands(lines, **kwargs):
        sent.extend(lines)
        plugin.received(
            None, 'RSD1 {"v":1,"op":"CAPABILITIES","q":1,"s":0,"chunk":192,"max_bytes":52428800}'
        )

    plugin._printer = types.SimpleNamespace(
        get_current_connection=lambda: ("Operational", configured["port"], 115200, {}),
        get_state_id=lambda: configured["state"],
        commands=commands,
    )
    request = {"operation": "CAPABILITIES", "sequence": 1, "session": 0}
    with pytest.raises(ValueError, match="423"):
        plugin.on_api_command("exchange", request)
    configured["enabled"] = True
    plugin.capability(None, "NEPTUNE_SD_READ", True)
    assert plugin.on_api_get(None)["can_read"]
    assert plugin.on_api_command("exchange", request)["reply"].startswith("RSD1 ")
    assert sent == ["M794 V1 O0 Q1 S0 A0 B0"]
    for state in ("PRINTING", "PAUSED", "CANCELLING", "UNKNOWN"):
        configured["state"] = state
        with pytest.raises(ValueError, match="423"):
            plugin.on_api_command("exchange", request)
    configured["state"] = "OPERATIONAL"
    configured["port"] = "REMOTE:socket://printer:8080"
    with pytest.raises(ValueError, match="423"):
        plugin.on_api_command("exchange", request)
    configured["port"] = "/dev/ttyUSB0"
    for fields in (
        {"operation": "M24"},
        {"sequence": True},
        {"raw": "M24"},
        {"operation": "OPEN", "filename": "../A.GCO"},
    ):
        with pytest.raises(ValueError, match="400"):
            plugin.on_api_command("exchange", {**request, **fields})
    allowed["control"] = False
    with pytest.raises(ValueError, match="403"):
        plugin.on_api_command("exchange", request)
    plugin.on_event("Disconnected", {})
    assert not plugin.on_api_get(None)["can_read"]
    assert plugin.received(None, "ok T:24") == "ok T:24"
    assert len(sent) == 1


def test_same_filename_different_content_and_bounded_eviction(tmp_path):
    from mks_wifi_bridge.robin_sd_read import MAX_SD_BYTES

    async def run():
        firmware = Firmware({"PART.GCO": GCODE})
        storage = NativeSdStorage(tmp_path, firmware, quota=MAX_SD_BYTES)
        for content in (GCODE, GCODE + b"G1 X20 Y20 E2\n"):
            firmware.files["PART.GCO"] = content
            job = storage.start("PART.GCO")
            await storage.tasks[job["id"]]
            assert job["status"] == "complete"
        assert len(storage.assets) == 2
        assert len({asset["sha256"] for asset in storage.assets.values()}) == 2
        for n in range(2):
            with (storage.blobs / ("large" + str(n) + ".gcode")).open("wb") as output:
                output.truncate(MAX_SD_BYTES // 2)
        storage.prune(1024)
        assert sum(path.stat().st_size for path in storage.blobs.iterdir()) + 1024 <= MAX_SD_BYTES

    asyncio.run(run())


def test_unsupported_firmware_never_receives_probe(tmp_path):
    async def run():
        firmware = Firmware({"PART.GCO": GCODE}, state="unknown")
        storage = NativeSdStorage(tmp_path, firmware)
        job = storage.start("PART.GCO")
        await storage.tasks[job["id"]]
        assert job["status"] == "failed"
        assert firmware.history == []

    asyncio.run(run())
