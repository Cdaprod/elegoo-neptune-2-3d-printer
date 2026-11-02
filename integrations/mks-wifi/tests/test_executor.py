from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mks_wifi_bridge.capabilities import CapabilityRegistry
from mks_wifi_bridge.commands import MACROS, DryRunCommandService
from mks_wifi_bridge.executor import (
    CommandExecutor,
    CommandTransportError,
    CooldownCommissioningExecutor,
    ExecutionBlocked,
    MksTcpCommandTransport,
)


def snapshot(state: str = "idle", age: float = 0.1, nozzle: float = 200.0) -> dict[str, Any]:
    return {
        "connection": {"connected": True},
        "stale": age > 5,
        "data_age_seconds": age,
        "telemetry": {
            "printer_state": {"value": state},
            "print_active": {"value": state in {"printing", "paused"}},
            "nozzle_temperature": {"value": nozzle},
        },
    }


def verified_executor(**kwargs: Any) -> CommandExecutor:
    """Build a test executor with explicit synthetic commissioning evidence."""
    requested = kwargs.pop("verified_macros", frozenset())
    registry = CapabilityRegistry()
    for macro_id in requested:
        capability = registry.get(macro_id)
        if capability["status"] == "ready_for_commissioning":
            registry.begin(macro_id, f"test-{macro_id}", [])
            registry.verify(
                macro_id,
                {"id": f"test-{macro_id}", "macro_id": macro_id, "commands": []},
                {"kind": "mocked_test_evidence"},
            )
    kwargs["capability_registry"] = registry
    executor = CommandExecutor(**kwargs)
    # Unit cases using this helper represent an already-armed dashboard
    # session; production and API tests use the explicit arm route.
    if executor.control_configured:
        executor.arm_session(attended=True, power_switch_reachable=True, no_competing_clients=True)
    return executor


class FakeTransport:
    def __init__(self, fail: CommandTransportError | None = None) -> None:
        self.commands: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.failure = fail

    async def connect(self) -> None:
        return None

    async def send(self, command: str) -> dict[str, Any]:
        self.commands.append(command)
        self.entered.set()
        if self.failure:
            raise self.failure
        await self.release.wait()
        return {"acknowledged": True, "acknowledgment": "ok"}

    async def close(self) -> None:
        return None


def execute(executor: CommandExecutor, transport: FakeTransport) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        transport.release.set()
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="cooldown",
            parameters={},
            confirmation="Cooldown",
            snapshot=snapshot(),
            io_lock=asyncio.Lock(),
        )
        await executor._tasks[queued["id"]]
        return executor.get(queued["id"]) or {}

    return asyncio.run(run())


def test_executor_defaults_disabled() -> None:
    assert CommandExecutor().execution_enabled is False


def test_command_history_survives_restart_without_restoring_motion_authority(tmp_path) -> None:
    history_path = tmp_path / "command-history.json"
    transport = FakeTransport()
    executor = verified_executor(
        enabled=True,
        verified_commands=frozenset({"M104", "M140"}),
        verified_macros=frozenset({"cooldown"}),
        transport_factory=lambda: transport,
        history_path=history_path,
    )
    operation = execute(executor, transport)
    assert operation["commands"] == ["M104 S0", "M140 S0"]
    assert operation["transport_acknowledged"] is True

    restarted = CommandExecutor(history_path=history_path)
    saved = restarted.history()
    assert len(saved) == 1
    assert saved[0]["commands"] == ["M104 S0", "M140 S0"]
    assert saved[0]["command_results"][0]["acknowledgment"] == "ok"
    assert restarted.execution_enabled is False
    assert restarted.homed_axes == set()


def test_external_home_reference_records_operator_report_without_command() -> None:
    executor = CommandExecutor()
    result = executor.record_external_homing_reference(["x", "y", "z"])
    assert result["commands_sent"] == []
    assert result["source"] == "operator_reported_stock_printer_touchscreen"
    assert executor.homed_axes == {"x", "y", "z"}
    assert executor.axis_positions_mm == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert all(
        executor.axis_reference_observations[axis]["kind"]
        == "operator_reported_external_homing_reference"
        for axis in "xyz"
    )


