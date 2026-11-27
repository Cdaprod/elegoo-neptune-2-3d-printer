#!/usr/bin/env python3
"""Read-only USB identity/ownership preflight; optional explicit deployment configuration.

Never opens the serial port, kills an owner, unpauses a container or sends G-code.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from setup_env import ENV, parse_env, replace_values, save


def identity(device, sys_root=Path("/sys/class/tty")):
    tty = Path(device).resolve(strict=True).name
    node = (sys_root / tty / "device").resolve(strict=True)
    for ancestor in (node, *node.parents):
        vendor, product = ancestor / "idVendor", ancestor / "idProduct"
        if vendor.is_file() and product.is_file():
            pair = (
                vendor.read_text().strip().lower()
                + ":"
                + product.read_text().strip().lower()
            )
            driver = (node / "driver").resolve().name
            if pair != "1a86:7523" or driver not in {"ch341", "ch341-uart"}:
                raise RuntimeError(
                    "USB identity/driver differs from verified CH340 1a86:7523 ch341 profile"
                )
            return {"vid_pid": pair, "driver": driver, "tty": tty}
    raise RuntimeError("USB VID/PID unavailable; refusing to guess a serial device")


def ownership(device, run=None):
    if run is None:
        def run(command, **kwargs):
            if command[0] == "fuser" and os.geteuid() != 0:
                command = ["sudo", "-n", *command]
            return subprocess.run(command, **kwargs)

    owners = run(
        ["fuser", str(Path(device).resolve())],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if owners.returncode not in (0, 1):
        raise RuntimeError("Cannot inspect USB owners with fuser")
    pids = {int(token) for token in owners.stdout.split() if token.isdigit()}
    allowed = set()
    paused = []
    for name in ("neptune-octoprint", "neptune-observatory"):
        inspection = run(
            ["docker", "inspect", "--format", "{{.State.Paused}}", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if inspection.returncode and "No such" not in getattr(inspection, "stderr", ""):
            raise RuntimeError("Cannot inspect container ownership/paused state; check Docker access")
        if inspection.returncode == 0 and inspection.stdout.strip() == "true":
            paused.append(name)
        if name == "neptune-octoprint" and inspection.returncode == 0:
            processes = run(
                ["docker", "top", name, "-eo", "pid"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if processes.returncode == 0:
                allowed = {
                    int(token) for token in processes.stdout.split() if token.isdigit()
                }
    if pids - allowed:
        raise RuntimeError(
            "USB is owned outside OctoPrint (PIDs "
            + ",".join(map(str, sorted(pids - allowed)))
            + "). Close picocom normally; do not kill or open another serial client."
        )
    if paused:
        raise RuntimeError(
            "Paused containers: "
            + ", ".join(paused)
            + ". After closing picocom and checking the printer, unpause explicitly before deployment."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure-usb", action="store_true")
    args = parser.parse_args()
    content = ENV.read_text() if ENV.exists() else ""
    values = {
        **parse_env(content),
        **{
            key: value
            for key, value in os.environ.items()
            if key in {"OCTOPRINT_TRANSPORT", "OCTOPRINT_SERIAL_DEVICE"}
        },
    }
    if not args.configure_usb and values.get("OCTOPRINT_TRANSPORT", "wifi") != "usb":
        return 0
    if sys.platform != "linux":
        parser.error("USB appliance deployment requires the commissioned Linux host")
    try:
        candidates = []
        for path in Path("/dev/serial/by-id").glob("*"):
            try:
                identity(path)
                candidates.append(path)
            except (OSError, RuntimeError):
                continue
        selected = values.get("OCTOPRINT_SERIAL_DEVICE")
        if selected:
            identity(selected)
            matches = [
                path
                for path in candidates
                if path.resolve() == Path(selected).resolve()
            ]
        else:
            matches = candidates
        if len(matches) != 1:
            raise RuntimeError(
                "Specify one verified stable /dev/serial/by-id device; discovery is missing or ambiguous"
            )
        device = matches[0]
        ownership(device)
        if args.configure_usb:
            save(
                replace_values(
                    content,
                    {
                        "OCTOPRINT_TRANSPORT": "usb",
                        "OCTOPRINT_SERIAL_DEVICE": str(device),
                        "COMPOSE_FILE": "compose.yaml:integrations/octoprint/compose.usb.yaml",
                        "OCTOPRINT_MKS_AUTO_CONNECT": "false",
                    },
                )
            )
        print(
            "USB preflight passed: CH340 1a86:7523 ch341; stable device "
            + str(device)
            + "; 115200 8N1. No serial port was opened."
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
