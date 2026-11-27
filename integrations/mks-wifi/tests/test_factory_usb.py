"""Factory evidence replay and explicitly simulated interleaving; no hardware I/O."""

import ast
import json
import re
import time
from pathlib import Path

import pytest

from mks_wifi_bridge.factory_usb import catalog_visible

SOURCE = Path(__file__).parents[2] / "octoprint/neptune_sd_read.py"
TREE = ast.parse(SOURCE.read_text())
NAMES = {"FACTORY_COMMANDS", "FACTORY_NAME", "catalog_name", "FactoryTransaction"}
NODES = [
    node
    for node in TREE.body
    if (isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in NAMES)
    or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in NAMES for target in node.targets)
    )
]
SCOPE = {"re": re, "time": time}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), SCOPE)
FactoryTransaction = SCOPE["FactoryTransaction"]
EVIDENCE = json.loads((Path(__file__).parent / "fixtures/robin_factory_usb.json").read_text())


@pytest.mark.parametrize(
    "operation",
    ["firmware", "configuration", "coordinates", "list", "list_long", "select", "status"],
)
def test_unsolicited_temperature_and_arbitrary_ok_do_not_complete_query(operation):
    transaction = FactoryTransaction(operation, "DISPMNT01.gcode")
    transaction.sent = True
    for line in ["ok", *EVIDENCE["unsolicited"], "ok"]:
        transaction.feed(line)
        assert not transaction.poll()


def test_actual_factory_firmware_selection_and_zero_cursor_fixtures():
    for operation, key in [
        ("firmware", "firmware_response"),
        ("select", "selection_response"),
        ("status", "status_response"),
    ]:
        transaction = FactoryTransaction(operation, "DISPMNT01.gcode")
        transaction.sent = True
        for line in EVIDENCE[key]:
            transaction.feed(EVIDENCE["unsolicited"][1])
            transaction.feed(line)
        assert transaction.done
        if operation == "firmware":
            assert transaction.data["firmware"] == "Robin_nano_v2.1.6.b"
            assert transaction.data["capabilities"]["EMERGENCY_PARSER"] is False
        elif operation == "select":
            assert transaction.data["size_bytes"] == 2113357
            assert transaction.data["selected"] and transaction.data["printing"] is None
        else:
            assert (
                transaction.data["byte_position"] == 0
                and transaction.data["total_bytes"] == 2113357
            )
            assert transaction.data["printing"] is None


def test_only_own_sent_transaction_accepts_markers_and_list_has_explicit_end():
    transaction = FactoryTransaction("list")
    transaction.feed("Begin file list")
    assert not transaction.list_started
    transaction.sent = True
    for line in [
        "Begin file list",
        "._DISPMNT01.gcode 4096",
        ".Spotlight-V100",
        "System Volume Information",
        "DISPMNT01.gcode 2113357",
        *EVIDENCE["unsolicited"],
        "ok",
    ]:
        transaction.feed(line)
    assert not transaction.done
    transaction.feed("End file list")
    assert transaction.done and transaction.data["entries"] == [
        {
            "name": "DISPMNT01.gcode",
            "kind": "file",
            "size_bytes": 2113357,
            "display_name": "DISPMNT01.gcode",
            "source": "factory_usb_m20",
            "geometry_available": False,
        }
    ]


def test_simulated_coordinates_and_configuration_never_claim_complete_backup(monkeypatch):
    position = FactoryTransaction("coordinates")
    position.sent = True
    position.feed("X:12.3 Y:45.6 Z:0.2 E:0 Count X:123 Y:456 Z:20")
    assert (
        position.done
        and position.data["position_kind"] == "firmware_reported_not_physical_measurement"
    )
    configuration = FactoryTransaction("configuration")
    configuration.sent = True
    configuration.feed("echo: M92 X80 Y80 Z400 E90")
    configuration.feed("ok")
    assert not configuration.done
    monkeypatch.setattr(time, "monotonic", lambda: configuration.last_data + 0.31)
    assert configuration.poll() and configuration.data["complete"] is False


@pytest.mark.parametrize(
    "name",
    [
        "._A.gcode",
        ".Spotlight-V100/a.gcode",
        "System Volume Information/a.gcode",
        "$RECYCLE.BIN/a.gcode",
        ".Trashes/a.gcode",
    ],
)
def test_catalog_system_entries_hidden(name):
    assert not catalog_visible(name)


def test_factory_queries_have_no_motion_heating_or_print_commands():
    assert set(SCOPE["FACTORY_COMMANDS"].values()) <= {
        "M115",
        "M503",
        "M105",
        "M114",
        "M20",
        "M20 L",
        "M27",
        "M23",
    }
    assert EVIDENCE["experimental_firmware_flashed"] is False
    assert "SD content retrieval" in EVIDENCE["not_verified"]


def test_factory_api_auth_gates_and_reported_coordinate_pipeline(monkeypatch):
    import asyncio
    from collections import deque
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from mks_wifi_bridge import factory_usb

    app = FastAPI()

    async def auth(request: Request):
        if request.headers.get("authorization") != "Bearer fixture":
            raise HTTPException(401)

    async def write(request: Request):
        await auth(request)
        if request.headers.get("origin") != "http://testserver":
            raise HTTPException(403)

    async def fake_query(backend, payload):
        return {
            "data": {"raw_line": "X:12.3 Y:45.6 Z:0.2 E:0 Count X:123 Y:456 Z:20"},
            "observed_at": time.time(),
        }

    monkeypatch.setattr(factory_usb, "query", fake_query)
    monkeypatch.setenv("NEPTUNE_FACTORY_USB_SELECTION_ENABLED", "false")
    printer = SimpleNamespace(
        backend=object(),
        io_lock=asyncio.Lock(),
        motion_latest=None,
        motion_history=deque(maxlen=10),
    )
    factory_usb.install(app, auth, write, printer)
    with TestClient(app) as client:
        assert client.get("/api/factory-usb").status_code == 401
        client.headers.update({"authorization": "Bearer fixture"})
        assert (
            client.post("/api/factory-usb/query", json={"operation": "coordinates"}).status_code
            == 403
        )
        client.headers.update({"origin": "http://testserver"})
        assert client.post("/api/factory-usb/query", json={"operation": "M24"}).status_code == 422
        assert (
            client.post(
                "/api/factory-usb/query",
                json={"operation": "select", "filename": "DISPMNT01.gcode"},
            ).status_code
            == 423
        )
        assert (
            client.post("/api/factory-usb/query", json={"operation": "coordinates"}).status_code
            == 200
        )
        assert printer.motion_latest["axes"]["x"]["value_mm"] == 12.3
        assert printer.motion_latest["source"] == "factory_usb_M114"


def test_selected_factory_sd_file_is_not_an_active_print():
    from mks_wifi_bridge.jobs import print_job_summary

    result = print_job_summary(
        {
            "connection": {"connected": True, "transport": "OctoPrint API"},
            "stale": False,
            "telemetry": {
                "connection_transport": "usb",
                "print_active": {"status": "available", "value": False},
                "printer_state": {"status": "available", "value": "idle"},
                "factory_usb": {
                    "select": {
                        "data": {
                            "selected": True,
                            "filename": "DISPMNT01.gcode",
                            "size_bytes": 2113357,
                        }
                    },
                    "status": {"data": {"byte_position": 0, "total_bytes": 2113357}},
                },
            },
        }
    )
    assert result["status"] == "selected" and result["active"] is None
    assert result["filename"]["value"] == "DISPMNT01.gcode"
    assert result["progress_percent"]["value"] is None
