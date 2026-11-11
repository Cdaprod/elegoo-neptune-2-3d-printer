#!/usr/bin/env python3
"""Initialize the root Compose .env without printing credentials or overwriting existing secrets."""
from __future__ import annotations

import argparse
from discover_mks import discover
import getpass
import os
import re
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
DEFAULT_PRINTER_IP = "192.168.0.95"  # Known static MKS WiFi endpoint for this deployment.
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_env(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def replace_values(content: str, replacements: dict[str, str]) -> str:
    lines = content.splitlines(keepends=True)
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in replacements:
            lines[i] = f"{key}={replacements[key]}\n"
            seen.add(key)
    for key, value in replacements.items():
        if key not in seen:
            lines.append(f"{key}={value}\n")
    return "".join(lines)


def save(content: str) -> None:
    # Atomic replace: credentials are never exposed in a world-readable temporary file.
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".neptune-env-", dir=ROOT)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, ENV)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-token", action="store_true")
    parser.add_argument("--rotate-token", action="store_true")
    args = parser.parse_args()
    if args.show_token:
        if not ENV.exists():
            parser.error("run make setup first")
        token = parse_env(ENV.read_text()).get("NEPTUNE_OBSERVATORY_TOKEN", "")
        if not token:
            parser.error("no Observatory token configured")
        print(token)
        return 0

    if not EXAMPLE.exists():
        parser.error("missing root .env.example")
    content = ENV.read_text() if ENV.exists() else EXAMPLE.read_text()
    values = parse_env(content)
    updates: dict[str, str] = {}

    if args.rotate_token:
        updates["NEPTUNE_OBSERVATORY_TOKEN"] = secrets.token_urlsafe(32)
        save(replace_values(content, updates))
        print("Generated and saved a new Observatory token (run make token to display).")
        return 0

    # Precedence: explicit shell override > saved non-placeholder value > known
    # static printer IP. Network discovery is opt-in, never a setup prerequisite.
    host = os.environ.get("OCTOPRINT_MKS_WIFI_HOST", "").strip()
    if not host:
        host = values.get("OCTOPRINT_MKS_WIFI_HOST", "").strip()
    if host in ("", "printer.local"):
        host = DEFAULT_PRINTER_IP
    if host.lower() == "discover":
        try:
            host = discover()
        except RuntimeError as exc:
            parser.error(str(exc))
    if not HOST_PATTERN.fullmatch(host):
        parser.error("printer address must be a hostname or IPv4 address without URL/port")
    updates["OCTOPRINT_MKS_WIFI_HOST"] = host
    print(f"Using configured MKS WiFi endpoint: {host}")

    key = values.get("OCTOPRINT_API_KEY", "")
    if not key or key == "replace-with-octoprint-application-api-key":
        if not sys.stdin.isatty():
            parser.error("OctoPrint API key missing; run make setup in an interactive terminal")
        key = getpass.getpass("Existing OctoPrint application API key (hidden): ").strip()
        if not key:
            parser.error("OctoPrint API key is required; generate one in OctoPrint first")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            parser.error("invalid OctoPrint API key characters")
        updates["OCTOPRINT_API_KEY"] = key

    token = values.get("NEPTUNE_OBSERVATORY_TOKEN", "")
    if not token or token == "replace-with-long-random-local-observatory-token":
        if sys.stdin.isatty():
            choice = input("Observatory token: [Enter] secure random / [p] custom phrase: ").strip().lower()
        else:
            choice = ""
        if choice == "p":
            token = getpass.getpass("Custom token phrase (hidden): ")
            if len(token) < 16 or any(ch in token for ch in "\r\n"):
                parser.error("custom token phrase must have at least 16 characters and no newlines")
            if not re.fullmatch(r"[A-Za-z0-9_.@!#%+=:,/-]+", token):
                parser.error("use a phrase without spaces, quotes, dollars, or shell metacharacters")
        elif choice == "":
            token = secrets.token_urlsafe(32)
        else:
            parser.error("choose Enter or p")
        updates["NEPTUNE_OBSERVATORY_TOKEN"] = token

    save(replace_values(content, updates))
    print("Root .env configured (permissions 0600). Credentials were not printed.")
    print("Next: make check && make up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
