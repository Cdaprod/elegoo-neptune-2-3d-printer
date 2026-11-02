"""Guarded fixed-command execution infrastructure.

Operator Mode starts disarmed and remains armed until explicit disarm or
service restart. Routine operations do not require prior physical verification;
they still require fresh telemetry, validated commands, and operator confirmation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .capabilities import CapabilityRegistry
from .commands import MACROS, DryRunCommandService, PreviewError

KNOWN_COMMANDS = frozenset(
    {
        "G28",
        "G1",
        "G90",
        "G91",
        "M17",
        "M18",
        "M400",
        "M82",
        "M83",
        "M84",
        "M104",
        "M106",
        "M107",
        "M119",
        "M109",
        "M140",
        "M190",
        "M23",
        "M24",
        "M25",
        "M524",
    }
)
KNOWN_MACROS = frozenset(macro.id for macro in MACROS if macro.templates)
_COMMAND_PATTERN = re.compile(
    r"^(?:G90|G91|M17|M18|M82|M83|M84|M400|M24|M25|M107|M119|M524|G28(?: [XYZ])?|"
    r"G1 E-?\d+(?:\.\d+)? F\d+(?:\.\d+)?|"
    r"G1 [XYZ]-?\d+(?:\.\d+)? F\d+(?:\.\d+)?|"
    r"G1 (?:[XYZ]-?\d+(?:\.\d+)?)(?: [XYZ]-?\d+(?:\.\d+)?){1,2} F\d+(?:\.\d+)?|"
    r"M104 S\d+(?:\.\d+)?|M109 S\d+(?:\.\d+)?|"
    r"M106 S\d{1,3}|"
    r"M140 S\d+(?:\.\d+)?|M190 S\d+(?:\.\d+)?|"
    r"M23 [A-Za-z0-9_./-]+)$"
)
MAX_RESPONSE_BYTES = 16_384


class ExecutionBlocked(RuntimeError):
    """An operation failed a safety gate before any command was sent."""


class CommandTransportError(RuntimeError):
    """Transport failure with explicit uncertainty about whether a command ran."""

    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class CommandTransport(Protocol):
    async def connect(self) -> None: ...

    async def send(self, command: str) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class MksTcpCommandTransport:
    """Line-based MKS TCP command session; caller must hold PrinterService.io_lock."""

    def __init__(self, host: str, port: int, timeout: float = 15.0) -> None:
        if timeout <= 0:
            raise ValueError("command timeout must be positive")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (OSError, TimeoutError) as exc:
            raise CommandTransportError(
                f"Could not connect to MKS WiFi: {exc}", sent=False
            ) from exc

    async def send(self, command: str, *, read_only: bool = False) -> dict[str, Any]:
        if not _COMMAND_PATTERN.fullmatch(command) and not (read_only and command == "M114"):
            raise CommandTransportError(
                "Generated command failed the transport allowlist.", sent=False
            )
        if self.reader is None or self.writer is None or self.writer.is_closing():
            raise CommandTransportError("MKS command session is not connected.", sent=False)
        try:
            self.writer.write((command + "\r\n").encode("ascii"))
            await asyncio.wait_for(self.writer.drain(), timeout=self.timeout)
        except Exception as exc:
            raise CommandTransportError(f"Command write failed: {exc}", sent=True) from exc

        received = 0
        lines: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while received < MAX_RESPONSE_BYTES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CommandTransportError(
                    "No command acknowledgment before timeout; outcome is unknown.", sent=True
                )
            try:
                raw = await asyncio.wait_for(self.reader.readline(), timeout=remaining)
            except (OSError, TimeoutError) as exc:
                raise CommandTransportError(
                    f"No command acknowledgment before timeout; outcome is unknown: {exc}",
                    sent=True,
                ) from exc
            if not raw:
                raise CommandTransportError(
                    "MKS connection closed before acknowledgment; outcome is unknown.", sent=True
                )
            received += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line[:500])
            if line.lower().startswith("error:") or line.lower().startswith("!!"):
                raise CommandTransportError("Printer rejected command: " + line[:300], sent=True)
            if line.lower() == "ok" or line.lower().startswith("ok "):
                return {
                    "acknowledged": True,
                    "acknowledgment": line[:500],
                    "response_lines": lines[-20:],
                }
        raise CommandTransportError(
            "MKS response exceeded the bounded response limit; outcome is unknown.", sent=True
        )

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=self.timeout)
            except (OSError, TimeoutError):
                pass
        self.reader = None
        self.writer = None


@dataclass
class Operation:
    id: str
    macro_id: str
    state: str
    created_at: str
    updated_at: str
    commands: list[str]
    command_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    cancel_requested: bool = False
    transport_acknowledged: bool = False
    physical_completion_confirmed: bool = False
    telemetry_before: dict[str, Any] | None = None
    telemetry_after: dict[str, Any] | None = None
    observed_axes: list[str] = field(default_factory=list)
    proposed_position_mm: float | None = None
    planned_axis: str | None = None
    planned_start_position_mm: float | None = None
    planned_distance_mm: float | None = None
    estimated_duration_seconds: float | None = None
    observation_required: bool = False
    operator_observation: str | None = None
    commissioning: bool = False
    mode: str = "operator"
    operator_authorized: bool = True
    command_dispatched: bool = False
    last_dispatched_at: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "macro_id": self.macro_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "commands": self.commands,
            "command_results": self.command_results,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "cancellation_note": (
                "Cancellation stops commands that have not been sent. "
                "It cannot interrupt an in-flight command and is not an emergency stop."
            ),
            "transport_acknowledged": self.transport_acknowledged,
            "command_dispatched": self.command_dispatched,
            "last_dispatched_at": self.last_dispatched_at,
            "operator_authorized": self.operator_authorized,
            "mode": "commissioning" if self.commissioning else self.mode,
            "physical_completion_confirmed": self.physical_completion_confirmed,
            "awaiting_operator_observation": (
                self.observation_required
                and self.state == "transport_acknowledged"
                and not self.physical_completion_confirmed
                and self.operator_observation is None
            ),
            "observed_axes": self.observed_axes,
            "proposed_position_mm": self.proposed_position_mm,
            "planned_axis": self.planned_axis,
            "planned_start_position_mm": self.planned_start_position_mm,
            "planned_end_position_mm": self.proposed_position_mm,
            "planned_distance_mm": self.planned_distance_mm,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "verification_confidence": (
                "operator_observed"
                if self.physical_completion_confirmed
                else "transport_acknowledgment_only"
                if self.transport_acknowledged
                else "not_acknowledged"
            ),
            "operator_observation": self.operator_observation,
            "commissioning": self.commissioning,
            "parameters": self.parameters,
            "telemetry_before": self.telemetry_before,
            "telemetry_after": self.telemetry_after,
            "telemetry_observation": (
                "Waiting for a read-only sample captured after the transport acknowledgment."
                if self.telemetry_after is None
                else "Most recent read-only sample after transport acknowledgment."
            ),
        }


class CooldownCommissioningExecutor:
    """One-shot, cooldown-only commissioning path; never accepts arbitrary commands."""

    def __init__(
        self,
        transport_factory: Any,
        timeout: float = 8.0,
        *,
        token: str | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        if not 0 < timeout <= 15:
            raise ValueError("commissioning timeout must be between 0 and 15 seconds")
        self.transport_factory = transport_factory
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self.state_path = Path(state_path) if state_path else None
        self.token_hash = hashlib.sha256(token.encode()).hexdigest() if token else None
        self._ledger = self._read_ledger()
        self._used = bool(
            self.token_hash and self.token_hash in self._ledger["consumed_token_hashes"]
        )

    def _read_ledger(self) -> dict[str, Any]:
        if self.state_path is None or not self.state_path.exists():
            return {"consumed_token_hashes": [], "operations": []}
        try:
            ledger = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(ledger, dict):
                raise ValueError("commissioning state must be a JSON object")
            ledger.setdefault("consumed_token_hashes", [])
            ledger.setdefault("operations", [])
            return ledger
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Could not read cooldown commissioning state: {exc}") from exc

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._ledger, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _consume_authorization(self) -> None:
        if not self.token_hash:
            raise ExecutionBlocked("No commissioning credential is configured.")
        if self.token_hash in self._ledger["consumed_token_hashes"]:
            self._used = True
            raise ExecutionBlocked("This one-time commissioning authorization has been consumed.")
        self._ledger["consumed_token_hashes"].append(self.token_hash)
        self._persist()
        self._used = True

    def record(self, operation: dict[str, Any]) -> None:
        self._ledger["operations"].append(operation)
        self._ledger["operations"] = self._ledger["operations"][-20:]
        self._persist()

    def history(self) -> list[dict[str, Any]]:
        return list(self._ledger["operations"])

    @property
    def used(self) -> bool:
        return self._used

    async def run(self, io_lock: asyncio.Lock, preflight: Any = None) -> dict[str, Any]:
        if self._used:
            raise ExecutionBlocked("This one-time commissioning authorization has been consumed.")
        if self._lock.locked():
            raise ExecutionBlocked("A cooldown commissioning operation is already active.")
        await self._lock.acquire()
        acquired_io_lock = False
        transport: CommandTransport | None = None
        results: list[dict[str, Any]] = []
        dispatch_attempted = False
        authorization_consumed = False
        try:
            await asyncio.wait_for(io_lock.acquire(), timeout=self.timeout)
            acquired_io_lock = True
            if preflight is not None:
                _require_fresh_idle(preflight())
            async with asyncio.timeout(self.timeout):
                transport = self.transport_factory()
                await transport.connect()
                for command in ("M104 S0", "M140 S0"):
                    if preflight is not None:
                        _require_fresh_idle(preflight())
                    if not authorization_consumed:
                        # Consume once, immediately before the first possible dispatch.
                        self._consume_authorization()
                        authorization_consumed = True
                    dispatch_attempted = True
                    result = await transport.send(command)
                    if result.get("acknowledged") is not True:
                        raise CommandTransportError(
                            "Transport returned without a positive acknowledgment; "
                            "outcome is unknown.",
                            sent=True,
                        )
                    results.append(
                        {"command": command, "state": "transport_acknowledged", **result}
                    )
            return {"state": "transport_acknowledged", "results": results}
        except Exception as exc:
            sent_any = dispatch_attempted or bool(results) or getattr(exc, "sent", False)
            return {
                "state": "outcome_unknown" if sent_any else "failed",
                "results": results,
                "error": str(exc) or type(exc).__name__,
            }
        except asyncio.CancelledError:
            return {
                "state": "outcome_unknown" if dispatch_attempted else "failed",
                "results": results,
                "error": "Request cancelled; command outcome is unknown and was not retried."
                if dispatch_attempted
                else "Request cancelled before command dispatch.",
            }
        finally:
            if transport is not None:
                try:
                    await transport.close()
                except Exception:
                    pass
            if acquired_io_lock:
                io_lock.release()
            self._lock.release()


class CommandExecutor:
    """Serialized, bounded executor which never retries a sent command."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        verified_commands: frozenset[str] = frozenset(),
        verified_macros: frozenset[str] = frozenset(),
        transport_factory: Any = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        timeout: float = 15.0,
        extrusion_min_temperature_c: float | None = None,
        history_path: str | Path | None = None,
        capability_registry: CapabilityRegistry | None = None,
    ) -> None:
        if timeout <= 0 or timeout > 600:
            raise ValueError("command timeout must be between 0 and 600 seconds")
        unknown = verified_commands - KNOWN_COMMANDS
        if unknown:
            raise ValueError("unknown verified command(s): " + ", ".join(sorted(unknown)))
        unknown_macros = verified_macros - KNOWN_MACROS
        if unknown_macros:
            raise ValueError("unknown verified macro(s): " + ", ".join(sorted(unknown_macros)))
        if (
            extrusion_min_temperature_c is not None
            and not 150 <= extrusion_min_temperature_c <= 260
        ):
            raise ValueError("verified extrusion minimum must be in 150..260 °C")
        self.enabled = enabled
        self.verified_commands = verified_commands
        self.verified_macros = verified_macros
        self.capability_registry = capability_registry or CapabilityRegistry()
        self.transport_factory = transport_factory or (
            lambda: MksTcpCommandTransport(host, port, timeout)
        )
        self.host = host
        self.port = port
        self.timeout = timeout
        self.extrusion_min_temperature_c = extrusion_min_temperature_c
        self.operations: deque[Operation] = deque(maxlen=100)
        self.history_path = Path(history_path) if history_path else None
        self._archived_operations = self._read_history()
        self.active_id: str | None = None
        self._active_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.operator_mode_armed = False
        self.session_attestation: dict[str, bool] = {}
        self.homed_axes: set[str] = set()
        self.axis_positions_mm: dict[str, float | None] = {"x": None, "y": None, "z": None}
        self.axis_reference_observations: dict[str, dict[str, str]] = {}
        self.axis_limits_mm = {"x": 220.0, "y": 220.0, "z": 250.0}
        self.pending_axes: set[str] = set()

    def _read_history(self) -> deque[dict[str, Any]]:
        if self.history_path is None or not self.history_path.exists():
            return deque(maxlen=500)
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            rows = payload.get("operations", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                raise ValueError("operation history must be a list")
            return deque(
                (row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)),
                maxlen=500,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Could not read command operation history: {exc}") from exc

    def _persist_operation(self, operation: Operation) -> None:
        """Keep an atomic, bounded record; never restore live safety state from disk."""
        entry = operation.as_dict()
        for index, existing in enumerate(self._archived_operations):
            if existing.get("id") == operation.id:
                self._archived_operations[index] = entry
                break
        else:
            self._archived_operations.append(entry)
        self._write_history()

    def _write_history(self) -> None:
        if self.history_path is None:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"operations": list(self._archived_operations)}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.history_path)

    def record_external_homing_reference(self, axes: list[str]) -> dict[str, Any]:
        """Record an operator-reported home completed outside this service; sends no G-code."""
        selected = sorted(set(axes))
        if not selected or set(selected) - {"x", "y", "z"}:
            raise ValueError("Select one or more of X, Y, and Z.")
        if self.active_id is not None or self._active_lock.locked():
            raise ExecutionBlocked("Wait until the active machine operation is complete.")
        observed_at = datetime.now(UTC).isoformat()
        for axis in selected:
            self.pending_axes.discard(axis)
            self.homed_axes.add(axis)
            self.axis_positions_mm[axis] = 0.0
            self.axis_reference_observations[axis] = {
                "observed_at": observed_at,
                "kind": "operator_reported_external_homing_reference",
                "source": "stock_printer_touchscreen",
            }
        for operation in self.operations:
            if (
                operation.observation_required
                and set(operation.observed_axes).intersection(selected)
                and operation.operator_observation is None
            ):
                operation.observation_required = False
                operation.operator_observation = "operator_reported_external_tft_homing"
                operation.updated_at = observed_at
                self._persist_operation(operation)
        for index, item in enumerate(self._archived_operations):
            macro_axis = str(item.get("macro_id", "")).removeprefix("home_")
            if (
                macro_axis in selected
                and item.get("awaiting_operator_observation") is True
                and item.get("operator_observation") is None
            ):
                item["awaiting_operator_observation"] = False
                item["operator_observation"] = "operator_reported_external_tft_homing"
                item["updated_at"] = observed_at
                self._archived_operations[index] = item
        self._write_history()
        return {
            "axes": selected,
            "observed_at": observed_at,
            "source": "operator_reported_stock_printer_touchscreen",
            "commands_sent": [],
            "note": (
                "Operator-reported homing reference only. This action sent no command and "
                "does not verify a dashboard homing command or provide continuous position."
            ),
        }

    @property
    def control_configured(self) -> bool:
        return self.enabled and self.transport_factory is not None

    @property
    def execution_enabled(self) -> bool:
        return bool(self.control_configured and self.operator_mode_armed)

    def arm_session(
        self,
        *,
        attended: bool = False,
        power_switch_reachable: bool = False,
        no_competing_clients: bool = False,
    ) -> dict[str, Any]:
        if not self.control_configured:
            raise ExecutionBlocked("No machine operations are configured for this application.")
        if self.active_id is not None:
            raise ExecutionBlocked("Disarm is unavailable while an operation is active.")
        if not (attended and power_switch_reachable and no_competing_clients):
            raise ExecutionBlocked(
                "Confirm attendance, a reachable power switch, and no competing printer clients."
            )
        self.operator_mode_armed = True
        self.session_attestation = {
            "attended": True,
            "power_switch_reachable": True,
            "no_competing_clients": True,
        }
        return {
            "armed": True,
            "duration_seconds": None,
            "until": "operator_disarm_or_service_restart",
        }

    def disarm_session(self) -> None:
        if self.active_id is not None:
            raise ExecutionBlocked("Wait for the active operation to finish before disarming.")
        self.operator_mode_armed = False
        self.session_attestation.clear()

    def capability_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.execution_enabled,
            "configured": self.control_configured,
            "session_armed": self.execution_enabled,
            "session_seconds_remaining": None if self.execution_enabled else 0,
            "session_until": (
                "operator_disarm_or_service_restart" if self.execution_enabled else None
            ),
            "supported_fixed_command_families": sorted(KNOWN_COMMANDS),
            "commissioned_macros": [
                item["id"]
                for item in self.capability_registry.list()
                if item["status"] == "verified"
            ],
            "commissionable_macros": [
                item["id"] for item in self.capability_registry.list() if item["commissionable"]
            ],
            "transport": (
                "MKS raw TCP line protocol; command acknowledgments are not proof "
                "of physical completion"
            ),
            "blocked_reason": None
            if self.execution_enabled
            else (
                "Operator Mode is disarmed. Arm it with your existing dashboard sign-in; "
                "fixed operations remain subject to fresh safety checks and confirmation."
            ),
            "active_operation_id": self.active_id,
            "axes": {
                axis: {
                    "homed": axis in self.homed_axes,
                    "pending_observation": axis in self.pending_axes,
                    "position_mm": self.axis_positions_mm[axis],
                    "position_source": (
                        (
                            self.axis_reference_observations[axis].get("kind", "unavailable")
                            if axis in self.axis_reference_observations
                            else "operator_confirmed_relative_estimate"
                        )
                        if self.axis_positions_mm[axis] is not None
                        else "unavailable"
                    ),
                    "reference_observation": self.axis_reference_observations.get(axis),
                    "software_limits_mm": [0.0, self.axis_limits_mm[axis]],
                }
                for axis in ("x", "y", "z")
            },
            "states": [
                "queued",
                "running",
                "transport_acknowledged",
                "failed",
                "outcome_unknown",
                "cancelled_before_next_command",
            ],
        }

    def can_run_commands(
        self, commands: list[str], macro_id: str | None = None, *, commissioning: bool = False
    ) -> bool:
        ready_statuses = {"ready_to_confirm"}
        if commissioning:
            ready_statuses.add("ready_for_commissioning")
        return (
            self.operation_readiness(commands, macro_id, commissioning=commissioning)["status"]
            in ready_statuses
        )

    def operation_readiness(
        self,
        commands: list[str],
        macro_id: str | None = None,
        *,
        commissioning: bool = False,
    ) -> dict[str, str]:
        """Explain the server-side gate without equating readiness with execution."""
        if not self.enabled:
            return {"status": "not_enabled", "reason": "Server execution is disabled."}
        if not commands:
            return {"status": "not_implemented", "reason": "No fixed command sequence exists."}
        macro = next((item for item in MACROS if item.id == macro_id), None)
        if macro is None:
            return {"status": "not_implemented", "reason": "No fixed macro exists."}
        if macro.execution_block_reason:
            return {
                "status": "temporarily_unavailable",
                "reason": macro.execution_block_reason,
            }
        capability = self.capability_registry.get(macro_id)
        if commissioning:
            if not capability["commissionable"]:
                return {"status": capability["status"], "reason": capability["reason"]}
            if capability["status"] not in {"ready_for_commissioning", "failed"}:
                return {"status": capability["status"], "reason": capability["reason"]}
        unavailable = {_command_family(command) for command in commands} - KNOWN_COMMANDS
        if unavailable:
            return {
                "status": "unsupported",
                "reason": "Command family is outside the fixed executor.",
            }
        if macro_id and macro_id.startswith("jog_"):
            axis = macro_id.split("_")[1]
            if axis not in self.homed_axes or self.axis_positions_mm[axis] is None:
                return {
                    "status": "awaiting_homing",
                    "reason": (
                        f"Home {axis.upper()} to establish a firmware coordinate reference "
                        "before jogging."
                    ),
                }
        if any(command.startswith("G1 E") for command in commands):
            if self.extrusion_min_temperature_c is None:
                return {
                    "status": "temperature_gate_unconfigured",
                    "reason": "A verified minimum extrusion temperature is not configured.",
                }
        if not self.execution_enabled:
            return {"status": "disarmed", "reason": "Arm Operator Mode to enable guarded actions."}
        if not all(self.session_attestation.values()):
            return {
                "status": "operator_attestation_required",
                "reason": (
                    "Arm Operator Mode and confirm attendance, power access, and no "
                    "competing clients."
                ),
            }
        if commissioning:
            return {
                "status": "ready_for_commissioning",
                "reason": "Prerequisites pass; review and confirm one supervised test.",
            }
        return {"status": "ready_to_confirm", "reason": "Operator Mode safety checks pass."}

    def record_operator_observation(
        self,
        operation_id: str,
        observed_success: bool,
        telemetry_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = next((item for item in self.operations if item.id == operation_id), None)
        if operation is None:
            raise KeyError(operation_id)
        if operation.state != "transport_acknowledged" or not operation.observation_required:
            raise ExecutionBlocked("This operation is not awaiting a physical observation.")
        if operation.operator_observation is not None:
            raise ExecutionBlocked("An operator observation has already been recorded.")
        telemetry_evidence = self._validate_commissioning_evidence(
            operation, telemetry_snapshot, observed_success
        )
        operation.updated_at = datetime.now(UTC).isoformat()
        for affected_axis in operation.observed_axes:
            self.pending_axes.discard(affected_axis)
        axis = operation.observed_axes[0] if len(operation.observed_axes) == 1 else None
        if observed_success:
            operation.physical_completion_confirmed = True
            operation.operator_observation = "operator_observed_expected_result"
            if operation.macro_id.startswith("home_"):
                for homed_axis in operation.observed_axes:
                    self.homed_axes.add(homed_axis)
                    self.axis_positions_mm[homed_axis] = 0.0
                    self.axis_reference_observations[homed_axis] = {
                        "operation_id": operation.id,
                        "macro_id": operation.macro_id,
                        "observed_at": operation.updated_at,
                        "kind": "operator_observed_homing_reference",
                    }
            elif operation.macro_id.startswith("jog_") and axis is not None:
                self.homed_axes.add(axis)
                self.axis_positions_mm[axis] = operation.proposed_position_mm
                reference = self.axis_reference_observations.get(axis)
                if reference is not None:
                    reference["observed_at"] = operation.updated_at
            self.capability_registry.verify(
                operation.macro_id,
                operation.as_dict(),
                {
                    "operator_observation": operation.operator_observation,
                    "observed_at": operation.updated_at,
                    "operating_mode": "commissioning" if operation.commissioning else "operator",
                    **telemetry_evidence,
                },
            )
        else:
            operation.operator_observation = "operator_reported_unexpected_or_no_motion"
            for affected_axis in operation.observed_axes:
                self.homed_axes.discard(affected_axis)
                self.axis_positions_mm[affected_axis] = None
                self.axis_reference_observations.pop(affected_axis, None)
            self.capability_registry.fail(
                operation.macro_id,
                operation.as_dict(),
                "Operator reported an unexpected or unsuccessful physical result.",
                unknown=False,
            )
        self._persist_operation(operation)
        return operation.as_dict()

    @staticmethod
    def _validate_commissioning_evidence(
        operation: Operation,
        snapshot: dict[str, Any] | None,
        observed_success: bool,
    ) -> dict[str, Any]:
        if not observed_success or not operation.commissioning:
            return {}
        expected: dict[str, float] = {}
        waits_for_temperature = False
        for command in operation.commands:
            match = re.match(r"^M(104|140|109|190)\s+S([0-9]+(?:\.[0-9]+)?)$", command)
            if not match:
                continue
            waits_for_temperature = waits_for_temperature or match.group(1) in {"109", "190"}
            heater = "nozzle_temperature" if match.group(1) in {"104", "109"} else "bed_temperature"
            expected[heater] = float(match.group(2))
        if not expected:
            return {}
        if (
            not snapshot
            or snapshot.get("stale")
            or not snapshot.get("connection", {}).get("connected")
            or snapshot.get("data_age_seconds") is None
            or snapshot["data_age_seconds"] > 5
        ):
            raise ExecutionBlocked(
                "Fresh post-command telemetry is required to verify this temperature test."
            )
        observed_at = snapshot.get("observed_at")
        if not observed_at or observed_at <= operation.updated_at:
            raise ExecutionBlocked(
                "Wait for a temperature sample captured after the command acknowledgment."
            )
        telemetry = snapshot.get("telemetry") or {}
        before = operation.telemetry_before or {}
        evidence: dict[str, Any] = {"telemetry_observed_at": observed_at, "targets": {}}
        for heater, target in expected.items():
            current = telemetry.get(heater) or {}
            previous = before.get(heater) or {}
            current_target = current.get("target_c")
            previous_target = previous.get("target_c")
            if current_target is None or abs(float(current_target) - target) > 0.5:
                raise ExecutionBlocked(
                    f"Fresh {heater.replace('_', ' ')} telemetry does not show the "
                    f"requested {target:g} °C target."
                )
            if previous_target is not None and abs(float(previous_target) - target) <= 0.5:
                raise ExecutionBlocked(
                    "The target was already at the requested value before this test; "
                    "this operation has not demonstrated a target change."
                )
            actual = current.get("value")
            if waits_for_temperature and (actual is None or float(actual) < target - 2):
                raise ExecutionBlocked(
                    f"Fresh {heater.replace('_', ' ')} telemetry has not reached "
                    "the requested target yet."
                )
            evidence["targets"][heater] = {
                "before_c": previous_target,
                "after_c": current_target,
                "requested_c": target,
                "actual_c": actual,
            }
        return evidence

    def history(self) -> list[dict[str, Any]]:
        rows = {item["id"]: item for item in self._archived_operations}
        rows.update({operation.id: operation.as_dict() for operation in self.operations})
        return list(rows.values())

    def get(self, operation_id: str) -> dict[str, Any] | None:
        for operation in reversed(self.operations):
            if operation.id == operation_id:
                return operation.as_dict()
        return None

    async def start(
        self,
        *,
        preview_service: DryRunCommandService,
        macro_id: str,
        parameters: dict[str, Any],
        confirmation: str | None,
        snapshot: dict[str, Any],
        io_lock: asyncio.Lock,
        snapshot_provider: Any = None,
        commissioning: bool = False,
        axis_at_home: bool = False,
        retreat_from_home: bool = False,
    ) -> dict[str, Any]:
        if not self.execution_enabled:
            raise ExecutionBlocked("Physical command execution is disabled.")
        if self.active_id is not None or self._active_lock.locked():
            raise ExecutionBlocked("Another machine operation is already active.")
        await self._active_lock.acquire()
        try:
            macro = next((item for item in MACROS if item.id == macro_id), None)
            if macro is None:
                raise PreviewError("Unknown macro. Choose an item from the fixed macro library.")
            if macro.execution_block_reason:
                raise ExecutionBlocked(macro.execution_block_reason)
            capability = self.capability_registry.get(macro_id)
            if commissioning:
                if not capability["commissionable"] or capability["status"] not in {
                    "ready_for_commissioning",
                    "failed",
                }:
                    raise ExecutionBlocked(capability["reason"])
            preview = preview_service.preview(
                macro_id, parameters, retreat_from_home=retreat_from_home
            )
            if not preview["commands"]:
                raise ExecutionBlocked(
                    "This item is a guided workflow and has no command executor."
                )
            if confirmation != macro.name:
                raise ExecutionBlocked(f"Type the exact operation name to confirm: {macro.name}")
            if not all(self.session_attestation.values()):
                raise ExecutionBlocked(
                    "Arm Operator Mode and confirm attendance, power access, and no "
                    "competing clients."
                )
            if retreat_from_home and not axis_at_home:
                raise ExecutionBlocked(
                    "Confirm the axes are at their home switches before retreat."
                )
            expected_state = _required_printer_state(macro_id)
            _require_fresh_state(snapshot, expected_state)
            state = snapshot.get("telemetry") or {}
            if macro_id == "neptune_3_axis_orbital_dance":
                _require_dance_cool_telemetry(state)
            commands = preview["commands"]
            used = {_command_family(command) for command in commands}
            unavailable_commands = used - KNOWN_COMMANDS
            if unavailable_commands:
                raise ExecutionBlocked(
                    "Command family is outside the fixed executor: "
                    + ", ".join(sorted(unavailable_commands))
                )
            if any(command.startswith("G1 E") for command in commands):
                nozzle = (state.get("nozzle_temperature") or {}).get("value")
                minimum = self.extrusion_min_temperature_c
                if minimum is None:
                    raise ExecutionBlocked(
                        "A verified minimum extrusion temperature is not configured."
                    )
                if nozzle is None or nozzle < minimum:
                    raise ExecutionBlocked(
                        "Extrusion is blocked until the observed nozzle temperature "
                        f"reaches {minimum:g} °C."
                    )
            observed_axes: list[str] = []
            proposed_position: float | None = None
            planned_axis: str | None = None
            planned_start: float | None = None
            planned_distance: float | None = None
            estimated_duration: float | None = None
            if macro_id.startswith("home_") or macro_id == "neptune_3_axis_orbital_dance":
                observed_axes = (
                    [macro_id.removeprefix("home_")]
                    if macro_id.startswith("home_") and macro_id != "home_axes"
                    else ["x", "y", "z"]
                )
                for axis in observed_axes:
                    if commissioning and axis in self.pending_axes:
                        raise ExecutionBlocked(
                            f"{axis.upper()} has an operation awaiting operator observation."
                        )
                    self.homed_axes.discard(axis)
                    self.axis_positions_mm[axis] = None
            elif macro_id.startswith("jog_"):
                _, axis, _direction = macro_id.split("_")
                if commissioning and axis in self.pending_axes:
                    raise ExecutionBlocked(
                        f"{axis.upper()} has an operation awaiting operator observation."
                    )
                current = self.axis_positions_mm[axis]
                if axis not in self.homed_axes or current is None:
                    raise ExecutionBlocked(
                        f"{axis.upper()} jogging requires a recent home command or an "
                        "operator-reported home reference. Home the axis first."
                    )
                step = float(parameters["distance_mm"])
                proposed_position = current + step
                planned_axis = axis
                planned_start = current
                planned_distance = abs(step)
                feed = next(
                    (
                        float(match.group(1))
                        for command in commands
                        if (match := re.search(r"\bF([0-9]+(?:\.[0-9]+)?)\b", command))
                    ),
                    None,
                )
                if feed and feed > 0:
                    estimated_duration = planned_distance / (feed / 60.0)
                if not 0 <= proposed_position <= self.axis_limits_mm[axis]:
                    raise ExecutionBlocked(
                        f"Jog would place {axis.upper()} outside the conservative "
                        f"0..{self.axis_limits_mm[axis]:g} mm software boundary."
                    )
                observed_axes = [axis]
            operation = Operation(
                id=str(uuid.uuid4()),
                macro_id=macro_id,
                state="queued",
                created_at=datetime.now(UTC).isoformat(),
                updated_at=datetime.now(UTC).isoformat(),
                commands=commands,
                telemetry_before=state,
                observed_axes=observed_axes,
                observation_required=commissioning,
                commissioning=commissioning,
                mode="commissioning" if commissioning else "operator",
                parameters=dict(parameters),
                proposed_position_mm=proposed_position,
                planned_axis=planned_axis,
                planned_start_position_mm=planned_start,
                planned_distance_mm=planned_distance,
                estimated_duration_seconds=estimated_duration,
            )
            if commissioning:
                try:
                    self.capability_registry.begin(macro_id, operation.id, commands)
                except ValueError as exc:
                    raise ExecutionBlocked(str(exc)) from exc
            self.operations.append(operation)
            self._persist_operation(operation)
            if operation.observation_required:
                self.pending_axes.update(observed_axes)
            self.active_id = operation.id
            task = asyncio.create_task(self._run(operation, io_lock, snapshot, snapshot_provider))
            self._tasks[operation.id] = task
            task.add_done_callback(lambda _task, key=operation.id: self._tasks.pop(key, None))
            return operation.as_dict()
        except Exception:
            self._active_lock.release()
            raise

    async def cancel(self, operation_id: str) -> dict[str, Any]:
        operation = next((item for item in self.operations if item.id == operation_id), None)
        if operation is None:
            raise KeyError(operation_id)
        if operation.state in {"queued", "running"}:
            operation.cancel_requested = True
            operation.updated_at = datetime.now(UTC).isoformat()
            self._persist_operation(operation)
            return operation.as_dict()
        return operation.as_dict()

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(
        self,
        operation: Operation,
        io_lock: asyncio.Lock,
        initial_snapshot: dict[str, Any],
        snapshot_provider: Any = None,
    ) -> None:
        transport: CommandTransport | None = None
        any_sent = False
        try:
            await asyncio.wait_for(io_lock.acquire(), timeout=self.timeout)
            try:
                if operation.cancel_requested:
                    operation.state = "cancelled_before_next_command"
                    return
                current_snapshot = (
                    snapshot_provider() if snapshot_provider is not None else initial_snapshot
                )
                expected_state = _required_printer_state(operation.macro_id)
                _require_fresh_state(current_snapshot, expected_state)
                operation.state = "running"
                operation.updated_at = datetime.now(UTC).isoformat()
                self._persist_operation(operation)
                transport = self.transport_factory()
                await asyncio.wait_for(transport.connect(), timeout=self.timeout)
                for index, command in enumerate(operation.commands):
                    if operation.cancel_requested:
                        operation.state = "cancelled_before_next_command"
                        break
                    if snapshot_provider is not None:
                        current_snapshot = snapshot_provider()
                        _require_fresh_state(current_snapshot, expected_state)
                        if operation.macro_id == "neptune_3_axis_orbital_dance":
                            _require_dance_cool_telemetry(current_snapshot.get("telemetry") or {})
                    dispatched_at = datetime.now(UTC).isoformat()
                    operation.command_dispatched = True
                    operation.last_dispatched_at = dispatched_at
                    self._persist_operation(operation)
                    try:
                        result = await asyncio.wait_for(
                            transport.send(command), timeout=self.timeout
                        )
                        if result.get("acknowledged") is not True:
                            raise CommandTransportError(
                                "Transport returned without a positive acknowledgment; "
                                "outcome is unknown.",
                                sent=True,
                            )
                        any_sent = True
                        operation.command_results.append(
                            {
                                "index": index,
                                "command": command,
                                "state": "transport_acknowledged",
                                "dispatched_at": dispatched_at,
                                "acknowledged_at": datetime.now(UTC).isoformat(),
                                "verification_confidence": "transport_acknowledgment_only",
                                **result,
                            }
                        )
                        self._persist_operation(operation)
                    except CommandTransportError as exc:
                        any_sent = any_sent or exc.sent
                        operation.command_results.append(
                            {
                                "index": index,
                                "command": command,
                                "state": "outcome_unknown" if exc.sent else "not_sent",
                                "dispatched_at": dispatched_at if exc.sent else None,
                                "acknowledged_at": None,
                                "verification_confidence": "outcome_unknown"
                                if exc.sent
                                else "not_sent",
                                "error": str(exc),
                            }
                        )
                        self._persist_operation(operation)
                        operation.state = "outcome_unknown" if exc.sent else "failed"
                        operation.error = str(exc)
                        break
                    except TimeoutError:
                        any_sent = True
                        operation.command_results.append(
                            {
                                "index": index,
                                "command": command,
                                "state": "outcome_unknown",
                                "dispatched_at": dispatched_at,
                                "acknowledged_at": None,
                                "verification_confidence": "outcome_unknown",
                                "error": "Command timed out; it will not be retried.",
                            }
                        )
                        self._persist_operation(operation)
                        operation.state = "outcome_unknown"
                        operation.error = (
                            "Command timed out; its outcome is unknown and it will not be retried."
                        )
                        break
                    except Exception as exc:
                        any_sent = True
                        operation.command_results.append(
                            {
                                "index": index,
                                "command": command,
                                "state": "outcome_unknown",
                                "dispatched_at": dispatched_at,
                                "acknowledged_at": None,
                                "verification_confidence": "outcome_unknown",
                                "error": str(exc) or type(exc).__name__,
                            }
                        )
                        self._persist_operation(operation)
                        operation.state = "outcome_unknown"
                        operation.error = (
                            "Transport failed after dispatch began; outcome is unknown "
                            "and the command will not be retried."
                        )
                        break
                else:
                    operation.transport_acknowledged = True
                    operation.state = "transport_acknowledged"
                    operation.error = None
                    if not operation.commissioning and (
                        operation.macro_id.startswith("home_")
                        or operation.macro_id == "neptune_3_axis_orbital_dance"
                    ):
                        for axis in operation.observed_axes:
                            self.homed_axes.add(axis)
                            self.axis_positions_mm[axis] = 0.0
                            self.axis_reference_observations[axis] = {
                                "operation_id": operation.id,
                                "macro_id": operation.macro_id,
                                "observed_at": operation.updated_at,
                                "kind": "firmware_homing_acknowledgment_estimate",
                            }
                    elif (
                        not operation.commissioning
                        and operation.macro_id.startswith("jog_")
                        and operation.planned_axis is not None
                    ):
                        self.axis_positions_mm[operation.planned_axis] = (
                            operation.proposed_position_mm
                        )
                        self.axis_reference_observations[operation.planned_axis] = {
                            "operation_id": operation.id,
                            "macro_id": operation.macro_id,
                            "observed_at": operation.updated_at,
                            "kind": "commanded_position_estimate",
                        }
            finally:
                io_lock.release()
        except asyncio.CancelledError:
            if any_sent:
                operation.state = "outcome_unknown"
                operation.error = (
                    "Execution was interrupted after dispatch; the outcome is unknown."
                )
            else:
                operation.state = "cancelled_before_next_command"
            raise
        except Exception as exc:
            operation.state = "outcome_unknown" if any_sent else "failed"
            operation.error = str(exc) or type(exc).__name__
        finally:
            if transport is not None:
                try:
                    await transport.close()
                except Exception:
                    pass
            operation.telemetry_after = None
            if operation.observed_axes and operation.state == "outcome_unknown":
                for axis in operation.observed_axes:
                    self.pending_axes.discard(axis)
                    self.homed_axes.discard(axis)
                    self.axis_positions_mm[axis] = None
                    self.axis_reference_observations.pop(axis, None)
            operation.updated_at = datetime.now(UTC).isoformat()
            if operation.commissioning and operation.state != "transport_acknowledged":
                try:
                    self.capability_registry.fail(
                        operation.macro_id,
                        operation.as_dict(),
                        operation.error
                        or "Commissioning operation did not receive a complete acknowledgment.",
                        unknown=operation.state == "outcome_unknown",
                    )
                except KeyError:
                    pass
            if self.active_id == operation.id:
                self.active_id = None
            self._persist_operation(operation)
            if self._active_lock.locked():
                self._active_lock.release()


def _required_printer_state(macro_id: str) -> str:
    if macro_id == "pause_print":
        return "printing"
    if macro_id == "resume_print":
        return "paused"
    if macro_id == "cancel_print":
        return "active_print"
    return "idle"


def _require_fresh_state(snapshot: dict[str, Any], expected_state: str) -> None:
    state = snapshot.get("telemetry") or {}
    state_name = (state.get("printer_state") or {}).get("value")
    age = snapshot.get("data_age_seconds")
    if (
        not snapshot.get("connection", {}).get("connected")
        or snapshot.get("stale")
        or age is None
        or age > 5.0
    ):
        raise ExecutionBlocked("Fresh printer telemetry is required before machine control.")
    if expected_state == "active_print":
        active = (state.get("print_active") or {}).get("value") is True
        if state_name not in {"printing", "paused"} or not active:
            raise ExecutionBlocked("An active or paused print is required for this operation.")
        return
    if state_name != expected_state:
        raise ExecutionBlocked(f"The printer must be freshly observed {expected_state}.")
    if expected_state == "idle" and (state.get("print_active") or {}).get("value") is not False:
        raise ExecutionBlocked("The printer must be freshly observed idle with no active print.")


def _require_fresh_idle(snapshot: dict[str, Any]) -> None:
    _require_fresh_state(snapshot, "idle")


def _require_dance_cool_telemetry(telemetry: dict[str, Any]) -> None:
    for telemetry_key, label in (("nozzle_temperature", "Nozzle"), ("bed_temperature", "Bed")):
        thermal = telemetry.get(telemetry_key) or {}
        actual, target = thermal.get("value"), thermal.get("target_c")
        if actual is None or target is None:
            raise ExecutionBlocked(
                f"Fresh {label.lower()} temperature and target telemetry are required."
            )
        if float(actual) > 50 or float(target) > 0.5:
            raise ExecutionBlocked(f"{label} must be at or below 50 °C with its heater target off.")


def _command_family(command: str) -> str:
    return command.split(maxsplit=1)[0]
