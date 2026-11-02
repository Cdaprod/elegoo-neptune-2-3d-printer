from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from mkswifi import PrinterState, PrinterStatus, Temperature

from mks_wifi_bridge.observatory import PrinterService, create_app
from mks_wifi_bridge.workflows import GuidedWorkflowManager, WorkflowError


def snapshot(
    *,
    connected: bool = True,
    stale: bool = False,
    age: float = 0.1,
    state: str = "idle",
    nozzle: float = 24,
    target: float = 0,
) -> dict[str, Any]:
    return {
        "connection": {"connected": connected},
        "stale": stale,
        "data_age_seconds": age,
        "telemetry": {
            "printer_state": {"value": state},
            "print_active": state in {"printing", "paused"},
            "nozzle_temperature": {"value": nozzle, "target_c": target},
        },
    }


def test_workflow_waits_for_operator_and_never_emits_commands() -> None:
    manager = GuidedWorkflowManager()
    current = manager.start(
        "filament_change_guided", {"minimum_nozzle_temperature_c": 190}, snapshot(), False
    )
    assert current["state"] == "awaiting_operator_confirmation"
    assert current["current_step"].startswith("Confirm Observatory")
    next_step = manager.confirm(current["id"], current["step_token"], True, snapshot(), False)
    assert next_step["step_index"] == 1
    assert next_step["step_token"] != current["step_token"]
    assert "preheat_pla" in next_step["available_operations"]
    assert manager.allows_operation("preheat_pla")
    assert not manager.allows_operation("filament_load")
    next_step = manager.confirm(next_step["id"], next_step["step_token"], True, snapshot(), False)
    assert next_step["step_index"] == 2
    assert next_step["available_operations"] == []
    with pytest.raises(WorkflowError, match="at least 190"):
        manager.confirm(next_step["id"], next_step["step_token"], True, snapshot(nozzle=180), False)
    assert manager.active is not None
    assert manager.active["step_index"] == 2
    assert "at least 190" in manager.active["last_error"]
    hot = snapshot(nozzle=195, target=200)
    next_step = manager.confirm(next_step["id"], next_step["step_token"], True, hot, False)
    assert next_step["step_index"] == 3
    assert "filament_load" in next_step["available_operations"]
    assert manager.allows_operation("filament_load")
    assert not manager.allows_operation("home_x")


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"connected": False}, "connection"),
        ({"stale": True}, "stale"),
        ({"age": 6}, "stale"),
        ({"state": "printing"}, "idle"),
    ],
)
def test_confirmation_rechecks_live_safety_and_remains_paused(
    condition: dict[str, Any], message: str
) -> None:
    manager = GuidedWorkflowManager()
    current = manager.start("filament_change_guided", {}, snapshot(), False)
    with pytest.raises(WorkflowError, match=message):
        manager.confirm(
            current["id"],
            current["step_token"],
            True,
            snapshot(**condition),
            False,
        )
    assert manager.active is not None
    assert manager.active["step_index"] == 0
    assert manager.active["last_error"]


def test_temperature_gate_is_rechecked_on_every_confirmation() -> None:
    manager = GuidedWorkflowManager()
    current = manager.start(
        "filament_change_guided",
        {"minimum_nozzle_temperature_c": 190},
        snapshot(nozzle=200, target=200),
        False,
    )
    current = manager.confirm(current["id"], current["step_token"], True, snapshot(), False)
    current = manager.confirm(current["id"], current["step_token"], True, snapshot(), False)
    with pytest.raises(WorkflowError, match="at least 190"):
        manager.confirm(
            current["id"], current["step_token"], True, snapshot(nozzle=180, target=200), False
        )


def test_workflow_rejects_replay_conflict_unsupported_macro_and_cancels() -> None:
    manager = GuidedWorkflowManager()
    with pytest.raises(WorkflowError, match="Only registered"):
        manager.start("cooldown", {}, snapshot(), False)
    with pytest.raises(WorkflowError, match="active"):
        manager.start("filament_change_guided", {}, snapshot(), True)
    current = manager.start("filament_change_guided", {}, snapshot(), False)
    with pytest.raises(WorkflowError, match="stale"):
        manager.confirm(current["id"], "old-token", True, snapshot(), False)
    cancelled = manager.cancel(current["id"])
    assert cancelled["state"] == "cancelled"
    assert manager.active is None
    assert manager.history[-1]["state"] == "cancelled"