def test_external_home_reference_closes_pending_ack_without_claiming_command_success() -> None:
    from mks_wifi_bridge.executor import Operation

    executor = CommandExecutor()
    operation = Operation(
        id="home-x",
        macro_id="home_x",
        state="transport_acknowledged",
        created_at="now",
        updated_at="now",
        commands=["G28 X"],
        transport_acknowledged=True,
        observed_axes=["x"],
        observation_required=True,
    )
    executor.operations.append(operation)
    executor.pending_axes.add("x")
    executor.record_external_homing_reference(["x"])
    saved = executor.get("home-x")
    assert saved is not None
    assert saved["operator_observation"] == "operator_reported_external_tft_homing"
    assert saved["physical_completion_confirmed"] is False
    assert saved["awaiting_operator_observation"] is False
    assert "x" not in executor.pending_axes


def test_external_home_reference_rejects_unknown_axes() -> None:
    with pytest.raises(ValueError, match="X, Y, and Z"):
        CommandExecutor().record_external_homing_reference(["e"])


def test_capability_registry_exposes_unverified_operation_in_operator_mode() -> None:
    executor = verified_executor(
        enabled=True,
        verified_commands=frozenset({"M104", "M140"}),
        verified_macros=frozenset({"cooldown"}),
        transport_factory=lambda: FakeTransport(),
    )
    assert executor.execution_enabled is True
    assert executor.can_run_commands(["M104 S0", "M140 S0"], "cooldown") is True
    assert (
        executor.can_run_commands(["M104 S200", "M140 S60"], "preheat_pla", commissioning=True)
        is True
    )
    assert executor.can_run_commands(["M104 S200", "M140 S60"], "preheat_pla") is True


def test_missing_extrusion_temperature_gate_does_not_block_xyz_jogs() -> None:
    executor = verified_executor(
        enabled=True,
        verified_commands=frozenset({"G91", "G1", "G90"}),
        verified_macros=frozenset({"jog_x_positive", "purge_nozzle"}),
        transport_factory=lambda: FakeTransport(),
    )
    executor.homed_axes.add("x")
    executor.axis_positions_mm["x"] = 0.0
    assert executor.can_run_commands(["G91", "G1 X1 F600", "G90"], "jog_x_positive")
    assert not executor.can_run_commands(["M83", "G1 E5 F120", "M82"], "purge_nozzle")


def test_cooldown_commissioning_is_durable_one_time_and_sends_only_fixed_commands(
    tmp_path: Any,
) -> None:
    class ImmediateTransport:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    async def run() -> None:
        transport = ImmediateTransport()
        commissioning = CooldownCommissioningExecutor(
            lambda: transport,
            timeout=1,
            token="test-one-time-token",
            state_path=tmp_path / "cooldown.json",
        )
        result = await commissioning.run(asyncio.Lock())
        assert result["state"] == "transport_acknowledged"
        assert transport.commands == ["M104 S0", "M140 S0"]
        assert commissioning.used is True
        restarted = CooldownCommissioningExecutor(
            lambda: transport,
            timeout=1,
            token="test-one-time-token",
            state_path=tmp_path / "cooldown.json",
        )
        assert restarted.used is True
        with pytest.raises(ExecutionBlocked, match="consumed"):
            await restarted.run(asyncio.Lock())

    asyncio.run(run())


def test_cooldown_cancellation_after_dispatch_is_recorded_unknown_without_retry() -> None:
    class CancelledTransport:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            raise asyncio.CancelledError

        async def close(self) -> None:
            return None

    async def run() -> None:
        transport = CancelledTransport()
        result = await CooldownCommissioningExecutor(
            lambda: transport, timeout=1, token="cancel-test-token"
        ).run(asyncio.Lock())
        assert result["state"] == "outcome_unknown"
        assert len(transport.commands) == 1

    asyncio.run(run())


