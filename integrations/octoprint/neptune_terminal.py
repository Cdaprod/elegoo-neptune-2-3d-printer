"""Read-only, bounded printer communication capture for Neptune Observatory.

OctoPrint retains exclusive ownership of the printer connection.
The send/receive hooks only append to an in-memory ring; they neither
write G-code nor modify any incoming firmware response.
"""

from collections import deque
from datetime import datetime, timezone
from threading import Lock
import re

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions


_REDACT = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|password|token|secret)\b\s*[:=]\s*)"
    r"(?!\[REDACTED\])(?:[^\s,;]+)"
)


class NeptuneTerminalPlugin(octoprint.plugin.SimpleApiPlugin):
    """In-memory serial console; no file I/O or socket I/O in comm hooks."""

    def __init__(self):
        self._events = deque(maxlen=500)
        self._sequence = 0
        self._lock = Lock()

    def is_api_protected(self):
        return True

    def is_api_adminonly(self):
        return False

    def get_api_commands(self):
        # No POST actions. This plugin cannot send printer commands.
        return {}

    def _capture(self, direction, raw):
        if not isinstance(raw, str) or not raw:
            return
        # Only bounded formatting and an in-memory append in the comm thread.
        message = _REDACT.sub(r"\1[REDACTED]", raw.strip()[:600])
        if not message:
            return
        with self._lock:
            self._sequence += 1
            self._events.append(
                {
                    "sequence": self._sequence,
                    "direction": direction,
                    "text": ("Send: " if direction == "send" else "Recv: ") + message,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "source": "octoprint_live_terminal_hook",
                }
            )

    def sent(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        try:
            self._capture("send", cmd)
        except Exception:
            # Monitoring must never interfere with printing.
            pass
        return None

    def received(self, comm_instance, line, *args, **kwargs):
        try:
            self._capture("recv", line)
        except Exception:
            pass
        # Critical: returning None would suppress firmware replies in OctoPrint.
        return line

    def on_api_get(self, request):
        if not Permissions.MONITOR.can():
            flask.abort(403)
        try:
            limit = int(request.args.get("limit", "250"))
        except (TypeError, ValueError):
            flask.abort(400)
        if not 1 <= limit <= 500:
            flask.abort(400)
        with self._lock:
            latest = list(self._events)[-limit:]
            sequence = self._sequence
        return flask.jsonify(
            {
                "status": "available",
                "source": "octoprint_live_terminal_hook",
                "events": latest,
                "limit": limit,
                "latest_sequence": sequence,
                "note": (
                    "Captured directly from OctoPrint's send/receive hooks "
                    "since the Neptune Terminal plugin was loaded. "
                    "No printer commands are issued by this API."
                ),
            }
        )


__plugin_name__ = "Neptune Observatory Terminal"
__plugin_description__ = "Bounded, read-only OctoPrint serial communications bridge."
__plugin_pythoncompat__ = ">=3.8,<4"


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__
    __plugin_implementation__ = NeptuneTerminalPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.sent,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.received,
    }
