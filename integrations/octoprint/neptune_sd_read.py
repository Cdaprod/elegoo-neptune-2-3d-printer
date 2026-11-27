"""Experimental RSD/1 bridge through OctoPrint's existing USB serial owner.

No raw command endpoint, sockets, serial ports, filesystem writes or print API calls.
Disabled on startup unless explicitly enabled in OctoPrint plugin settings.
"""

import json
import re
import time
from threading import Condition, Lock

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions

OPS = ("CAPABILITIES", "LIST", "STAT", "OPEN", "READ", "CLOSE", "CANCEL")
ALIAS = re.compile(r"[A-Z0-9_~-]{1,8}\.(?:GCO|GC|G)", re.IGNORECASE)


class NeptuneSdReadPlugin(
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        self._advertised = False
        self._condition = Condition()
        self._exchange_lock = Lock()
        self._expected = None
        self._reply = None

    def get_settings_defaults(self):
        return {"enabled": False}

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {"exchange": ["operation", "sequence", "session"]}

    def on_event(self, event, payload):
        if event in {"Disconnected", "Connecting", "Error"}:
            with self._condition:
                self._advertised = False
                self._condition.notify_all()

    def capability(self, comm_instance, capability, enabled, *args, **kwargs):
        if capability == "NEPTUNE_SD_READ":
            self._advertised = enabled is True

    def received(self, comm_instance, line, *args, **kwargs):
        if isinstance(line, str) and line.startswith("RSD1 ") and len(line) <= 768:
            try:
                frame = json.loads(line[5:])
                with self._condition:
                    if (frame.get("q"), frame.get("op")) == self._expected:
                        self._reply = line
                        self._condition.notify_all()
            except (ValueError, AttributeError):
                pass
        return line  # Never swallow serial acknowledgments or unrelated responses.

    def _ready(self):
        connection = self._printer.get_current_connection()
        port = connection[1] if len(connection) > 1 else None
        return (
            self._settings.get_boolean(["enabled"])
            and self._advertised
            and self._printer.get_state_id() == "OPERATIONAL"
            and isinstance(port, str)
            and port.startswith(
                ("/dev/ttyUSB", "/dev/ttyACM", "/dev/cu.usb", "/dev/serial/")
            )
        )

    def on_api_get(self, request):
        if not Permissions.MONITOR.can():
            flask.abort(403)
        return flask.jsonify(
            {
                "version": 1,
                "can_read": bool(self._ready()),
                "advertised": self._advertised,
                "provider": "printer_sd",
                "transport": "octoprint_usb_owner",
                "note": "Requires experimental RSD/1 firmware, idle USB connection and explicit enablement.",
            }
        )

    def on_api_command(self, command, data):
        if not Permissions.CONTROL.can():
            flask.abort(403)
        if command != "exchange" or not self._ready():
            flask.abort(
                423, "RSD/1 unavailable: idle USB owner, capability and opt-in required"
            )
        if set(data) - {
            "command",
            "operation",
            "sequence",
            "session",
            "offset",
            "count",
            "filename",
        }:
            flask.abort(400)
        operation = data.get("operation")
        if operation not in OPS:
            flask.abort(400)
        values = []
        for key, maximum in (
            ("sequence", 0xFFFFFFFF),
            ("session", 0xFFFFFFFF),
            ("offset", 0xFFFFFFFF),
            ("count", 192),
        ):
            value = data.get(key, 0)
            if type(value) is not int or not 0 <= value <= maximum:
                flask.abort(400)
            values.append(value)
        name = data.get("filename", "")
        if (
            not isinstance(name, str)
            or (operation in {"STAT", "OPEN"} and not ALIAS.fullmatch(name))
            or (name and operation not in {"STAT", "OPEN"})
        ):
            flask.abort(400)
        wire = "M794 V1 O{} Q{} S{} A{} B{}".format(OPS.index(operation), *values)
        if name:
            wire += " P" + "".join(f"{ord(c):03d}" for c in name.upper())
        if len(wire) > 95:
            flask.abort(400)
        if not self._exchange_lock.acquire(False):
            flask.abort(409, "Another SD request is pending")
        try:
            with self._condition:
                if not self._ready():
                    flask.abort(423)
                self._expected = (values[0], operation)
                self._reply = None
                self._printer.commands(
                    [wire], tags={"source:plugin", "plugin:neptune_sd_read"}
                )
                deadline = time.monotonic() + 5
                while self._reply is None and self._ready():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        flask.abort(
                            504, "SD reply timed out; no automatic command retry"
                        )
                    self._condition.wait(remaining)
                if self._reply is None:
                    flask.abort(409, "Printer state changed during read")
                return flask.jsonify({"reply": self._reply})
        finally:
            with self._condition:
                self._expected = None
            self._exchange_lock.release()


__plugin_name__ = "Neptune Read-only Native SD"
__plugin_pythoncompat__ = ">=3.8,<4"


def __plugin_load__():
    global __plugin_implementation__, __plugin_hooks__
    __plugin_implementation__ = NeptuneSdReadPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.firmware.capabilities": __plugin_implementation__.capability,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.received,
    }