@pytest.mark.parametrize("printer_state", ["printing", "paused", "unknown"])
def test_non_idle_state_blocks_before_transport(printer_state: str) -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        )
        with pytest.raises(ExecutionBlocked, match="idle"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="cooldown",
                parameters={},
                confirmation="Cooldown",
                snapshot=snapshot(state=printer_state),
                io_lock=asyncio.Lock(),
            )
        assert transport.commands == []

    asyncio.run(run())


def test_cancel_print_is_limited_to_fresh_reported_active_jobs() -> None:
    class ImmediateTransport:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    async def run() -> None:
        transport = ImmediateTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M524"}),
            verified_macros=frozenset({"cancel_print"}),
            transport_factory=lambda: transport,
        )
        with pytest.raises(ExecutionBlocked, match="active or paused print"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="cancel_print",
                parameters={},
                confirmation="Cancel active print",
                snapshot=snapshot(),
                io_lock=asyncio.Lock(),
            )
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="cancel_print",
            parameters={},
            confirmation="Cancel active print",
            snapshot=snapshot(state="printing"),
            io_lock=asyncio.Lock(),
        )
        await executor._tasks[queued["id"]]
        assert transport.commands == ["M524"]

    asyncio.run(run())


def test_stale_telemetry_blocks_and_legacy_command_allowlist_is_not_the_gate() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        )
        with pytest.raises(ExecutionBlocked, match="[Ff]resh"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="cooldown",
                parameters={},
                confirmation="Cooldown",
                snapshot=snapshot(age=8),
                io_lock=asyncio.Lock(),
            )
        assert executor.can_run_commands(["M104 S0", "M140 S0"], "cooldown")
        assert not transport.commands

    asyncio.run(run())


def test_executor_revalidates_telemetry_after_waiting_for_shared_connection() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        )
        lock = asyncio.Lock()
        await lock.acquire()
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="cooldown",
            parameters={},
            confirmation="Cooldown",
            snapshot=snapshot(),
            io_lock=lock,
            snapshot_provider=lambda: snapshot(state="printing"),
        )
        await asyncio.sleep(0)
        lock.release()
        await executor._tasks[queued["id"]]
        result = executor.get(queued["id"])
        assert result is not None and result["state"] == "failed"
        assert "idle" in (result["error"] or "")
        assert transport.commands == []

    asyncio.run(run())


def test_cooldown_lock_timeout_does_not_consume_one_time_authorization() -> None:
    async def run() -> None:
        transport = FakeTransport()
        lock = asyncio.Lock()
        await lock.acquire()
        commissioner = CooldownCommissioningExecutor(
            lambda: transport, timeout=0.01, token="timeout-token"
        )
        result = await commissioner.run(lock)
        lock.release()
        assert result["state"] == "failed"
        assert commissioner.used is False
        assert transport.commands == []

    asyncio.run(run())


