"""Read-only MKS WiFi probe using the verified public ``mkswifi`` API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from mkswifi import MksWifiClient, MksWifiConnectionError

from .protocol import READ_ONLY_COMMANDS, SD_LIST_COMMANDS, telemetry_payload

DEFAULT_HOST = "printer.local"
DEFAULT_PORT = 8080
DEFAULT_TIMEOUT = 2.5


async def probe_printer(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Read temperatures/state and M115 identification; no writes or controls."""
    report: dict[str, Any] = {
        "target": {"host": host, "port": port, "transport": "raw TCP"},
        "connection": {"reachable": False},
        "telemetry": None,
        "errors": [],
        "safety": {
            "commands": list(READ_ONLY_COMMANDS),
            "optional_sd_listing_command": list(SD_LIST_COMMANDS),
            "no_arbitrary_commands": True,
            "no_printer_controls": True,
            "mks_single_client_warning": (
                "MKS firmware may replace an existing TCP client when this probe connects."
            ),
        },
    }
    client = MksWifiClient(host, port=port, timeout=timeout)
    try:
        status = await client.get_status()
    except MksWifiConnectionError as exc:
        report["errors"].append({"stage": "status", "message": str(exc)})
        return report

    report["connection"] = {"reachable": True}
    try:
        firmware = await client.get_firmware()
    except MksWifiConnectionError as exc:
        firmware = None
        report["errors"].append({"stage": "firmware_identification", "message": str(exc)})
    report["telemetry"] = telemetry_payload(status, firmware)
    return report


async def list_sd_root(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Request the documented read-only Marlin M20 root listing, once."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        writer.write(b"M20\r\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        chunks: list[bytes] = []
        deadline = asyncio.get_running_loop().time() + timeout
        received = 0
        while received < 32768:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except TimeoutError:
                break
            if not line:
                break
            chunks.append(line)
            received += len(line)
            if b"end file list" in line.lower():
                break
        response = b"".join(chunks)
        raw = response.decode("utf-8", errors="replace").strip()
        complete = "end file list" in raw.lower()
        return {
            "command": "M20",
            "status": "complete_listing" if complete else "partial_or_no_response",
            "raw_response": raw,
            "note": ("The listing is complete only when the End file list marker is received."),
        }
    finally:
        writer.close()
        await writer.wait_closed()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only telemetry probe for MKS WiFi.")
    parser.add_argument("--host", default=os.getenv("MKS_WIFI_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=_env_int("MKS_WIFI_PORT", DEFAULT_PORT))
    parser.add_argument(
        "--timeout", type=float, default=_env_float("MKS_WIFI_TIMEOUT", DEFAULT_TIMEOUT)
    )
    parser.add_argument("--json", action="store_true", help="print structured JSON")
    parser.add_argument(
        "--list-sd", action="store_true", help="send one read-only M20 listing request"
    )
    parser.add_argument(
        "--confirm-single-client-risk",
        action="store_true",
        help="acknowledge MKS may replace another active TCP client",
    )
    return parser


def _human_output(report: dict[str, Any]) -> str:
    if not report["connection"]["reachable"]:
        messages = "; ".join(error["message"] for error in report["errors"])
        return f"Unreachable: {messages or 'no response'}"
    telemetry = report["telemetry"]
    lines = [f"MKS WiFi at {report['target']['host']}:{report['target']['port']}"]
    for key, label, unit in (
        ("nozzle_temperature", "Nozzle", "°C"),
        ("bed_temperature", "Bed", "°C"),
    ):
        field = telemetry[key]
        if field["status"] == "available":
            lines.append(f"{label}: {field['value']}{unit} / target {field['target_c']}{unit}")
        else:
            lines.append(f"{label}: unsupported or not reported")
    for key, label in (("printer_state", "State"), ("firmware_identification", "M115 ID")):
        field = telemetry[key]
        value = field["value"] if field["value"] is not None else "unsupported or not reported"
        lines.append(f"{label}: {value}")
    active = telemetry["print_active"]["value"]
    lines.append(f"Print active: {active if active is not None else 'unknown'}")
    if report["errors"]:
        lines.extend(f"Warning: {e['stage']}: {e['message']}" for e in report["errors"])
    lines.append(report["safety"]["mks_single_client_warning"])
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    if args.port < 1 or args.port > 65535:
        print("error: port must be in 1..65535", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("error: timeout must be positive", file=sys.stderr)
        return 2
    if not args.confirm_single_client_risk:
        print(
            "error: this MKS module may replace another TCP client; pass "
            "--confirm-single-client-risk only after ensuring no other host is connected",
            file=sys.stderr,
        )
        return 2
    try:
        report = await asyncio.wait_for(
            probe_printer(args.host, args.port, args.timeout), timeout=2 * args.timeout + 1
        )
    except TimeoutError:
        report = {
            "target": {"host": args.host, "port": args.port, "transport": "raw TCP"},
            "connection": {"reachable": False},
            "telemetry": None,
            "errors": [{"stage": "probe", "message": "overall deadline exceeded"}],
            "safety": {"commands": list(READ_ONLY_COMMANDS)},
        }
    if args.list_sd and report["connection"]["reachable"]:
        try:
            report["sd_listing"] = await asyncio.wait_for(
                list_sd_root(args.host, args.port, args.timeout), timeout=args.timeout + 1
            )
        except (OSError, TimeoutError) as exc:
            report["sd_listing"] = {
                "command": "M20",
                "status": "error",
                "error": str(exc),
            }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_human_output(report))
    return 0 if report["connection"]["reachable"] else 1


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