def test_filament_workflow_rejects_missing_or_unsafe_temperature_threshold() -> None:
    manager = GuidedWorkflowManager()
    with pytest.raises(WorkflowError, match="150 and 260"):
        manager.start(
            "filament_change_guided", {"minimum_nozzle_temperature_c": 0}, snapshot(), False
        )
    with pytest.raises(WorkflowError, match="150 and 260"):
        manager.start(
            "filament_change_guided", {"minimum_nozzle_temperature_c": 300}, snapshot(), False
        )


class MockClient:
    async def get_status(self) -> Any:
        from datetime import timedelta

        return PrinterStatus(
            state=PrinterState.IDLE,
            nozzles={0: Temperature(24.0, 0.0)},
            bed=Temperature(23.0, 0.0),
            progress=0,
            elapsed=timedelta(seconds=0),
        )

    async def get_firmware(self) -> str:
        return "TFT28/32"


def service() -> PrinterService:
    return PrinterService(
        host="127.0.0.1", interval=30, client_factory=lambda *_args, **_kwargs: MockClient()
    )


def test_authenticated_workflow_api_pauses_confirms_and_cancels() -> None:
    app = create_app(service())
    with TestClient(app) as client:
        assert client.get("/api/control/workflows").status_code == 200
        import asyncio

        asyncio.run(app.state.printer.poll_once())
        started = client.post(
            "/api/control/workflows",
            json={"macro_id": "filament_change_guided", "parameters": {}},
        )
        assert started.status_code == 201
        first = started.json()
        assert first["state"] == "awaiting_operator_confirmation"
        assert first["available_operations"] == []
        denied = client.post("/api/control/execute", json={"macro_id": "cooldown"})
        assert denied.status_code == 409
        no_auth = TestClient(create_app(service()), client=("198.51.100.44", 53000))
        with no_auth:
            assert (
                no_auth.post(
                    "/api/control/workflows",
                    json={"macro_id": "filament_change_guided"},
                ).status_code
                == 401
            )
        confirm = client.post(
            f"/api/control/workflows/{first['id']}/confirm",
            json={"step_token": first["step_token"], "confirmed": True},
        )
        assert confirm.status_code == 200
        assert confirm.json()["step_index"] == 1
        assert "preheat_pla" in confirm.json()["available_operations"]
        denied = client.post("/api/control/execute", json={"macro_id": "home_x"})
        assert denied.status_code == 409
        assert client.get("/api/control/state").json()["control"]["enabled"] is False
        cancelled = client.post(f"/api/control/workflows/{first['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"


def test_workflow_api_revalidates_idle_and_temperature_before_advance() -> None:
    app = create_app(service())
    with TestClient(app) as client:
        import asyncio

        asyncio.run(app.state.printer.poll_once())
        start_response = client.post(
            "/api/control/workflows",
            json={
                "macro_id": "filament_change_guided",
                "parameters": {"minimum_nozzle_temperature_c": 190},
            },
        )
        assert start_response.status_code == 201, start_response.text
        started = start_response.json()
        first = client.post(
            f"/api/control/workflows/{started['id']}/confirm",
            json={"step_token": started["step_token"], "confirmed": True},
        ).json()
        second = client.post(
            f"/api/control/workflows/{started['id']}/confirm",
            json={"step_token": first["step_token"], "confirmed": True},
        ).json()
        blocked = client.post(
            f"/api/control/workflows/{started['id']}/confirm",
            json={"step_token": second["step_token"], "confirmed": True},
        )
        assert blocked.status_code == 409
        assert "at least 190" in blocked.json()["detail"]
        state = client.get(f"/api/control/workflows/{started['id']}").json()
        assert state["step_index"] == 2
        assert state["last_error"]
        assert client.post("/api/control/execute", json={"macro_id": "cooldown"}).status_code == 409