def test_axis_jog_requires_operator_confirmed_home_and_tracks_only_confirmed_steps() -> None:
    class ImmediateTransport:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def connect(self) -> None:
            return None

        async def send(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            return {"acknowledged": True, "acknowledgment": "ok"}

        async def close(self) -> None:
            return None

    async def run() -> None:
        transport = ImmediateTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"G90", "G91", "G28", "G1"}),
            verified_macros=frozenset({"home_x", "jog_x_positive", "jog_x_negative"}),
            transport_factory=lambda: transport,
        )
        preview = DryRunCommandService()
        lock = asyncio.Lock()
        with pytest.raises(ExecutionBlocked, match="Home the axis first"):
            await executor.start(
                preview_service=preview,
                macro_id="jog_x_positive",
                parameters={"distance_mm": "1", "feed_mm_min": 300},
                confirmation="Jog X Positive",
                snapshot=snapshot(),
                io_lock=lock,
            )

        with pytest.raises(ExecutionBlocked, match="at their home switches"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="home_x",
                parameters={},
                confirmation="Home X",
                snapshot=snapshot(),
                io_lock=lock,
                retreat_from_home=True,
            )
        assert transport.commands == []

        home = await executor.start(
            preview_service=preview,
            macro_id="home_x",
            parameters={},
            confirmation="Home X",
            snapshot=snapshot(),
            io_lock=lock,
            axis_at_home=True,
            retreat_from_home=True,
        )
        await executor._tasks[home["id"]]
        assert transport.commands == ["G91", "G1 X30 F600", "G90", "G28 X"]
        completed_home = executor.get(home["id"])
        assert completed_home["state"] == "transport_acknowledged"
        assert completed_home["physical_completion_confirmed"] is False
        assert completed_home["awaiting_operator_observation"] is False
        assert executor.axis_positions_mm["x"] == 0.0

        normal_home = await executor.start(
            preview_service=preview,
            macro_id="home_x",
            parameters={},
            confirmation="Home X",
            snapshot=snapshot(),
            io_lock=lock,
        )
        await executor._tasks[normal_home["id"]]
        assert transport.commands[-1:] == ["G28 X"]
        assert executor.axis_positions_mm["x"] == 0.0

        jog = await executor.start(
            preview_service=preview,
            macro_id="jog_x_positive",
            parameters={"distance_mm": "10", "feed_mm_min": 300},
            confirmation="Jog X Positive",
            snapshot=snapshot(),
            io_lock=lock,
        )
        await executor._tasks[jog["id"]]
        assert transport.commands[-3:] == ["G91", "G1 X10 F300", "G90"]
        assert jog["planned_axis"] == "x"
        assert jog["planned_start_position_mm"] == 0.0
        assert jog["planned_end_position_mm"] == 10.0
        assert jog["planned_distance_mm"] == 10.0
        assert jog["estimated_duration_seconds"] == 2.0
        result = executor.get(jog["id"])
        move_result = result["command_results"][1]
        assert move_result["dispatched_at"]
        assert move_result["acknowledged_at"]
        assert move_result["verification_confidence"] == "transport_acknowledgment_only"
        assert result["physical_completion_confirmed"] is False
        assert executor.axis_positions_mm["x"] == 10.0
        assert executor.axis_positions_mm["x"] == 10.0

        return_home = await executor.start(
            preview_service=preview,
            macro_id="jog_x_negative",
            parameters={"distance_mm": "-10", "feed_mm_min": 300},
            confirmation="Jog X Negative",
            snapshot=snapshot(),
            io_lock=lock,
        )
        await executor._tasks[return_home["id"]]
        assert executor.axis_positions_mm["x"] == 0.0
        assert executor.axis_positions_mm["x"] == 0.0

        with pytest.raises(ExecutionBlocked, match="outside the conservative"):
            await executor.start(
                preview_service=preview,
                macro_id="jog_x_negative",
                parameters={"distance_mm": "-0.1", "feed_mm_min": 300},
                confirmation="Jog X Negative",
                snapshot=snapshot(),
                io_lock=lock,
            )

    asyncio.run(run())


def test_new_fixed_control_families_are_transport_allowlisted() -> None:
    async def run() -> None:
        received: list[str] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while command := await reader.readline():
                received.append(command.decode().strip())
                if received[-1] == "M119":
                    writer.write(b"x_min: open\r\ny_min: open\r\nok\r\n")
                else:
                    writer.write(b"ok\r\n")
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = MksTcpCommandTransport("127.0.0.1", port, timeout=0.2)
        try:
            await transport.connect()
            results = [
                await transport.send(command) for command in ("M106 S255", "M107", "M119", "M524")
            ]
            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
        assert received == ["M106 S255", "M107", "M119", "M524"]
        assert all(result["acknowledged"] for result in results)
        assert results[2]["response_lines"] == ["x_min: open", "y_min: open", "ok"]

    asyncio.run(run())


