"""Server-side, operator-confirmed manual workflows (no printer transport)."""

from __future__ import annotations

import secrets
from collections import deque
from datetime import UTC, datetime
from typing import Any

from .commands import MACROS

MAX_WORKFLOW_HISTORY = 50
MAX_TELEMETRY_AGE_SECONDS = 5.0

# Fixed machine actions that may be launched from the matching guided step.
# Each still passes the normal executor allowlist and safety checks.
WORKFLOW_OPERATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "preheat_pla",
        "preheat_petg",
        "set_nozzle_temperature",
        "set_bed_temperature",
        "nozzle_preparation",
    ),
    3: ("filament_load", "filament_unload", "purge_nozzle"),
}


class WorkflowError(ValueError):
    """A workflow cannot be started or advanced safely."""


class GuidedWorkflowManager:
    """Hold one resumable manual checklist and require fresh state per step."""

    def __init__(self) -> None:
        self.active: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=MAX_WORKFLOW_HISTORY)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _validate_snapshot(snapshot: dict[str, Any], minimum_nozzle_c: float) -> None:
        age = snapshot.get("data_age_seconds")
        telemetry = snapshot.get("telemetry") or {}
        state = (telemetry.get("printer_state") or {}).get("value")
        if not snapshot.get("connection", {}).get("connected"):
            raise WorkflowError("Printer connection is unavailable; workflow remains paused.")
        if snapshot.get("stale") or age is None or age > MAX_TELEMETRY_AGE_SECONDS:
            raise WorkflowError("Printer telemetry is stale; wait for a fresh successful poll.")
        if state != "idle" or telemetry.get("print_active") is True:
            raise WorkflowError("Printer must be freshly observed as idle with no active print.")
        if minimum_nozzle_c > 0:
            nozzle = telemetry.get("nozzle_temperature") or {}
            current = nozzle.get("value")
            target = nozzle.get("target_c")
            if current is None or current < minimum_nozzle_c:
                raise WorkflowError(
                    f"Nozzle must be at least {minimum_nozzle_c:g} °C before this step."
                )
            if target is None or target < minimum_nozzle_c:
                raise WorkflowError("Nozzle target does not meet the selected minimum temperature.")

    def start(
        self,
        macro_id: str,
        parameters: dict[str, Any],
        snapshot: dict[str, Any],
        command_operation_active: bool,
    ) -> dict[str, Any]:
        macro = next((item for item in MACROS if item.id == macro_id), None)
        if macro is None or not macro.workflow or macro.templates:
            raise WorkflowError("Only registered manual workflows can start a guided session.")
        if self.active and self.active["state"] == "awaiting_operator_confirmation":
            raise WorkflowError("Another guided workflow is already active.")
        if command_operation_active:
            raise WorkflowError("A machine command operation is active.")
        minimum = parameters.get("minimum_nozzle_temperature_c", 190)
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise WorkflowError("Minimum nozzle temperature must be a number.")
        if not 150 <= minimum <= 260:
            raise WorkflowError("Minimum nozzle temperature must be between 150 and 260 °C.")
        # Allow the session to begin before preheating from the dashboard. The
        # threshold is enforced before every step confirmation.
        self._validate_snapshot(snapshot, 0)
        now = self._now()
        self.active = {
            "id": secrets.token_urlsafe(18),
            "macro_id": macro.id,
            "name": macro.name,
            "state": "awaiting_operator_confirmation",
            "step_index": 0,
            "steps": list(macro.workflow),
            "minimum_nozzle_temperature_c": float(minimum),
            "step_token": secrets.token_urlsafe(18),
            "created_at": now,
            "updated_at": now,
            "last_error": None,
            "notice": "Manual checklist only. This session sends no printer commands.",
        }
        return self.public_state(self.active)

    @staticmethod
    def public_state(session: dict[str, Any] | None) -> dict[str, Any] | None:
        if session is None:
            return None
        visible = {key: value for key, value in session.items() if key != "step_token"}
        visible["current_step"] = (
            session["steps"][session["step_index"]]
            if session["state"] == "awaiting_operator_confirmation"
            else None
        )
        # The step token is an opaque replay guard, not an authorization secret.
        visible["step_token"] = session.get("step_token")
        visible["available_operations"] = list(
            WORKFLOW_OPERATIONS.get(session["step_index"], ())
            if session["state"] == "awaiting_operator_confirmation"
            else ()
        )
        return visible

    def allows_operation(self, macro_id: str) -> bool:
        """Check whether a fixed macro is part of the current guide step."""
        session = self.active
        return bool(
            session
            and session["state"] == "awaiting_operator_confirmation"
            and macro_id in WORKFLOW_OPERATIONS.get(session["step_index"], ())
        )

    def confirm(
        self,
        workflow_id: str,
        step_token: str,
        confirmed: bool,
        snapshot: dict[str, Any],
        command_operation_active: bool,
    ) -> dict[str, Any]:
        session = self.active
        if not session or session["id"] != workflow_id:
            raise KeyError("Workflow not found")
        if session["state"] != "awaiting_operator_confirmation":
            raise WorkflowError("Workflow is not waiting for operator confirmation.")
        if not confirmed:
            raise WorkflowError("Explicit operator confirmation is required.")
        if not secrets.compare_digest(step_token, session["step_token"]):
            raise WorkflowError("This step confirmation is stale; refresh the workflow state.")
        if command_operation_active:
            raise WorkflowError(
                "A machine command operation became active; workflow remains paused."
            )
        try:
            # Enforce heat only while confirming the explicit wait-for-target
            # step (index 2), not during initial idle checks or material review.
            minimum = session["minimum_nozzle_temperature_c"] if session["step_index"] == 2 else 0
            self._validate_snapshot(snapshot, minimum)
        except WorkflowError as exc:
            session["last_error"] = str(exc)
            session["updated_at"] = self._now()
            raise
        session["step_index"] += 1
        session["last_error"] = None
        session["updated_at"] = self._now()
        if session["step_index"] == len(session["steps"]):
            session["state"] = "completed"
            session["step_token"] = None
            self.history.append(self.public_state(session))
            self.active = None
            return self.public_state(session)
        session["step_token"] = secrets.token_urlsafe(18)
        return self.public_state(session)

    def cancel(self, workflow_id: str) -> dict[str, Any]:
        session = self.active
        if not session or session["id"] != workflow_id:
            raise KeyError("Workflow not found")
        session["state"] = "cancelled"
        session["step_token"] = None
        session["updated_at"] = self._now()
        result = self.public_state(session)
        self.history.append(result)
        self.active = None
        return result

    def payload(self) -> dict[str, Any]:
        return {
            "active_workflow": self.public_state(self.active),
            "history": list(self.history),
        }
