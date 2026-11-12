"""Neptune 2 telemetry dashboard with a separate default-disabled executor."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mkswifi import MksWifiClient, MksWifiConnectionError
from pydantic import BaseModel, ConfigDict, Field

from .capabilities import CapabilityRegistry
from .client import DEFAULT_HOST, DEFAULT_PORT, list_sd_root
from .commands import CAPABILITIES, DryRunCommandService, PreviewError
from .executor import (
    KNOWN_COMMANDS,
    KNOWN_MACROS,
    CommandExecutor,
    CooldownCommissioningExecutor,
    ExecutionBlocked,
    MksTcpCommandTransport,
    _require_fresh_state,
    _required_printer_state,
)
from .gcode_thumbnail import extract_gcode_thumbnail
from .jobs import print_job_summary
from .local_gcode_index import LocalGcodeIndexer
from .mks_http_status import MksHttpStatusError, MksHttpStatusReader
from .mks_upload import MAX_UPLOAD_BYTES, UploadError, upload_to_mks, validate_upload
from .motion import AXES, build_motion_sample, query_m114
from .motion_timeline import MotionTimeline
from .octoprint_backend import OctoPrintBackend, OctoPrintError
from .protocol import READ_ONLY_COMMANDS, telemetry_payload
from .workflows import GuidedWorkflowManager, WorkflowError

MAX_HISTORY = 1800
MAX_BACKOFF_SECONDS = 30.0
FIRMWARE_REFRESH_SECONDS = 300.0
SESSION_COOKIE = "neptune_observatory_session"
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class TemperatureValue(BaseModel):
    status: str
    value: float | None = None
    target_c: float | None = None
    unit: str = "°C"


class ConnectionInfo(BaseModel):
    connected: bool
    host: str
    port: int
    transport: str = "raw TCP"


class StatusResponse(BaseModel):
    connection: ConnectionInfo
    telemetry: dict[str, Any] | None
    observed_at: str | None
    last_attempt_at: str | None
    data_age_seconds: float | None
    stale: bool
    poll_interval_seconds: float
    poll_latency_ms: float | None = None
    last_error: str | None
    firmware_identification: str | None
    printer_backend: str = "mks"
    motion: dict[str, Any]
    command_activity: dict[str, Any] = Field(default_factory=dict)
    timeline: dict[str, Any] = Field(default_factory=dict)
    print_job: dict[str, Any] = Field(default_factory=dict)


class TemperatureResponse(BaseModel):
    connection: ConnectionInfo
    observed_at: str | None
    data_age_seconds: float | None
    stale: bool
    nozzle: TemperatureValue
    bed: TemperatureValue
    history: list[dict[str, Any]]


class FirmwareResponse(BaseModel):
    status: str
    value: str | None
    observed_at: str | None


class SdEntry(BaseModel):
    name: str
    kind: str
    size_bytes: int | None = None
    preview_available: bool = False
    ambiguous: bool = False
    preview_source: str = "none"
    local_preview: bool = False
    local_match: str = "unmatched"
    local_metadata: dict[str, Any] | None = None


class SdCardResponse(BaseModel):
    status: str
    path: str = "/"
    entries: list[SdEntry]
    refreshed_at: str | None
    error: str | None = None
    local_index: dict[str, Any] = Field(default_factory=dict)
    printer_listing_source: str = "cached_or_unavailable"
    note: str = "Read-only root listing. File contents are not available through this endpoint."


class AuthRequest(BaseModel):
    token: str


class PreviewRequest(BaseModel):
    macro_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    retreat_from_home: bool = False
    first_layer_contents_confirmed: bool = False
    mode: Literal["operator", "commissioning"] = "operator"
    # Backward-compatible alias for clients from the prior preview API.
    commissioning: bool = False


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    macro_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    confirmation: str | None = None
    attended: bool = False
    no_competing_clients: bool = False
    power_switch_reachable: bool = False
    clear_travel_path: bool = False
    axis_at_home: bool = False
    retreat_from_home: bool = False
    first_layer_contents_confirmed: bool = False
    mode: Literal["operator", "commissioning"] = "operator"
    # Backward-compatible alias for clients from the prior execute API.
    commissioning: bool = False
    gantry_supported: bool = False


class OperatorObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_success: bool


class CapabilityRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str
    attended: bool
    no_competing_clients: bool
    power_switch_reachable: bool


class ExternalHomingReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: list[str]
    confirmation: str
    attended: bool
    no_competing_clients: bool
    power_switch_reachable: bool


class ControlArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str
    attended: bool
    no_competing_clients: bool
    power_switch_reachable: bool


class WorkflowStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    macro_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class WorkflowConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_token: str
    confirmed: bool


class CooldownCommissioningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    macro_id: str
    confirmation: str
    attended: bool
    no_competing_clients: bool


class MotionQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str


class PrinterService:
    """Owns the single poller, serialized printer I/O, cache, and bounded history."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        interval: float = 2.0,
        timeout: float = 2.5,
        client_factory: Any = MksWifiClient,
        sd_reader: Any = list_sd_root,
        motion_enabled: bool = False,
        motion_query_enabled: bool = False,
        motion_interval: float = 10.0,
        motion_query: Any = query_m114,
        backend: OctoPrintBackend | None = None,
        physical_status_reader: Any = None,
    ) -> None:
        if interval <= 0 or timeout <= 0:
            raise ValueError("poll interval and connection timeout must be positive")
        if not math.isfinite(motion_interval) or not 5 <= motion_interval <= 300:
            raise ValueError("motion polling interval must be between 5 and 300 seconds")
        self.host = host
        self.port = port
        self.interval = interval
        self.timeout = timeout
        self.client_factory = client_factory
        self.backend = backend
        self.physical_status_reader = physical_status_reader
        self.external_sd_status: dict[str, Any] | None = None
        self.sd_reader = sd_reader
        self.motion_enabled = motion_enabled
        self.motion_query_enabled = motion_query_enabled
        self.motion_interval = motion_interval
        self.motion_query = motion_query
        self.motion_last_query_monotonic: float | None = None
        self.motion_last_observed_at: datetime | None = None
        self.motion_latest: dict[str, Any] | None = None
        self.motion_manual_query_monotonic: float | None = None
        self.motion_history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)
        self.motion_status = "disabled" if not motion_enabled else "waiting_for_idle"
        self.motion_continuity_valid = False
        self.io_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.pause_polling = False
        self.worker_task: asyncio.Task[None] | None = None
        self.started_at = utc_now()
        self.last_attempt_at: datetime | None = None
        self.observed_at: datetime | None = None
        self.connected = False
        self.last_error: str | None = None
        self.telemetry: dict[str, Any] | None = None
        self.firmware: str | None = None
        self.firmware_observed_at: datetime | None = None
        self.failure_count = 0
        self.successful_polls = 0
        self.failed_polls = 0
        self.latencies_ms: deque[float] = deque(maxlen=60)
        self.history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)
        self.sd_status = "not_requested"
        self.sd_entries: list[dict[str, Any]] = []
        self.sd_refreshed_at: datetime | None = None
        self.sd_error: str | None = None

    async def poll_once(self) -> dict[str, Any]:
        started = time.monotonic()
        self.last_attempt_at = utc_now()
        try:
            async with self.io_lock:
                if self.backend is not None:
                    status = await self.backend.read_state()
                    if self.physical_status_reader is not None:
                        try:
                            self.external_sd_status = await self.physical_status_reader.read_state()
                        except (MksHttpStatusError, OSError, TimeoutError) as exc:
                            self.external_sd_status = {
                                "status": "unavailable", "state": "unknown",
                                "active": None, "source": "mks_wifi_http",
                                "confidence": "unavailable",
                                "error": str(exc), "observed_at": iso(utc_now()),
                            }
                else:
                    client = self.client_factory(self.host, port=self.port, timeout=self.timeout)
                    status = await client.get_status()
                    if (
                        self.firmware is None
                        or self.firmware_observed_at is None
                        or (utc_now() - self.firmware_observed_at).total_seconds()
                        >= FIRMWARE_REFRESH_SECONDS
                    ):
                        try:
                            self.firmware = await client.get_firmware()
                            self.firmware_observed_at = utc_now()
                        except MksWifiConnectionError:
                            # Status remains useful; retain the last known firmware string.
                            pass
                    if self.motion_enabled:
                        now = time.monotonic()
                        due = (
                            self.motion_last_query_monotonic is None
                            or now - self.motion_last_query_monotonic >= self.motion_interval
                        )
                        if due:
                            current_payload = telemetry_payload(status, self.firmware)
                            printer_state = current_payload["printer_state"].get("value")
                            idle = (
                                printer_state == "idle"
                                and current_payload["print_active"].get("value") is False
                            )
                            self.motion_last_query_monotonic = now
                            if idle:
                                try:
                                    result = await self.motion_query(
                                        self.host, self.port, min(self.timeout, 3.0)
                                    )
                                except Exception as exc:
                                    logger.info("M114 position query unavailable: %s", exc)
                                    result = {
                                        "status": "unsupported_or_unavailable",
                                        "error": str(exc) or type(exc).__name__,
                                        "axes": {
                                            axis: {
                                                "status": "unavailable",
                                                "value_mm": None,
                                            }
                                            for axis in AXES
                                        },
                                        "raw_lines": [],
                                    }
                                sample = build_motion_sample(
                                    result,
                                    observed_at=iso(utc_now()) or "",
                                    printer_state=printer_state,
                                    previous=(
                                        self.motion_latest if self.motion_continuity_valid else None
                                    ),
                                )
                                self.motion_latest = sample
                                self.motion_last_observed_at = utc_now()
                                self.motion_history.append(sample)
                                self.motion_status = sample["status"]
                                self.motion_continuity_valid = sample["status"] == "available"
                            else:
                                self.motion_status = "skipped_non_idle_or_unknown"
                                self.motion_continuity_valid = False
        except (MksWifiConnectionError, OctoPrintError, OSError, TimeoutError) as exc:
            self.motion_continuity_valid = False
            self.connected = False
            error = str(exc) or type(exc).__name__
            if error != self.last_error:
                logger.warning("Printer polling unavailable: %s", error)
            self.last_error = error
            self.failure_count += 1
            self.failed_polls += 1
            self.latencies_ms.append((time.monotonic() - started) * 1000)
            return self.status_payload()
        except Exception as exc:
            self.motion_continuity_valid = False
            logger.exception("Unexpected error during read-only printer poll")
            self.connected = False
            self.last_error = str(exc) or type(exc).__name__
            self.failure_count += 1
            self.failed_polls += 1
            self.latencies_ms.append((time.monotonic() - started) * 1000)
            return self.status_payload()

        self.telemetry = (
            status if self.backend is not None else telemetry_payload(status, self.firmware)
        )
        if self.backend is not None:
            self.telemetry["external_sd"] = self.external_sd_status or {
                "status": "unavailable", "state": "unknown", "active": None,
                "source": "mks_wifi_http", "confidence": "not_configured",
                "observed_at": None,
            }
        self.observed_at = utc_now()
        self.last_attempt_at = self.observed_at
        self.connected = True
        self.last_error = None
        self.failure_count = 0
        self.successful_polls += 1
        self.latencies_ms.append((time.monotonic() - started) * 1000)
        nozzle = self.telemetry["nozzle_temperature"].get("value")
        bed = self.telemetry["bed_temperature"].get("value")
        if nozzle is not None or bed is not None:
            self.history.append(
                {
                    "observed_at": iso(self.observed_at),
                    "nozzle_c": nozzle,
                    "bed_c": bed,
                }
            )
        return self.status_payload()

    async def run_poller(self, broadcaster: Any) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            if self.pause_polling:
                await asyncio.sleep(min(self.interval, 0.1))
                continue
            snapshot = await self.poll_once()
            await broadcaster(snapshot)
            if self.failure_count:
                delay = min(self.interval * (2 ** min(self.failure_count, 8)), MAX_BACKOFF_SECONDS)
            else:
                delay = self.interval
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, delay - elapsed))
            except TimeoutError:
                pass

    async def refresh_sd(self) -> dict[str, Any]:
        async with self.io_lock:
            try:
                if self.backend is not None:
                    self.sd_entries = await self.backend.list_files(refresh=True)
                    self.sd_refreshed_at = utc_now()
                    self.sd_status = "available"
                    self.sd_error = None
                else:
                    result = await self.sd_reader(self.host, self.port, timeout=self.timeout)
                    raw = result["raw_response"]
                    complete = result["status"] == "complete_listing"
                    if not complete:
                        self.sd_status = "partial_or_no_response"
                        self.sd_error = "Printer did not return the complete End file list marker."
                    else:
                        entries: list[dict[str, Any]] = []
                        for raw_line in raw.splitlines():
                            name = raw_line.strip()
                            lower = name.lower()
                            if not name or lower in {"begin file list", "end file list", "ok"}:
                                continue
                            kind = "directory" if name.upper().endswith(".DIR") else "file"
                            if kind == "directory":
                                name = name[:-4].rstrip("/") + "/"
                            entries.append({"name": name, "kind": kind, "size_bytes": None})
                        self.sd_entries = entries
                        self.sd_refreshed_at = utc_now()
                        self.sd_status = "available"
                        self.sd_error = None
            except (OSError, TimeoutError) as exc:
                self.sd_status = "error"
                self.sd_error = str(exc) or type(exc).__name__
            except Exception as exc:
                logger.exception("Unexpected error during read-only SD listing")
                self.sd_status = "error"
                self.sd_error = str(exc) or type(exc).__name__
        return self.sd_payload()

    def age_seconds(self) -> float | None:
        if self.observed_at is None:
            return None
        return max(0.0, (utc_now() - self.observed_at).total_seconds())

    def status_payload(self) -> dict[str, Any]:
        age = self.age_seconds()
        return {
            "connection": {
                "connected": self.connected,
                "host": self.host,
                "port": self.port,
                "transport": "OctoPrint API" if self.backend is not None else "raw TCP",
            },
            "telemetry": self.telemetry,
            "observed_at": iso(self.observed_at),
            "last_attempt_at": iso(self.last_attempt_at),
            "data_age_seconds": age,
            "stale": not self.connected or age is None or age > max(self.interval * 2.5, 5.0),
            "poll_interval_seconds": self.interval,
            "poll_latency_ms": self.latencies_ms[-1] if self.latencies_ms else None,
            "last_error": self.last_error,
            "firmware_identification": self.firmware,
            "motion": self.motion_payload(),
        }

    def motion_payload(self) -> dict[str, Any]:
        if (
            not self.motion_enabled
            and not self.motion_query_enabled
            and self.motion_latest is None
            and not self.motion_history
        ):
            return {
                "enabled": self.motion_query_enabled,
                "query_enabled": self.motion_query_enabled,
                "polling_enabled": False,
                "status": "waiting_for_query" if self.motion_query_enabled else "disabled",
                "printer_connected": self.connected,
                "note": (
                    "Periodic M114 polling is off. One-shot queries are enabled only by "
                    "server configuration."
                    if self.motion_query_enabled
                    else "M114 polling and one-shot queries are disabled until separately "
                    "authorized."
                ),
                "latest": None,
                "history": [],
                "poll_interval_seconds": self.motion_interval,
            }
        age = (
            None
            if self.motion_last_observed_at is None
            else max(0.0, (utc_now() - self.motion_last_observed_at).total_seconds())
        )
        latest = dict(self.motion_latest) if self.motion_latest else None
        if latest is not None:
            latest["age_seconds"] = age
            latest["fresh"] = age is not None and age <= max(self.motion_interval * 3, 30)
        history = []
        for sample in self.motion_history:
            item = dict(sample)
            try:
                sample_age = max(
                    0.0,
                    (utc_now() - datetime.fromisoformat(sample["poll_timestamp"])).total_seconds(),
                )
            except (KeyError, TypeError, ValueError):
                sample_age = None
            item["age_seconds"] = sample_age
            item["fresh"] = sample_age is not None and sample_age <= max(
                self.motion_interval * 3, 30
            )
            history.append(item)
        return {
            "enabled": True,
            "query_enabled": self.motion_query_enabled,
            "polling_enabled": self.motion_enabled,
            "status": self.motion_status,
            "printer_connected": self.connected,
            "note": (
                "M114 reports planned coordinates from firmware, not physical measurements. "
                "Polling is skipped unless printer state is freshly idle."
            ),
            "latest": latest,
            "history": history,
            "data_age_seconds": age,
            "stale": age is None or age > max(self.motion_interval * 3, 30),
            "poll_interval_seconds": self.motion_interval,
        }

    def temperatures_payload(self) -> dict[str, Any]:
        status = self.status_payload()
        telemetry = self.telemetry or {}
        return {
            "connection": status["connection"],
            "observed_at": status["observed_at"],
            "data_age_seconds": status["data_age_seconds"],
            "stale": status["stale"],
            "nozzle": telemetry.get(
                "nozzle_temperature", {"status": "unsupported_or_not_reported"}
            ),
            "bed": telemetry.get("bed_temperature", {"status": "unsupported_or_not_reported"}),
            "history": list(self.history),
        }

    def sd_payload(self) -> dict[str, Any]:
        return {
            "status": self.sd_status,
            "path": "/",
            "entries": self.sd_entries,
            "refreshed_at": iso(self.sd_refreshed_at),
            "error": self.sd_error,
            "source": "octoprint_sdcard" if self.backend is not None else "firmware_m20",
        }

    def diagnostics_payload(self) -> dict[str, Any]:
        latencies = list(self.latencies_ms)
        return {
            "worker_running": self.worker_task is not None and not self.worker_task.done(),
            "connection": self.status_payload()["connection"],
            "last_success_at": iso(self.observed_at),
            "last_attempt_at": iso(self.last_attempt_at),
            "last_error": self.last_error,
            "poll_interval_seconds": self.interval,
            "data_age_seconds": self.age_seconds(),
            "successful_polls": self.successful_polls,
            "failed_polls": self.failed_polls,
            "latency_last_ms": latencies[-1] if latencies else None,
            "latency_average_ms": sum(latencies) / len(latencies) if latencies else None,
            "uptime_seconds": max(0.0, (utc_now() - self.started_at).total_seconds()),
            "allowed_commands": list(READ_ONLY_COMMANDS)
            + [
                "M114 (idle-only, opt-in)"
                if self.motion_enabled
                else "M114 (disabled; separate authorization required)",
                "M20 (manual listing only)",
            ],
        }