def test_mks_command_transport_uses_crlf_and_accepts_only_fixed_command_shapes() -> None:
    async def run() -> None:
        received: list[bytes] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.readline())
            writer.write(b"ok\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            transport = MksTcpCommandTransport("127.0.0.1", port, timeout=0.2)
            await transport.connect()
            response = await transport.send("G28 X")
            await transport.close()
        finally:
            server.close()
            await server.wait_closed()
        assert received == [b"G28 X\r\n"]
        assert response["acknowledged"] is True

        transport = MksTcpCommandTransport("127.0.0.1", port, timeout=0.2)
        with pytest.raises(CommandTransportError, match="allowlist"):
            await transport.send("M112")

    asyncio.run(run())


def test_exact_confirmation_and_dry_run_have_no_dispatch() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        )
        with pytest.raises(ExecutionBlocked, match="exact operation name"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="cooldown",
                parameters={},
                confirmation="yes",
                snapshot=snapshot(),
                io_lock=asyncio.Lock(),
            )
        assert not transport.commands

    asyncio.run(run())


def test_acknowledgment_is_not_reported_as_physical_completion() -> None:
    transport = FakeTransport()
    result = execute(
        verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        ),
        transport,
    )
    assert result["state"] == "transport_acknowledged"
    assert result["transport_acknowledged"] is True
    assert result["physical_completion_confirmed"] is False
    assert transport.commands == ["M104 S0", "M140 S0"]


def test_disconnect_after_send_is_outcome_unknown_and_is_not_retried() -> None:
    transport = FakeTransport(CommandTransportError("connection lost", sent=True))
    result = execute(
        verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        ),
        transport,
    )
    assert result["state"] == "outcome_unknown"
    assert len(transport.commands) == 1


def test_ack_timeout_is_bounded_and_recorded_as_unknown_without_retry() -> None:
    class StalledTransport(FakeTransport):
        async def send(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            await asyncio.Event().wait()
            return {}

    async def run() -> None:
        transport = StalledTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
            timeout=0.02,
        )
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="cooldown",
            parameters={},
            confirmation="Cooldown",
            snapshot=snapshot(),
            io_lock=asyncio.Lock(),
        )
        await asyncio.wait_for(executor._tasks[queued["id"]], timeout=0.2)
        result = executor.get(queued["id"])
        assert result["state"] == "outcome_unknown"
        assert len(transport.commands) == 1

    asyncio.run(run())


def test_unexpected_transport_failure_after_dispatch_is_unknown_without_retry() -> None:
    class BrokenTransport(FakeTransport):
        async def send(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            raise RuntimeError("simulated parser failure")

    transport = BrokenTransport()
    result = execute(
        verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        ),
        transport,
    )
    assert result["state"] == "outcome_unknown"
    assert len(transport.commands) == 1


def test_concurrent_operation_is_rejected() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        )
        lock = asyncio.Lock()
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="cooldown",
            parameters={},
            confirmation="Cooldown",
            snapshot=snapshot(),
            io_lock=lock,
        )
        await asyncio.wait_for(transport.entered.wait(), timeout=1)
        with pytest.raises(ExecutionBlocked, match="already active"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="cooldown",
                parameters={},
                confirmation="Cooldown",
                snapshot=snapshot(),
                io_lock=lock,
            )
        transport.release.set()
        await executor._tasks[queued["id"]]

    asyncio.run(run())


def test_cancel_stops_before_next_command_and_sends_no_stop_command() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"M104", "M140"}),
            verified_macros=frozenset({"cooldown"}),
            transport_factory=lambda: transport,
        )
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="cooldown",
            parameters={},
            confirmation="Cooldown",
            snapshot=snapshot(),
            io_lock=asyncio.Lock(),
        )
        await asyncio.wait_for(transport.entered.wait(), timeout=1)
        await executor.cancel(queued["id"])
        transport.release.set()
        await executor._tasks[queued["id"]]
        assert executor.get(queued["id"])["state"] == "cancelled_before_next_command"
        assert transport.commands == ["M104 S0"]

    asyncio.run(run())


