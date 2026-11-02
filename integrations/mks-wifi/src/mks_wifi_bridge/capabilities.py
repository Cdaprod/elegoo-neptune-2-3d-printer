"""Persistent per-operation commissioning and verification registry."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .commands import MACROS

CAPABILITY_STATES = frozenset(
    {
        "unsupported",
        "implemented",
        "ready_for_commissioning",
        "commissioning_in_progress",
        "verified",
        "failed",
        "temporarily_blocked",
    }
)
HISTORY_LIMIT = 200


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CapabilityRegistry:
    """Persist operation evidence without restoring runtime machine references."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._items = self._load()
        self._ensure_catalog()
        self._recover_interrupted_tests()
        self._persist()

    def _defaults(self) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        for macro in MACROS:
            if macro.compatibility == "unknown":
                status = "temporarily_blocked"
                reason = "Safe command sequence and physical location are not defined."
            elif macro.compatibility == "workflow_only" or not macro.templates:
                status = "implemented"
                reason = "Manual workflow only; it has no command sequence to commission."
            else:
                status = "ready_for_commissioning"
                reason = "Fixed, validated command sequence is ready for supervised commissioning."
            evidence: list[dict[str, Any]] = []
            if macro.id == "home_x":
                status = "verified"
                reason = "Previously operator-verified; current X home reference is runtime-only."
                evidence.append(
                    {
                        "at": "historical",
                        "kind": "operator_observed_success",
                        "operation": "G28 X",
                        "detail": (
                            "The operator observed the X carriage reach its reference switch "
                            "and stop normally after a dashboard G28 X."
                        ),
                    }
                )
            items[macro.id] = {
                "status": status,
                "reason": reason,
                "commissionable": bool(macro.templates) and macro.compatibility != "unknown",
                "history": [
                    {
                        "at": _now(),
                        "from": None,
                        "to": status,
                        "evidence": "catalog_initialization",
                    }
                ],
                "evidence": evidence,
            }
        items["sd_upload"] = {
            "status": "temporarily_blocked",
            "reason": (
                "The installed MKS firmware's upload endpoint and post-transfer integrity "
                "check are not verified. Upload remains disabled by a separate server gate."
            ),
            "commissionable": False,
            "history": [
                {
                    "at": _now(),
                    "from": None,
                    "to": "temporarily_blocked",
                    "evidence": "protocol_compatibility_not_verified",
                }
            ],
            "evidence": [],
        }
        return items

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return self._defaults()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("capabilities") if isinstance(payload, dict) else None
            if not isinstance(rows, dict):
                raise ValueError("capabilities must be an object")
            result: dict[str, dict[str, Any]] = {}
            for key, item in rows.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(item, dict)
                    or item.get("status") not in CAPABILITY_STATES
                    or not isinstance(item.get("history"), list)
                    or not isinstance(item.get("evidence"), list)
                ):
                    raise ValueError("capability entry is malformed")
                result[key] = {
                    **item,
                    "history": item["history"][-HISTORY_LIMIT:],
                    "evidence": item["evidence"][-HISTORY_LIMIT:],
                }
            return result
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Could not read capability registry: {exc}") from exc

    def _ensure_catalog(self) -> None:
        defaults = self._defaults()
        for key, value in defaults.items():
            self._items.setdefault(key, value)

    def _recover_interrupted_tests(self) -> None:
        for key, item in self._items.items():
            if item["status"] == "commissioning_in_progress":
                self._transition(
                    key,
                    "temporarily_blocked",
                    "Process restarted during a commissioning test; outcome is unknown. "
                    "Reconcile the printer locally before retrying.",
                    {"kind": "interrupted_test_recovery", "automatic_retry": False},
                )

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "capabilities": self._items}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _transition(
        self,
        capability_id: str,
        status: str,
        reason: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in CAPABILITY_STATES:
            raise ValueError("unknown capability state")
        item = self._items.get(capability_id)
        if item is None:
            raise KeyError(capability_id)
        previous = item["status"]
        item["status"] = status
        item["reason"] = reason
        item["updated_at"] = _now()
        item.setdefault("history", []).append(
            {"at": item["updated_at"], "from": previous, "to": status, "evidence": evidence}
        )
        item["history"] = item["history"][-HISTORY_LIMIT:]
        if evidence.get("kind") not in {"catalog_initialization", "retry_authorized"}:
            item.setdefault("evidence", []).append({"at": item["updated_at"], **evidence})
            item["evidence"] = item["evidence"][-HISTORY_LIMIT:]
        self._persist()
        return self.get(capability_id)

    def get(self, capability_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.get(capability_id)
            if item is None:
                raise KeyError(capability_id)
            return json.loads(json.dumps({"id": capability_id, **item}))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.get(key) for key in self._items]

    def begin(self, capability_id: str, operation_id: str, commands: list[str]) -> dict[str, Any]:
        with self._lock:
            item = self.get(capability_id)
            if not item["commissionable"]:
                raise ValueError(item["reason"])
            if item["status"] not in {"ready_for_commissioning", "failed"}:
                raise ValueError(f"Capability cannot enter commissioning from {item['status']}.")
            return self._transition(
                capability_id,
                "commissioning_in_progress",
                "Supervised test dispatched; waiting for operation evidence.",
                {
                    "kind": "commissioning_started",
                    "operation_id": operation_id,
                    "commands": commands,
                },
            )

    def verify(
        self, capability_id: str, operation: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            item = self.get(capability_id)
            if not item["commissionable"] or item["status"] not in {
                "ready_for_commissioning",
                "commissioning_in_progress",
                "failed",
                "verified",
            }:
                return item
            return self._transition(
                capability_id,
                "verified",
                "Independent operator evidence was recorded; guarded execution is available.",
                {
                    "kind": "operator_verified_success",
                    "operation_id": operation.get("id"),
                    "macro_id": operation.get("macro_id"),
                    "commands": operation.get("commands", []),
                    "command_results": operation.get("command_results", []),
                    **evidence,
                },
            )

    def fail(
        self, capability_id: str, operation: dict[str, Any], reason: str, *, unknown: bool
    ) -> dict[str, Any]:
        with self._lock:
            item = self.get(capability_id)
            if not item["commissionable"] or item["status"] not in {
                "ready_for_commissioning",
                "commissioning_in_progress",
                "failed",
            }:
                return item
            status = "temporarily_blocked" if unknown else "failed"
            return self._transition(
                capability_id,
                status,
                reason,
                {
                    "kind": "commissioning_outcome_unknown" if unknown else "commissioning_failed",
                    "operation_id": operation.get("id"),
                    "commands": operation.get("commands", []),
                    "command_results": operation.get("command_results", []),
                    "error": operation.get("error"),
                },
            )

    def safe_to_retry(self, capability_id: str, confirmation: str) -> dict[str, Any]:
        with self._lock:
            item = self.get(capability_id)
            if not item["commissionable"]:
                raise ValueError(item["reason"])
            if item["status"] not in {"failed", "temporarily_blocked"}:
                raise ValueError("Only a failed or blocked test can be reset for a new attempt.")
            if confirmation != capability_id:
                raise ValueError(f"Type the exact capability id to confirm: {capability_id}")
            return self._transition(
                capability_id,
                "ready_for_commissioning",
                "Operator reconciled the printer and explicitly allowed a new supervised test.",
                {"kind": "retry_authorized", "confirmation": confirmation},
            )
