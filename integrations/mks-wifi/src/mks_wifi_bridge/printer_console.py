"""Bounded read-only OctoPrint serial.log parser and terminal presentation.

OctoPrint is the sole owner of the native printer socket. The serial.log is
OctoPrint's record of Send:/Recv: lines, not a second live MKS connection.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LOG_BYTES = 2_200_000
MAX_LINES = 500
STAMP = re.compile(
    r"^(?P<stamp>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?)\s*[-:]?\s*(?P<body>.*)$"
)
MESSAGE = re.compile(r"(?<!\w)(?P<direction>Send|Recv):\s*(?P<payload>.*)$", re.IGNORECASE)
SENSITIVE = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|password|token|secret)\b\s*[:=]\s*)"
    r"(?!\[REDACTED\])(?:[^\s,;]+)"
)


def parse_serial_log(raw: bytes, *, limit: int = 200, truncated: bool = False) -> dict[str, Any]:
    """Return only real serial send/receive events, never fabricate printer replies."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LINES:
        raise ValueError("Terminal history limit must be between 1 and 500")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(events) >= limit:
            break
        candidate = line.strip()
        stamp = None
        stamped = STAMP.match(candidate)
        if stamped:
            stamp = stamped.group("stamp")
            candidate = stamped.group("body")
        match = MESSAGE.search(candidate)
        if not match:
            continue
        direction = match.group("direction").capitalize()
        message = match.group("payload").strip()
        if not message:
            continue
        message = SENSITIVE.sub(r"\1[REDACTED]", message)
        events.append(
            {
                "direction": direction.lower(),
                "text": f"{direction}: {message[:600]}",
                "observed_at": stamp,
                "source": "octoprint_serial_log",
            }
        )
    events.reverse()
    return {
        "status": "available",
        "source": "octoprint_serial_log",
        "events": events,
        "limit": limit,
        "truncated": truncated,
        "note": (
            "OctoPrint's logged serial communications, not independent printer telemetry. "
            "Only messages logged by OctoPrint are available; other clients may differ."
        ),
    }