def test_extrusion_needs_configured_minimum_and_fresh_hot_reading() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = verified_executor(
            enabled=True,
            verified_commands=frozenset({"G1", "M82", "M83"}),
            verified_macros=frozenset({"purge_nozzle"}),
            transport_factory=lambda: transport,
            extrusion_min_temperature_c=180,
        )
        with pytest.raises(ExecutionBlocked, match="reaches 180"):
            await executor.start(
                preview_service=DryRunCommandService(),
                macro_id="purge_nozzle",
                parameters={"length_mm": 10, "feed_mm_min": 120},
                confirmation="Purge nozzle",
                snapshot=snapshot(nozzle=179),
                io_lock=asyncio.Lock(),
            )
        transport.release.set()
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id="purge_nozzle",
            parameters={"length_mm": 10, "feed_mm_min": 120},
            confirmation="Purge nozzle",
            snapshot=snapshot(nozzle=180),
            io_lock=asyncio.Lock(),
        )
        await executor._tasks[queued["id"]]
        assert transport.commands == ["M83", "G1 E10 F120", "M82"]

    asyncio.run(run())


def orbital_snapshot() -> dict[str, Any]:
    result = snapshot(nozzle=24.0)
    result["telemetry"]["nozzle_temperature"]["target_c"] = 0.0
    result["telemetry"]["bed_temperature"] = {"value": 23.0, "target_c": 0.0}
    return result


def test_orbital_dance_runs_full_guarded_sequence_with_mock_transport() -> None:
    async def run() -> None:
        transport = FakeTransport()
        transport.release.set()
        executor = CommandExecutor(enabled=True, transport_factory=lambda: transport)
        executor.arm_session(attended=True, power_switch_reachable=True, no_competing_clients=True)
        macro = next(item for item in MACROS if item.id == "neptune_3_axis_orbital_dance")
        params = {"amplitude_percent": 7, "feed_mm_min": 6000}
        preview = DryRunCommandService().preview(macro.id, params)
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id=macro.id,
            parameters=params,
            confirmation=macro.name,
            snapshot=orbital_snapshot(),
            io_lock=asyncio.Lock(),
        )
        await executor._tasks[queued["id"]]
        result = executor.get(queued["id"])
        assert result["state"] == "transport_acknowledged"
        assert result["physical_completion_confirmed"] is False
        assert transport.commands == preview["commands"]
        assert transport.commands[0] == "M400"
        assert transport.commands[1] == "G28"
        assert transport.commands[-2:] == ["G28", "M400"]
        assert executor.homed_axes == {"x", "y", "z"}
        assert all(
            executor.axis_reference_observations[axis]["kind"]
            == "firmware_homing_acknowledgment_estimate"
            for axis in "xyz"
        )

    asyncio.run(run())


def test_orbital_dance_cancel_stops_without_final_home() -> None:
    async def run() -> None:
        transport = FakeTransport()
        executor = CommandExecutor(enabled=True, transport_factory=lambda: transport)
        executor.arm_session(attended=True, power_switch_reachable=True, no_competing_clients=True)
        macro = next(item for item in MACROS if item.id == "neptune_3_axis_orbital_dance")
        queued = await executor.start(
            preview_service=DryRunCommandService(),
            macro_id=macro.id,
            parameters={"amplitude_percent": 7, "feed_mm_min": 6000},
            confirmation=macro.name,
            snapshot=orbital_snapshot(),
            io_lock=asyncio.Lock(),
        )
        await asyncio.wait_for(transport.entered.wait(), timeout=1)
        await executor.cancel(queued["id"])
        transport.release.set()
        await executor._tasks[queued["id"]]
        assert executor.get(queued["id"])["state"] == "cancelled_before_next_command"
        assert transport.commands == ["M400"]
        assert "G28" not in transport.commands

    asyncio.run(run())