class BroadcastHub:
    def __init__(self) -> None:
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self.clients):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.clients.discard(queue)


def _is_loopback(host: str) -> bool:
    if host.lower() in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authorized(request: Request, token: str | None) -> bool:
    peer = request.client.host if request.client else ""
    if not request.app.state.auth_required and _is_loopback(peer):
        return True
    if not token:
        return False
    cookie = request.cookies.get(SESSION_COOKIE, "")
    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    return hmac.compare_digest(cookie, token) or hmac.compare_digest(bearer, token)


def _origin_matches(host_header: str, origin: str | None) -> bool:
    if not origin:
        return False
    from urllib.parse import urlsplit

    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host_header.lower()


def create_app(
    service: PrinterService | None = None,
    *,
    bind_host: str | None = None,
    auth_token: str | None = None,
    control_enabled: bool | None = None,
    control_token: str | None = None,
    verified_commands: frozenset[str] | None = None,
    verified_macros: frozenset[str] | None = None,
    executor: CommandExecutor | None = None,
    commissioning_executor: CooldownCommissioningExecutor | None = None,
    timeline: MotionTimeline | None = None,
    capability_registry: CapabilityRegistry | None = None,
    local_gcode_indexer: LocalGcodeIndexer | None = None,
) -> FastAPI:
    bind_host = bind_host or os.getenv("NEPTUNE_OBSERVATORY_BIND_HOST", "127.0.0.1")
    auth_token = auth_token if auth_token is not None else os.getenv("NEPTUNE_OBSERVATORY_TOKEN")
    control_enabled = (
        control_enabled
        if control_enabled is not None
        else os.getenv("NEPTUNE_CONTROL_EXECUTION_ENABLED", "false").lower() == "true"
    )
    # `control_token` is accepted for compatibility with older callers but is
    # intentionally unused: existing dashboard authentication is sufficient.
    # These parameters remain accepted for compatibility with older callers, but
    # static environment allowlists no longer decide commissioning or verification.
    verified_commands = verified_commands or frozenset()
    verified_macros = verified_macros or frozenset()
    if verified_commands - KNOWN_COMMANDS:
        raise ValueError("NEPTUNE_VERIFIED_COMMANDS contains an unknown command family")
    if verified_macros - KNOWN_MACROS:
        raise ValueError("NEPTUNE_VERIFIED_MACROS contains an unknown macro identifier")
    auth_required = not _is_loopback(bind_host)
    if auth_required and not auth_token:
        raise ValueError("NEPTUNE_OBSERVATORY_TOKEN is required when binding beyond localhost")
    backend_name = os.getenv("NEPTUNE_PRINTER_BACKEND", "mks").strip().lower()
    if backend_name not in {"mks", "octoprint"}:
        raise ValueError("NEPTUNE_PRINTER_BACKEND must be mks or octoprint")
    physical_status_reader = None
    if (
        service is None and backend_name == "octoprint"
        and os.getenv("NEPTUNE_MKS_HTTP_MONITOR_ENABLED", "true").lower() == "true"
        and os.getenv("OCTOPRINT_MKS_WIFI_HOST")
    ):
        physical_status_reader = MksHttpStatusReader(
            os.environ["OCTOPRINT_MKS_WIFI_HOST"],
            port=int(os.getenv("NEPTUNE_MKS_HTTP_PORT", "80")),
            timeout=float(os.getenv("NEPTUNE_MKS_HTTP_TIMEOUT_SECONDS", "1.5")),
        )
    octoprint_backend = (
        OctoPrintBackend(
            os.getenv("OCTOPRINT_BASE_URL", "http://octoprint:80"),
            os.getenv("OCTOPRINT_API_KEY", ""),
            timeout=float(os.getenv("OCTOPRINT_TIMEOUT_SECONDS", "3")),
        )
        if service is None and backend_name == "octoprint"
        else None
    )
    printer = service or PrinterService(
        host=(
            octoprint_backend.host
            if octoprint_backend is not None
            else os.getenv("MKS_WIFI_HOST", DEFAULT_HOST)
        ),
        port=(
            octoprint_backend.port
            if octoprint_backend is not None
            else int(os.getenv("MKS_WIFI_PORT", str(DEFAULT_PORT)))
        ),
        backend=octoprint_backend,
        physical_status_reader=physical_status_reader,
        interval=float(os.getenv("NEPTUNE_POLL_INTERVAL_SECONDS", "2")),
        timeout=float(os.getenv("MKS_WIFI_TIMEOUT", "2.5")),
        motion_enabled=(
            octoprint_backend is None
            and os.getenv("NEPTUNE_MOTION_POLLING_ENABLED", "false").lower() == "true"
        ),
        motion_query_enabled=(
            octoprint_backend is None
            and os.getenv("NEPTUNE_MOTION_QUERY_ENABLED", "false").lower() == "true"
        ),
        motion_interval=float(os.getenv("NEPTUNE_MOTION_POLL_INTERVAL_SECONDS", "10")),
    )
    octoprint_mode = printer.backend is not None
    if octoprint_mode:
        # Never dispatch directly to the MKS module while OctoPrint owns it.
        control_enabled = False
        printer.motion_enabled = False
        printer.motion_query_enabled = False
    motion_timeline = timeline or MotionTimeline(
        os.getenv("NEPTUNE_TIMELINE_DB_PATH", "/data/neptune-observatory.db")
    )
    local_gcode_indexer = local_gcode_indexer or LocalGcodeIndexer(
        os.getenv("NEPTUNE_GCODE_SOURCE_DIR", "/mnt/neptune-source"), motion_timeline
    )
    hub = BroadcastHub()
    commands = DryRunCommandService()
    workflows = GuidedWorkflowManager()
    default_registry_path = "/data/capability-registry.json" if os.path.isdir("/data") else None
    registry_path = os.getenv("NEPTUNE_CAPABILITY_REGISTRY_PATH") or default_registry_path
    capability_registry_runtime = capability_registry or CapabilityRegistry(registry_path)
    command_executor = executor or CommandExecutor(
        enabled=control_enabled,
        verified_commands=verified_commands,
        verified_macros=verified_macros,
        host=printer.host,
        port=printer.port,
        timeout=float(os.getenv("NEPTUNE_COMMAND_TIMEOUT_SECONDS", "60")),
        extrusion_min_temperature_c=(
            float(
                os.getenv("NEPTUNE_EXTRUSION_MIN_TEMP_C")
                or os.getenv("NEPTUNE_VERIFIED_EXTRUSION_MIN_TEMP_C")
                or "180"
            )
        ),
        history_path=os.getenv("NEPTUNE_COMMAND_HISTORY_PATH", "/data/command-history.json"),
        capability_registry=capability_registry_runtime,
    )
    capability_registry_runtime = command_executor.capability_registry
    # Every application startup begins locked, even when an operator allowlist is configured.
    command_executor.disarm_session()
    if octoprint_mode:
        command_executor.enabled = False
    commissioning_enabled = (
        not octoprint_mode
        and os.getenv("NEPTUNE_COOLDOWN_COMMISSIONING_ENABLED", "false").lower() == "true"
    )
    cooldown_executor = commissioning_executor or CooldownCommissioningExecutor(
        lambda: MksTcpCommandTransport(
            printer.host,
            printer.port,
            float(os.getenv("NEPTUNE_COMMAND_TIMEOUT_SECONDS", "8")),
        ),
        timeout=min(float(os.getenv("NEPTUNE_COMMAND_TIMEOUT_SECONDS", "8")), 15.0),
        # Legacy one-shot route uses the existing dashboard credential as its
        # one-time ledger key; no second credential is needed.
        token=auth_token,
        state_path=os.getenv(
            "NEPTUNE_COOLDOWN_COMMISSIONING_STATE_PATH", "/data/cooldown-commissioning.json"
        ),
    )

    def recording_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Reconcile print history without changing OctoPrint control interlocks."""
        if printer.backend is None:
            return snapshot
        result = dict(snapshot)
        reported = dict(result.get("telemetry") or {})
        external = reported.get("external_sd") or {}
        # Do not let a conflicting MKS idle snapshot terminate a host-owned print.
        host_active = (reported.get("print_active") or {}).get("value")
        if external.get("status") == "available" and host_active is not True:
            reported["printer_state"] = {
                "status": "available", "value": external.get("state"),
                "source": "mks_wifi_http",
            }
            reported["print_active"] = {
                "status": "available", "value": external.get("active"),
                "source": "mks_wifi_http",
            }
            reported["progress_percent"] = {
                "status": "available" if external.get("progress_percent") is not None
                else "unsupported_or_not_reported",
                "value": external.get("progress_percent"),
                "source_command": "M27", "source": "mks_wifi_http",
            }
            reported["current_file"] = {
                "status": "available" if external.get("filename")
                else "unsupported_or_not_reported",
                "value": external.get("filename"), "source": "mks_wifi_http",
            }
            result["physical_source"] = "mks_wifi_http"
        elif (reported.get("print_active") or {}).get("value") is False:
            # OctoPrint Operational does not mean that a TFT SD job stopped.
            reported["printer_state"] = {
                "status": "unsupported_or_not_reported", "value": None,
            }
            reported["print_active"] = {
                "status": "unsupported_or_not_reported", "value": None,
            }
        result["telemetry"] = reported
        return result

    def dashboard_snapshot(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """Attach runtime coordinate estimates without presenting them as M114 data."""
        payload = dict(snapshot or printer.status_payload())
        reconciled = recording_snapshot(payload)
        motion = dict(payload.get("motion") or printer.motion_payload())
        control = command_executor.capability_payload()
        references = {
            axis: dict(values)
            for axis, values in control["axes"].items()
            if values.get("homed") and values.get("reference_observation")
        }
        motion["operator_reference"] = {
            "source": "runtime_axis_reference_estimate",
            "axes": references,
            "note": (
                "Command-acknowledged home origins and operator-reported home origins are "
                "coordinate estimates, not firmware position samples or continuous movement."
            ),
        }
        history = command_executor.history()
        operation_events = [
            {
                "id": item.get("id"),
                "macro_id": item.get("macro_id"),
                "state": item.get("state"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "timestamp_precision": item.get("timestamp_precision", "recorded"),
                "commands": item.get("commands", []),
                "transport_acknowledged": item.get("transport_acknowledged", False),
                "physical_completion_confirmed": item.get("physical_completion_confirmed", False),
                "operator_observation": item.get("operator_observation"),
                "observed_axes": item.get("observed_axes", []),
            }
            for item in history[-30:]
            if str(item.get("macro_id", "")).startswith(("home_", "jog_"))
            or item.get("macro_id") == "neptune_3_axis_orbital_dance"
        ]
        motion["operation_events"] = operation_events
        activity_operations = list(history[-10:])
        payload["command_activity"] = {
            "operations": [
                {
                    "id": item.get("id"),
                    "macro_id": item.get("macro_id"),
                    "state": item.get("state"),
                    "created_at": item.get("created_at"),
                    "commands": item.get("commands", []),
                    "command_results": item.get("command_results", []),
                    "transport_acknowledged": item.get("transport_acknowledged", False),
                    "physical_completion_confirmed": item.get(
                        "physical_completion_confirmed", False
                    ),
                    "operator_observation": item.get("operator_observation"),
                    "error": item.get("error"),
                }
                for item in activity_operations[-10:]
            ],
            "events": list(commands.audit)[-10:],
        }
        payload["motion"] = motion
        try:
            timeline_data = motion_timeline.snapshot(live_snapshot=reconciled)
        except Exception as exc:
            logger.exception("Motion timeline is temporarily unavailable")
            timeline_data = {
                "recording": "degraded",
                "active_session": None,
                "sessions": [],
                "gcode_files": [],
                "telemetry": [],
                "operations": [],
                "reconstruction": None,
                "planned_history": [],
                "feedrate_history": [],
                "timeline_events": [],
                "machine_state_events": [],
                "note": f"Timeline storage unavailable: {str(exc) or type(exc).__name__}",
            }
        payload["timeline"] = timeline_data
        payload["print_job"] = print_job_summary(payload)
        payload["printer_backend"] = "octoprint" if octoprint_mode else "mks"
        motion["planned_history"] = timeline_data.get("planned_history", [])
        return payload

    async def publish_dashboard_snapshot(snapshot: dict[str, Any]) -> None:
        try:
            motion_timeline.record_snapshot(
                recording_snapshot(snapshot), command_executor.history()
            )
        except Exception:
            logger.exception("Could not persist this telemetry timeline sample")
        await hub.publish(dashboard_snapshot(snapshot))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        printer.stop_event.clear()
        printer.worker_task = asyncio.create_task(printer.run_poller(publish_dashboard_snapshot))
        try:
            yield
        finally:
            await command_executor.shutdown()
            printer.stop_event.set()
            if printer.worker_task:
                with suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(printer.worker_task, timeout=printer.timeout + 2)
                if not printer.worker_task.done():
                    printer.worker_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await printer.worker_task
            printer.worker_task = None

    app = FastAPI(
        title="Neptune Observatory",
        version="1.0.0",
        description="Printer observability via MKS WiFi or the OctoPrint API.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.printer = printer
    app.state.motion_timeline = motion_timeline
    app.state.hub = hub

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_storage_error(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        is_full = "full" in str(exc).lower() or getattr(exc, "sqlite_errorcode", None) == getattr(
            sqlite3, "SQLITE_FULL", -1
        )
        return JSONResponse(
            status_code=507 if is_full else 503,
            content={
                "detail": (
                    "Persistent history storage is full. Live telemetry remains available; "
                    "history was not saved. Free Docker storage and retry this request."
                    if is_full
                    else (
                        "Persistent history storage is temporarily unavailable. "
                        "Check diagnostics before retrying."
                    )
                ),
                "history_storage": motion_timeline.storage_diagnostics(),
                "path": request.url.path,
            },
        )

    app.state.commands = commands
    app.state.workflows = workflows
    app.state.command_executor = command_executor
    app.state.cooldown_executor = cooldown_executor
    app.state.cooldown_commissioning_enabled = commissioning_enabled
    app.state.cooldown_audit = deque(cooldown_executor.history(), maxlen=20)
    app.state.cooldown_in_progress = False
    app.state.auth_required = auth_required
    app.state.auth_token = auth_token
    app.state.sd_upload_enabled = (
        not octoprint_mode and os.getenv("NEPTUNE_SD_UPLOAD_ENABLED", "false").lower() == "true"
    )
    app.state.sd_upload_in_progress = False
    app.state.octoprint_control_lock = asyncio.Lock()
    app.state.octoprint_command_last_accepted_at = {}

    app.mount(
        "/static",
        StaticFiles(directory=str(files("mks_wifi_bridge").joinpath("static"))),
        name="static",
    )

    async def require_auth(request: Request) -> None:
        if not _authorized(request, auth_token):
            raise HTTPException(status_code=401, detail="Authentication required")

    async def require_control_token(request: Request) -> None:
        await require_auth(request)
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")

    async def require_control_auth(request: Request) -> None:
        await require_control_token(request)
        if octoprint_mode:
            raise HTTPException(
                status_code=423,
                detail="Direct MKS machine controls are disabled in OctoPrint mode.",
            )
        if not command_executor.execution_enabled:
            raise HTTPException(
                status_code=423,
                detail="Machine controls are locked. Arm an authorized control session first.",
            )

    async def require_commissioning_auth(request: Request) -> None:
        await require_auth(request)
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")

    def request_requires_auth(request: Request) -> bool:
        peer = request.client.host if request.client else ""
        return auth_required or not _is_loopback(peer)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        page = files("mks_wifi_bridge").joinpath("static/index.html").read_text(encoding="utf-8")
        return HTMLResponse(page)

    @app.post("/api/auth/session", include_in_schema=False)
    async def create_session(payload: AuthRequest, request: Request):
        if not _origin_matches(request.headers.get("host", ""), request.headers.get("origin")):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        if not request_requires_auth(request):
            return {"authenticated": True}
        if not auth_token or not hmac.compare_digest(payload.token, auth_token):
            raise HTTPException(status_code=401, detail="Invalid token")
        from fastapi.responses import JSONResponse

        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            SESSION_COOKIE,
            auth_token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=12 * 60 * 60,
            path="/",
        )
        return response

    @app.get("/health", dependencies=[Depends(require_auth)])
    async def health() -> dict[str, Any]:
        worker = printer.worker_task
        storage = motion_timeline.storage_diagnostics()
        return {
            "status": "ok"
            if worker and not worker.done() and storage["status"] == "healthy"
            else "degraded",
            "poller_running": worker is not None and not worker.done(),
            "printer_connected": printer.connected,
            "history_storage": storage,
        }

    @app.get(
        "/api/printer/status",
        response_model=StatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def status() -> dict[str, Any]:
        return dashboard_snapshot()

    @app.get("/api/printer/motion", dependencies=[Depends(require_auth)])
    async def motion() -> dict[str, Any]:
        return dashboard_snapshot()["motion"]

    @app.get("/api/print/history", dependencies=[Depends(require_auth)])
    async def print_history(limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="Limit must be from 1 to 500.")
        return await asyncio.to_thread(motion_timeline.list_print_history, limit)

    @app.get("/api/print/history/octoprint", dependencies=[Depends(require_auth)])
    async def octoprint_print_history(limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="Limit must be from 1 to 500.")
        if not octoprint_mode or printer.backend is None:
            raise HTTPException(status_code=404, detail="OctoPrint backend is not active.")
        try:
            return await printer.backend.print_job_history(limit)
        except OctoPrintError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/timeline", dependencies=[Depends(require_auth)])
    async def timeline_snapshot() -> dict[str, Any]:
        return dashboard_snapshot()["timeline"]

    @app.get("/api/timeline/offline-demo", dependencies=[Depends(require_auth)])
    async def offline_motion_demo() -> dict[str, Any]:
        return motion_timeline.offline_demo()

    @app.get("/api/timeline/sessions/{session_id}", dependencies=[Depends(require_auth)])
    async def timeline_session(session_id: str) -> dict[str, Any]:
        result = motion_timeline.session_detail(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Print session not found")
        return result

    @app.post("/api/timeline/gcode", dependencies=[Depends(require_auth)])
    async def analyze_gcode(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        filename, content = payload.get("filename"), payload.get("content")
        if not isinstance(filename, str) or not filename.strip() or not isinstance(content, str):
            raise HTTPException(
                status_code=422, detail="Provide a G-code filename and text content"
            )
        try:
            return motion_timeline.store_gcode(content, filename)
        except (TypeError, ValueError) as exc:
            code = 413 if "50 MB" in str(exc) else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.post("/api/printer/motion/query", dependencies=[Depends(require_auth)])
    async def query_motion(payload: MotionQueryRequest, request: Request) -> dict[str, Any]:
        if not printer.motion_query_enabled:
            raise HTTPException(
                status_code=423,
                detail="One-shot M114 is disabled by server configuration. No command was sent.",
            )
        if payload.confirmation != "M114":
            raise HTTPException(
                status_code=422,
                detail='Type the exact read-only query name "M114" to continue.',
            )
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        if app.state.cooldown_in_progress or workflows.active is not None:
            raise HTTPException(
                status_code=409,
                detail="Another guided or commissioning workflow is active.",
            )
        active_operation = command_executor.get(command_executor.active_id or "")
        if active_operation and active_operation.get("state") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="A machine operation is active.")
        if printer.motion_manual_query_monotonic is not None and (
            time.monotonic() - printer.motion_manual_query_monotonic < 30
        ):
            raise HTTPException(
                status_code=429, detail="M114 is limited to one query per 30 seconds."
            )
        if printer.io_lock.locked():
            raise HTTPException(
                status_code=409, detail="Printer connection is busy; try again later."
            )

        async with printer.io_lock:
            snapshot = printer.status_payload()
            telemetry = snapshot.get("telemetry") or {}
            printer_state = (telemetry.get("printer_state") or {}).get("value")
            if (
                not snapshot["connection"]["connected"]
                or snapshot["stale"]
                or snapshot["data_age_seconds"] is None
                or snapshot["data_age_seconds"] > 5
                or printer_state != "idle"
                or (telemetry.get("print_active") or {}).get("value") is not False
            ):
                raise HTTPException(
                    status_code=409,
                    detail="M114 requires connected, fresh telemetry showing an idle printer.",
                )
            if command_executor.active_id is not None or workflows.active is not None:
                raise HTTPException(status_code=409, detail="A machine workflow became active.")
            printer.motion_manual_query_monotonic = time.monotonic()
            # Treat the configured one-shot as a consumable capability. Consume it
            # before touching the transport so an ambiguous timeout cannot be retried.
            printer.motion_query_enabled = False
            result = await printer.motion_query(
                printer.host, printer.port, min(printer.timeout, 3.0)
            )
            sample = build_motion_sample(
                result,
                observed_at=iso(utc_now()) or "",
                printer_state=printer_state,
                previous=(
                    printer.motion_latest
                    if printer.motion_latest and printer.motion_latest.get("status") == "available"
                    else None
                ),
            )
            printer.motion_latest = sample
            printer.motion_last_observed_at = utc_now()
            printer.motion_history.append(sample)
            printer.motion_status = sample["status"]
            printer.motion_continuity_valid = sample["status"] == "available"
        commands.record_event(
            "motion_m114",
            "position_query_completed"
            if sample["status"] == "available"
            else "position_query_unavailable",
            sample.get("error"),
        )
        snapshot = dashboard_snapshot()
        await hub.publish(snapshot)
        return {"sample": sample, "motion": snapshot["motion"]}

    @app.get(
        "/api/printer/temperatures",
        response_model=TemperatureResponse,
        dependencies=[Depends(require_auth)],
    )
    async def temperatures() -> dict[str, Any]:
        return printer.temperatures_payload()

    @app.get(
        "/api/printer/firmware",
        response_model=FirmwareResponse,
        dependencies=[Depends(require_auth)],
    )
    async def firmware() -> dict[str, Any]:
        return {
            "status": "available" if printer.firmware else "unsupported_or_not_reported",
            "value": printer.firmware,
            "observed_at": iso(printer.firmware_observed_at),
        }

    @app.get(
        "/api/printer/sdcard",
        response_model=SdCardResponse,
        dependencies=[Depends(require_auth)],
    )
    async def sdcard(request: Request, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            origin = request.headers.get("origin")
            host_header = request.headers.get("host", "")
            if origin and not _origin_matches(host_header, origin):
                raise HTTPException(status_code=403, detail="Request origin is not allowed")
            result = await printer.refresh_sd()
            commands.update_sd_listing(result.get("entries", []))
            payload = result
        else:
            payload = printer.sd_payload()
        commands.update_sd_listing(payload.get("entries", []))
        local_index = await asyncio.to_thread(local_gcode_indexer.scan)
        entries = [
            {
                **entry,
                "ambiguous": entry.get("kind") == "file"
                and commands.is_sd_file_ambiguous(entry.get("name", "")),
                "preview_available": entry.get("kind") == "file"
                and motion_timeline.has_sd_thumbnail(entry.get("name", "")),
            }
            for entry in payload.get("entries", [])
        ]
        payload["entries"] = local_gcode_indexer.match_entries(entries, local_index)
        payload["local_index"] = local_index
        payload["printer_listing_source"] = (
            "octoprint_sdcard"
            if octoprint_mode
            else "firmware_m20"
            if payload.get("status") == "available"
            and payload.get("refreshed_at")
            and (utc_now() - datetime.fromisoformat(payload["refreshed_at"])).total_seconds() <= 60
            else "cached_or_unavailable"
        )
        return payload

    @app.get(
        "/api/printer/sdcard/preview/{filename}",
        dependencies=[Depends(require_auth)],
    )
    async def sdcard_preview(filename: str) -> Response:
        preview = motion_timeline.get_unique_local_gcode_preview(filename)
        if preview is None:
            preview = motion_timeline.get_sd_thumbnail(filename)
        if preview is None:
            raise HTTPException(status_code=404, detail="No saved print thumbnail is available.")
        mime_type, image_data = preview
        return Response(
            content=image_data,
            media_type=mime_type,
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/printer/sdcard/local-preview", dependencies=[Depends(require_auth)])
    async def local_sdcard_preview(path: str) -> Response:
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise HTTPException(status_code=400, detail="Invalid local G-code index path.")
        preview = motion_timeline.get_local_gcode_preview_by_path(path)
        if preview is None:
            raise HTTPException(status_code=404, detail="No cached local preview is available.")
        mime_type, image_data = preview
        return Response(
            content=image_data,
            media_type=mime_type,
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @app.post(
        "/api/printer/sdcard/preview",
        dependencies=[Depends(require_auth)],
    )
    async def cache_sdcard_preview(request: Request, filename: str) -> dict[str, Any]:
        """Read only the embedded thumbnail from a local G-code file.

        The G-code is never retained or sent to the printer by this endpoint.
        """
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        declared_size = request.headers.get("content-length")
        if declared_size is not None:
            try:
                if int(declared_size) > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="G-code file exceeds 50 MB.")
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid Content-Length header."
                ) from exc
        chunks: list[bytes] = []
        content_size = 0
        async for chunk in request.stream():
            content_size += len(chunk)
            if content_size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="G-code file exceeds 50 MB.")
            chunks.append(chunk)
        content = b"".join(chunks)
        try:
            validate_upload(filename, content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        thumbnail = extract_gcode_thumbnail(content)
        if thumbnail is None:
            return {
                "filename": filename,
                "preview_available": False,
                "note": (
                    "No supported embedded PNG/JPEG thumbnail was found; "
                    "the G-code was not retained."
                ),
            }
        saved = motion_timeline.save_sd_thumbnail(filename, *thumbnail)
        if not saved:
            raise HTTPException(
                status_code=507, detail="Could not save the print preview to Observatory storage."
            )
        return {
            "filename": filename,
            "preview_available": True,
            "note": "Embedded preview saved. G-code was not retained or sent to the printer.",
        }

    @app.get("/api/printer/sdcard/upload", dependencies=[Depends(require_auth)])
    async def sd_upload_status() -> dict[str, Any]:
        configured = bool(app.state.sd_upload_enabled)
        return {
            "supported_by_source": not octoprint_mode,
            "configured": configured,
            "available": configured and command_executor.execution_enabled,
            "status": "ready_after_confirmation"
            if configured and command_executor.execution_enabled
            else "disabled_by_default"
            if not configured
            else "control_session_disarmed",
            "note": (
                "Makerbase source contains a raw POST /upload?X-Filename handler. "
                "This installed printer has not been tested; an HTTP response is not proof "
                "that the file reached or is readable from the SD card."
            ),
        }

    @app.post("/api/printer/sdcard/upload", dependencies=[Depends(require_auth)])
    async def upload_sd_file(request: Request, filename: str) -> dict[str, Any]:
        if octoprint_mode:
            raise HTTPException(
                status_code=423, detail="MKS SD uploads disabled in OctoPrint mode."
            )
        if not app.state.sd_upload_enabled:
            raise HTTPException(
                status_code=423,
                detail="Wireless upload is disabled by default. No file was sent.",
            )
        declared_size = request.headers.get("content-length")
        if declared_size is not None:
            try:
                if int(declared_size) > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="The G-code file exceeds the 50 MB upload limit.",
                    )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid Content-Length header."
                ) from exc
        await require_control_auth(request)
        confirmation = request.headers.get("x-upload-confirmation", "")
        if confirmation != f"UPLOAD {filename}":
            raise HTTPException(
                status_code=422,
                detail=f'Type "UPLOAD {filename}" exactly to confirm this SD-card write.',
            )
        if any(
            request.headers.get(header, "").lower() != "true"
            for header in (
                "x-operator-attended",
                "x-power-switch-reachable",
                "x-no-competing-clients",
            )
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Physical attendance, a reachable power switch, and no competing clients "
                    "are required."
                ),
            )
        if (
            app.state.sd_upload_in_progress
            or command_executor.active_id is not None
            or workflows.active is not None
            or app.state.cooldown_in_progress
        ):
            raise HTTPException(status_code=409, detail="Another printer operation is active.")
        snapshot = printer.status_payload()
        telemetry = snapshot.get("telemetry") or {}
        try:
            _require_fresh_state(snapshot, "idle")
        except ExecutionBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if (telemetry.get("print_active") or {}).get("value") is not False:
            raise HTTPException(status_code=409, detail="Printer must report no active print.")
        if (
            not printer.sd_refreshed_at
            or (utc_now() - printer.sd_refreshed_at).total_seconds() > 60
        ):
            raise HTTPException(status_code=409, detail="Refresh the read-only M20 listing first.")
        if filename.casefold() in {item.casefold() for item in commands.allowed_sd_files}:
            raise HTTPException(
                status_code=409,
                detail="That filename already exists on the SD card. Overwrite is blocked.",
            )
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            chunks: list[bytes] = []
            content_size = 0
            async for chunk in request.stream():
                content_size += len(chunk)
                if content_size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="The G-code file exceeds the 50 MB upload limit.",
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
            validate_upload(filename, content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if printer.io_lock.locked():
            raise HTTPException(
                status_code=409,
                detail=(
                    "The shared MKS connection is busy. No upload was attempted; return to the "
                    "SD page and try later."
                ),
            )
        app.state.sd_upload_in_progress = True
        try:
            async with printer.io_lock:
                result = await asyncio.to_thread(upload_to_mks, printer.host, filename, content)
        except UploadError as exc:
            commands.record_event("sd_upload", "transport_failed", str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            app.state.sd_upload_in_progress = False
        digest = hashlib.sha256(content).hexdigest()
        commands.record_event("sd_upload", "transport_accepted_file_transfer")
        thumbnail = extract_gcode_thumbnail(content)
        preview_saved = False
        if thumbnail is not None:
            preview_saved = motion_timeline.save_sd_thumbnail(filename, *thumbnail)
        return {
            "filename": filename,
            "size_bytes": len(content),
            "sha256": digest,
            "state": "http_accepted_transfer_completion_unconfirmed",
            "http_status": result["http_status"],
            "response": result["response"],
            "preview_saved": preview_saved,
            "next_step": "Refresh M20 and verify the exact filename before using it.",
        }

    @app.get("/api/diagnostics", dependencies=[Depends(require_auth)])
    async def diagnostics() -> dict[str, Any]:
        return {
            **printer.diagnostics_payload(),
            "history_storage": motion_timeline.storage_diagnostics(),
        }

    @app.get("/api/control/catalog", dependencies=[Depends(require_auth)])
    async def control_catalog() -> dict[str, Any]:
        macro_catalog = commands.list_macros()
        for macro in macro_catalog:
            proposed = macro["command_families"]
            capability = capability_registry_runtime.get(macro["id"])
            if macro["id"] == "home_x" and not any(
                evidence.get("commands") == ["G91", "G1 X30 F600", "G90", "G28 X"]
                for evidence in capability.get("evidence", [])
            ):
                capability = {
                    **capability,
                    "status": "ready_for_commissioning",
                    "reason": (
                        "The original G28 X was physically observed to work. This new "
                        "30 mm retreat-and-home sequence still needs its own observation."
                    ),
                }
            readiness = command_executor.operation_readiness(proposed, macro["id"])
            if readiness["status"] in {"disarmed", "ready_to_confirm"}:
                try:
                    _require_fresh_state(
                        printer.status_payload(), _required_printer_state(macro["id"])
                    )
                except ExecutionBlocked as exc:
                    readiness = {"status": "temporarily_unavailable", "reason": str(exc)}
            macro["readiness"] = readiness
            macro["enabled_for_execution"] = readiness["status"] == "ready_to_confirm"
            macro["capability"] = capability
            macro["commissioning_status"] = capability["status"]
            macro["commissioning_readiness"] = command_executor.operation_readiness(
                proposed, macro["id"], commissioning=True
            )
            if macro["id"] == "home_x":
                normal_home_verified = any(
                    evidence.get("commands") == ["G28 X"] or evidence.get("operation") == "G28 X"
                    for evidence in capability.get("evidence", [])
                )
                macro["compatibility"] = (
                    "operator_verified_on_this_printer"
                    if normal_home_verified
                    else "documented_unverified"
                )
                macro["commissioning_status"] = (
                    "command_acknowledged_reference_estimate"
                    if "x" in command_executor.homed_axes
                    else "verified_historically_reference_cleared"
                    if normal_home_verified
                    else "not_physically_verified"
                )
                macro["current_home_reference"] = "x" in command_executor.homed_axes
            elif macro["id"] in {"jog_x_positive", "jog_x_negative"}:
                macro["commissioning_status"] = (
                    "estimated_reference_available"
                    if "x" in command_executor.homed_axes
                    else "awaiting_home_reference"
                )
        capabilities = [dict(item) for item in CAPABILITIES]
        if "x" in command_executor.homed_axes:
            for item in capabilities:
                if item["command"] == "G1 X":
                    item["status"] = "estimated_reference_available"
                    item["basis"] = (
                        "Bounded jogging is available after an in-session home reference. "
                        "Positions are command estimates until firmware position feedback exists."
                    )
        return {
            "execution_enabled": command_executor.execution_enabled,
            "printer_backend": "octoprint" if octoprint_mode else "mks",
            "macros": macro_catalog,
            "calibrations": commands.list_calibrations(),
            "capabilities": capabilities,
            "capability_registry": capability_registry_runtime.list(),
            "control": command_executor.capability_payload(),
            "notice": (
                "Operator Mode permits fixed supported actions without prior commissioning. "
                "It starts disarmed and remains armed until explicit disarm or service restart. "
                "Your existing dashboard sign-in authorizes guarded controls; each action still "
                "requires explicit confirmation, fresh telemetry, and its safety prerequisites. "
                "Commissioning is optional and records physical verification separately."
            ),
        }

    @app.get("/api/octoprint/control/capabilities", dependencies=[Depends(require_auth)])
    async def octoprint_control_capabilities() -> dict[str, Any]:
        enabled = (
            octoprint_mode
            and os.getenv("NEPTUNE_OCTOPRINT_CONTROLS_ENABLED", "false").lower() == "true"
        )
        return {
            "backend": "octoprint" if octoprint_mode else "mks",
            "enabled": enabled,
            "operations": (
                ["home_x", "home_y", "home_z", "pause", "resume", "cancel"] if enabled else []
            ),
            "notice": (
                "OctoPrint exclusively owns the MKS link. Every action requires "
                "fresh telemetry, physical attendance and exact confirmation. "
                "Acceptance does not prove mechanical completion."
            ),
        }

    @app.post("/api/octoprint/control", dependencies=[Depends(require_auth)])
    async def octoprint_control(request: Request) -> dict[str, Any]:
        """Explicit, attended commands through OctoPrint only; never native MKS."""
        await require_control_token(request)
        if not octoprint_mode or printer.backend is None:
            raise HTTPException(status_code=404, detail="OctoPrint backend is not active.")
        if os.getenv("NEPTUNE_OCTOPRINT_CONTROLS_ENABLED", "false").lower() != "true":
            raise HTTPException(status_code=423, detail="OctoPrint controls are disabled.")
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid request JSON.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Expected a JSON object.")
        operation = body.get("operation")
        allowed = {"pause", "resume", "cancel", "home_x", "home_y", "home_z"}
        if not isinstance(operation, str) or operation not in allowed:
            raise HTTPException(status_code=422, detail="Unsupported control operation.")
        if (
            body.get("confirmation") != f"CONFIRM {operation.upper()}"
            or body.get("attended") is not True
            or body.get("power_switch_reachable") is not True
        ):
            raise HTTPException(
                status_code=422,
                detail="Exact confirmation, attendance, and accessible power switch required.",
            )
        if operation.startswith("home_") and body.get("travel_path_clear") is not True:
            raise HTTPException(
                status_code=422,
                detail="A clear homing travel path must be confirmed.",
            )
        async with app.state.octoprint_control_lock:
            snapshot = printer.status_payload()
            state = (snapshot.get("telemetry") or {}).get("printer_state", {}).get("value")
            age = snapshot.get("data_age_seconds")
            if (
                snapshot.get("stale")
                or not snapshot.get("connection", {}).get("connected")
                or age is None
                or age > 5
            ):
                raise HTTPException(status_code=409, detail="Fresh printer telemetry is required.")
            required = (
                "idle"
                if operation.startswith("home_")
                else (
                    "paused"
                    if operation == "resume"
                    else "printing"
                    if operation == "pause"
                    else None
                )
            )
            if required is not None and state != required:
                raise HTTPException(
                    status_code=409,
                    detail=f"Operation requires printer state: {required}.",
                )
            if operation == "cancel" and state not in {"printing", "paused"}:
                raise HTTPException(status_code=409, detail="No active print to cancel.")
            last_sent = app.state.octoprint_command_last_accepted_at.get(operation)
            if last_sent is not None and time.monotonic() - last_sent < 10:
                raise HTTPException(
                    status_code=409,
                    detail="A matching command was recently accepted. Verify the printer first.",
                )
            try:
                result = await printer.backend.execute_control(operation)
            except OctoPrintError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            if result.get("accepted"):
                app.state.octoprint_command_last_accepted_at[operation] = time.monotonic()
            commands.record_event(operation, "octoprint_command_accepted")
            return {**result, "backend": "octoprint", "verified_complete": False}

    @app.post("/api/control/arm", dependencies=[Depends(require_auth)])
    async def arm_control_session(payload: ControlArmRequest, request: Request) -> dict[str, Any]:
        await require_control_token(request)
        if octoprint_mode:
            raise HTTPException(
                status_code=423, detail="Machine controls are locked in OctoPrint mode."
            )
        if payload.confirmation != "ENABLE OPERATOR MODE":
            raise HTTPException(status_code=422, detail="Type ENABLE OPERATOR MODE exactly.")
        if (
            not payload.attended
            or not payload.no_competing_clients
            or not payload.power_switch_reachable
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Physical attendance, a reachable power switch, and disconnection of "
                    "other printer clients are required."
                ),
            )
        if workflows.active is not None or command_executor.active_id is not None:
            raise HTTPException(status_code=409, detail="A machine operation is already active.")
        try:
            snapshot = printer.status_payload()
            telemetry = snapshot.get("telemetry") or {}
            state_name = (telemetry.get("printer_state") or {}).get("value")
            age = snapshot.get("data_age_seconds")
            if (
                not snapshot.get("connection", {}).get("connected")
                or snapshot.get("stale")
                or age is None
                or age > 5
                or state_name not in {"idle", "printing", "paused"}
            ):
                raise ExecutionBlocked(
                    "Fresh connected telemetry must report idle, printing, or paused before "
                    "the control session can arm."
                )
            active_value = (telemetry.get("print_active") or {}).get("value")
            if state_name == "idle" and active_value is not False:
                raise ExecutionBlocked(
                    "Idle state must include an explicit no-active-print report."
                )
            if state_name in {"printing", "paused"} and active_value is not True:
                raise ExecutionBlocked(
                    "Print-control arming requires an explicit active-print report."
                )
            armed = command_executor.arm_session(
                attended=payload.attended,
                no_competing_clients=payload.no_competing_clients,
                power_switch_reachable=payload.power_switch_reachable,
            )
        except ExecutionBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event("machine_controls", "control_session_armed")
        return {**command_executor.capability_payload(), **armed}

    @app.post("/api/control/disarm", dependencies=[Depends(require_auth)])
    async def disarm_control_session(request: Request) -> dict[str, Any]:
        await require_control_token(request)
        try:
            command_executor.disarm_session()
        except ExecutionBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event("machine_controls", "control_session_disarmed")
        return command_executor.capability_payload()

    @app.post("/api/control/references/home", dependencies=[Depends(require_auth)])
    async def record_external_home_reference(
        payload: ExternalHomingReferenceRequest, request: Request
    ) -> dict[str, Any]:
        await require_control_token(request)
        if (
            payload.confirmation != "RECORD TFT-HOMED AXES"
            or not payload.attended
            or not payload.no_competing_clients
            or not payload.power_switch_reachable
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Type RECORD TFT-HOMED AXES and confirm attendance, the reachable "
                    "power switch, and no competing clients."
                ),
            )
        if workflows.active is not None or command_executor.active_id is not None:
            raise HTTPException(status_code=409, detail="A machine operation is active.")
        snapshot = printer.status_payload()
        telemetry = snapshot.get("telemetry") or {}
        if (
            not snapshot.get("connection", {}).get("connected")
            or snapshot.get("stale")
            or snapshot.get("data_age_seconds") is None
            or snapshot["data_age_seconds"] > 5
            or (telemetry.get("printer_state") or {}).get("value") != "idle"
            or (telemetry.get("print_active") or {}).get("value") is not False
        ):
            raise HTTPException(
                status_code=409,
                detail="Fresh idle telemetry is required to record home references.",
            )
        try:
            result = command_executor.record_external_homing_reference(payload.axes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ExecutionBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event(
            "external_homing_reference",
            "operator_reported_tft_homing_" + "_".join(result["axes"]),
        )
        return {**result, "control": command_executor.capability_payload()}

    @app.get("/api/control/state", dependencies=[Depends(require_auth)])
    async def control_state() -> dict[str, Any]:
        return {
            "control": command_executor.capability_payload(),
            "active_operation": (
                command_executor.get(command_executor.active_id)
                if command_executor.active_id
                else None
            ),
            "operations": command_executor.history(),
            "latest_telemetry": printer.status_payload(),
            "workflows": workflows.payload(),
        }

    @app.post("/api/control/workflows", status_code=201, dependencies=[Depends(require_auth)])
    async def start_workflow(payload: WorkflowStartRequest, request: Request) -> dict[str, Any]:
        if octoprint_mode:
            raise HTTPException(status_code=423, detail="MKS workflows disabled in OctoPrint mode.")
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            session = workflows.start(
                payload.macro_id,
                payload.parameters,
                printer.status_payload(),
                command_executor.active_id is not None,
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event(payload.macro_id, "guided_workflow_started")
        return session

    @app.post(
        "/api/control/commissioning/cooldown",
        status_code=202,
        dependencies=[Depends(require_commissioning_auth)],
    )
    async def commissioning_cooldown(
        payload: CooldownCommissioningRequest, request: Request
    ) -> dict[str, Any]:
        if not commissioning_enabled:
            raise HTTPException(status_code=423, detail="Cooldown commissioning is disabled.")
        if request.app.state.cooldown_in_progress:
            raise HTTPException(
                status_code=409, detail="A cooldown commissioning operation is already active."
            )
        if (
            payload.macro_id != "cooldown"
            or payload.confirmation != "Cooldown"
            or not payload.attended
            or not payload.no_competing_clients
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "This route requires exact-name Cooldown, physical attendance, and "
                    "confirmation that other printer clients are disconnected."
                ),
            )
        if command_executor.execution_enabled or command_executor.active_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Normal command execution must remain disabled and idle.",
            )
        if workflows.active is not None:
            raise HTTPException(status_code=409, detail="Cancel the active guided workflow first.")
        snapshot = printer.status_payload()
        state = snapshot.get("telemetry") or {}
        state_name = (state.get("printer_state") or {}).get("value")
        age = snapshot.get("data_age_seconds")
        if (
            not snapshot.get("connection", {}).get("connected")
            or snapshot.get("stale")
            or age is None
            or age > 5
        ):
            raise HTTPException(
                status_code=409, detail="Fresh connected telemetry <=5 seconds is required."
            )
        if state_name != "idle" or (state.get("print_active") or {}).get("value") is not False:
            raise HTTPException(
                status_code=409,
                detail="Printer must be freshly observed idle with no active print.",
            )
        if cooldown_executor.used:
            raise HTTPException(
                status_code=409,
                detail="This one-time commissioning authorization was already used.",
            )
        # Stop the periodic poller before opening the single-client MKS socket.
        request.app.state.cooldown_in_progress = True
        printer.pause_polling = True
        operation_started = time.monotonic()
        try:
            try:
                result = await cooldown_executor.run(
                    printer.io_lock, preflight=printer.status_payload
                )
            except ExecutionBlocked as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            post_snapshot = await printer.poll_once()
        finally:
            printer.pause_polling = False
            request.app.state.cooldown_in_progress = False
        audit = {
            "created_at": iso(utc_now()),
            "macro_id": "cooldown",
            "commands": ["M104 S0", "M140 S0"],
            "state": result["state"],
            "elapsed_seconds": round(time.monotonic() - operation_started, 3),
            "command_results": result["results"],
            "telemetry_before": snapshot,
            "telemetry_after": post_snapshot,
            "telemetry_note": (
                "Read-only sample collected after the command session. Compare heater targets; "
                "temperature decline takes longer and is not implied by target acknowledgment."
            ),
            "error": result.get("error"),
        }
        request.app.state.cooldown_audit.append(audit)
        cooldown_executor.record(audit)
        commands.record_event("cooldown", "commissioning_" + result["state"], result.get("error"))
        return audit

    @app.get("/api/control/commissioning/cooldown", dependencies=[Depends(require_auth)])
    async def commissioning_cooldown_status() -> dict[str, Any]:
        return {
            "enabled": commissioning_enabled and bool(auth_token) and not cooldown_executor.used,
            "used": cooldown_executor.used,
            "in_progress": app.state.cooldown_in_progress,
            "one_time": True,
            "operations": cooldown_executor.history(),
            "normal_execution_enabled": command_executor.execution_enabled,
        }

    @app.get("/api/control/workflows", dependencies=[Depends(require_auth)])
    async def workflow_state() -> dict[str, Any]:
        return workflows.payload()

    @app.get("/api/control/workflows/{workflow_id}", dependencies=[Depends(require_auth)])
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        session = workflows.public_state(workflows.active)
        if not session or session["id"] != workflow_id:
            session = next(
                (item for item in reversed(workflows.history) if item["id"] == workflow_id),
                None,
            )
        if session is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return session

    @app.post(
        "/api/control/workflows/{workflow_id}/confirm",
        dependencies=[Depends(require_auth)],
    )
    async def confirm_workflow_step(
        workflow_id: str, payload: WorkflowConfirmationRequest, request: Request
    ) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            result = workflows.confirm(
                workflow_id,
                payload.step_token,
                payload.confirmed,
                printer.status_payload(),
                command_executor.active_id is not None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WorkflowError as exc:
            commands.record_event("filament_change_guided", "guided_workflow_paused", str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event(
            "filament_change_guided",
            "guided_workflow_completed"
            if result["state"] == "completed"
            else "guided_workflow_step_confirmed",
        )
        await hub.publish(dashboard_snapshot())
        return result

    @app.post(
        "/api/control/workflows/{workflow_id}/cancel",
        dependencies=[Depends(require_auth)],
    )
    async def cancel_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            result = workflows.cancel(workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        commands.record_event("filament_change_guided", "guided_workflow_cancelled")
        return result

    @app.post("/api/control/preview", dependencies=[Depends(require_auth)])
    async def control_preview(payload: PreviewRequest) -> dict[str, Any]:
        if octoprint_mode:
            raise HTTPException(
                status_code=423, detail="Direct MKS controls disabled in OctoPrint mode."
            )
        try:
            if payload.macro_id in {"start_sd_print", "print_first_layer_sd"}:
                if (
                    printer.sd_refreshed_at is None
                    or (utc_now() - printer.sd_refreshed_at).total_seconds() > 30
                ):
                    commands.update_sd_listing([])
                else:
                    commands.update_sd_listing(printer.sd_entries)
            preview = commands.preview(
                payload.macro_id,
                payload.parameters,
                retreat_from_home=payload.retreat_from_home,
            )
            commissioning = payload.mode == "commissioning" or payload.commissioning
            readiness = command_executor.operation_readiness(
                preview["commands"], payload.macro_id, commissioning=commissioning
            )
            ready = readiness["status"] in {"ready_to_confirm", "ready_for_commissioning"}
            state_error = None
            if ready:
                try:
                    _require_fresh_state(
                        printer.status_payload(), _required_printer_state(payload.macro_id)
                    )
                except ExecutionBlocked as exc:
                    ready = False
                    state_error = str(exc)
            preview["execution_enabled"] = ready
            preview["execution_notice"] = (
                "This sequence may be submitted only when all server safety gates pass."
                if preview["execution_enabled"]
                else state_error or readiness["reason"]
            )
            preview["readiness"] = readiness
            preview["commissioning"] = commissioning
            preview["mode"] = "commissioning" if commissioning else "operator"
            preview["capability"] = command_executor.capability_registry.get(payload.macro_id)
            return preview
        except PreviewError as exc:
            commands.record_error(payload.macro_id, str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/control/execute", status_code=202, dependencies=[Depends(require_auth)])
    async def control_execute(payload: ExecutionRequest, request: Request) -> dict[str, Any]:
        if octoprint_mode:
            raise HTTPException(
                status_code=423, detail="Direct MKS controls disabled in OctoPrint mode."
            )
        if workflows.active is not None and not workflows.allows_operation(payload.macro_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This operation is not available at the current guided-workflow step. "
                    "Use one of the dashboard actions shown in the active step or cancel it."
                ),
            )
        if command_executor.active_id is not None:
            active_operation = command_executor.get(command_executor.active_id)
            if active_operation and active_operation.get("state") in {"queued", "running"}:
                raise HTTPException(
                    status_code=409,
                    detail="Another machine command operation is active.",
                )
        if not command_executor.execution_enabled:
            commands.reject_execution(payload.macro_id)
            raise HTTPException(
                status_code=423,
                detail="Machine-control execution is disabled. No command was sent to the printer.",
            )
        try:
            await require_control_auth(request)
        except HTTPException as exc:
            commands.record_event(payload.macro_id, "execution_denied", exc.detail)
            raise
        if not all(command_executor.session_attestation.values()):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Arm Operator Mode with physical attendance, reachable power access, and "
                    "no competing printer clients before a command operation."
                ),
            )
        needs_clear_path = payload.macro_id in {
            "home_x",
            "home_y",
            "home_z",
            "home_axes",
            "neptune_3_axis_orbital_dance",
            "jog_x_positive",
            "jog_x_negative",
            "jog_y_positive",
            "jog_y_negative",
            "jog_z_positive",
            "jog_z_negative",
        }
        if needs_clear_path and not payload.clear_travel_path:
            raise HTTPException(
                status_code=422,
                detail="Confirm that the printer's travel path is clear before homing.",
            )
        if payload.retreat_from_home and not payload.axis_at_home:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Confirm the selected axis, or all three axes for Home All, are physically "
                    "at their home switches before choosing a retreat."
                ),
            )
        if payload.macro_id == "motors_disable" and not payload.gantry_supported:
            raise HTTPException(
                status_code=422,
                detail="Confirm the gantry is mechanically supported before disabling motors.",
            )
        if (
            payload.macro_id == "print_first_layer_sd"
            and not payload.first_layer_contents_confirmed
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Confirm that the selected G-code contains only the intended first layer. "
                    "The M20 listing cannot verify file contents."
                ),
            )
        if payload.macro_id in {"start_sd_print", "print_first_layer_sd"}:
            commands.update_sd_listing(printer.sd_entries)
            filename = payload.parameters.get("filename")
            listed_files = {
                entry["name"] for entry in printer.sd_entries if entry.get("kind") == "file"
            }
            if (
                printer.sd_refreshed_at is None
                or (utc_now() - printer.sd_refreshed_at).total_seconds() > 30
                or filename not in listed_files
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Refresh the read-only SD listing and choose a currently listed file.",
                )
            if commands.is_sd_file_ambiguous(filename):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "M20 returned duplicate file names; refresh the card or resolve "
                        "the duplicate before printing."
                    ),
                )
        try:
            operation = await command_executor.start(
                preview_service=commands,
                macro_id=payload.macro_id,
                parameters=payload.parameters,
                confirmation=payload.confirmation,
                snapshot=printer.status_payload(),
                io_lock=printer.io_lock,
                snapshot_provider=printer.status_payload,
                commissioning=payload.mode == "commissioning" or payload.commissioning,
                axis_at_home=payload.axis_at_home,
                retreat_from_home=payload.retreat_from_home,
            )
        except PreviewError as exc:
            commands.record_error(payload.macro_id, str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ExecutionBlocked as exc:
            commands.record_event(payload.macro_id, "execution_blocked", str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event(payload.macro_id, "execution_queued")
        return operation

    @app.post(
        "/api/control/capabilities/{capability_id}/retry",
        dependencies=[Depends(require_auth)],
    )
    async def retry_capability(
        capability_id: str, payload: CapabilityRetryRequest, request: Request
    ) -> dict[str, Any]:
        await require_control_token(request)
        if payload.confirmation != capability_id:
            raise HTTPException(status_code=422, detail=f"Type {capability_id} exactly.")
        if not (
            payload.attended and payload.no_competing_clients and payload.power_switch_reachable
        ):
            raise HTTPException(
                status_code=422,
                detail="Confirm attendance, clear client access, and reachable power switch.",
            )
        if command_executor.active_id is not None or workflows.active is not None:
            raise HTTPException(
                status_code=409, detail="An operation or guided workflow is active."
            )
        try:
            _require_fresh_state(printer.status_payload(), "idle")
            return capability_registry_runtime.safe_to_retry(capability_id, payload.confirmation)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Capability not found.") from exc
        except (ValueError, ExecutionBlocked) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/control/operations/{operation_id}/observe",
        dependencies=[Depends(require_auth)],
    )
    async def observe_operation(
        operation_id: str, payload: OperatorObservationRequest, request: Request
    ) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin and not _origin_matches(request.headers.get("host", ""), origin):
            raise HTTPException(status_code=403, detail="Request origin is not allowed")
        try:
            await require_control_token(request)
            result = command_executor.record_operator_observation(
                operation_id, payload.observed_success, printer.status_payload()
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Operation not found") from exc
        except ExecutionBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        commands.record_event(
            result["macro_id"],
            (
                "physical_result_observed"
                if payload.observed_success
                else "unexpected_result_reported"
            ),
        )
        await hub.publish(dashboard_snapshot())
        return result

    @app.get("/api/control/operations/{operation_id}", dependencies=[Depends(require_auth)])
    async def control_operation(operation_id: str) -> dict[str, Any]:
        operation = command_executor.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="Operation not found")
        if operation["state"] not in {"queued", "running"} and printer.observed_at is not None:
            completed_at = datetime.fromisoformat(operation["updated_at"])
            if printer.observed_at > completed_at:
                operation["telemetry_after"] = printer.telemetry
                operation["telemetry_after_observed_at"] = iso(printer.observed_at)
                operation["telemetry_observation"] = (
                    "Read-only telemetry sampled after transport acknowledgment; "
                    "this still does not confirm physical completion."
                )
        return operation

    @app.post(
        "/api/control/operations/{operation_id}/cancel",
        dependencies=[Depends(require_control_auth)],
    )
    async def control_cancel(operation_id: str) -> dict[str, Any]:
        try:
            result = await command_executor.cancel(operation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Operation not found") from exc
        commands.record_event(result["macro_id"], "cancellation_requested")
        return result

    @app.get("/api/control/audit", dependencies=[Depends(require_auth)])
    async def control_audit() -> dict[str, Any]:
        return {
            "entries": list(commands.audit),
            "operations": command_executor.history(),
            "execution_enabled": command_executor.execution_enabled,
        }

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi(request: Request) -> dict[str, Any]:
        await require_auth(request)
        return app.openapi()

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    async def docs(request: Request) -> HTMLResponse:
        await require_auth(request)
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(openapi_url="/openapi.json", title="Neptune Observatory API")

    @app.websocket("/ws/printer")
    async def websocket_printer(websocket: WebSocket) -> None:
        peer = websocket.client.host if websocket.client else ""
        if auth_required or not _is_loopback(peer):
            cookie = websocket.cookies.get(SESSION_COOKIE, "")
            auth = websocket.headers.get("authorization", "")
            bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not (
                hmac.compare_digest(cookie, auth_token or "")
                or hmac.compare_digest(bearer, auth_token or "")
            ):
                await websocket.close(code=4401, reason="Authentication required")
                return
        if not _origin_matches(websocket.headers.get("host", ""), websocket.headers.get("origin")):
            await websocket.close(code=4403, reason="Origin is not allowed")
            return
        await websocket.accept()
        queue = hub.subscribe()
        try:
            await websocket.send_json(dashboard_snapshot())
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            hub.unsubscribe(queue)

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Neptune Observatory on the local network.")
    parser.add_argument("--host", default=os.getenv("NEPTUNE_OBSERVATORY_BIND_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("NEPTUNE_OBSERVATORY_PORT", "8000"))
    )
    args = parser.parse_args()
    if not _is_loopback(args.host) and not os.getenv("NEPTUNE_OBSERVATORY_TOKEN"):
        parser.error("set NEPTUNE_OBSERVATORY_TOKEN before binding beyond localhost")
    import uvicorn

    uvicorn.run(
        create_app(bind_host=args.host, auth_token=os.getenv("NEPTUNE_OBSERVATORY_TOKEN")),
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
