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


# Bounded, command-specific factory responses. Generic `ok` never completes a query.
FACTORY_COMMANDS = {
    "firmware": "M115",
    "configuration": "M503",
    "temperature": "M105",
    "coordinates": "M114",
    "list": "M20",
    "list_long": "M20 L",
    "status": "M27",
    "initialize": "M20",
    "select": "M23",
}
FACTORY_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_. ~-]{0,199}\.(?:gcode|gco|gc|g)", re.IGNORECASE | re.ASCII
)


def catalog_name(name):
    parts = name.replace("\\", "/").split("/")
    return bool(parts) and not any(
        not part
        or part.startswith(".")
        or part.casefold()
        in {"system volume information", "$recycle.bin", "lost+found"}
        for part in parts
    )


class FactoryTransaction:
    def __init__(self, operation, filename=None):
        self.operation, self.filename = operation, filename
        self.sent = False
        self.done = False
        self.data = {}
        self.lines = []
        self.byte_count = 0
        self.last_data = 0
        self.list_started = False
        self.error = None

    def feed(self, raw):
        if not self.sent or self.done:
            return
        line = raw.strip()
        if len(line) > 600:
            self.error = "Oversized factory response"
            return
        if re.search(r"(?:^|\s)T(?:0)?:[-+\d]", line) or 'enqueueing "M105"' in line:
            if self.operation == "temperature" and "T:" in line:
                self.data = {
                    "raw_line": line,
                    "correlation": "observed_temperature_not_command_ack",
                }
                self.done = True
            return
        if self.operation == "firmware":
            match = re.search(
                r"(?:Firmware:|FIRMWARE_NAME:)\s*(.+)", line, re.IGNORECASE
            )
            if match:
                self.data["firmware"] = match.group(1)[:160]
            cap = re.fullmatch(r"Cap:([A-Z_0-9]+):([01])", line)
            if cap:
                self.data.setdefault("capabilities", {})[cap[1]] = cap[2] == "1"
            # This is the last capability in the operator-provided factory profile.
            if (
                self.data.get("firmware") == "Robin_nano_v2.1.6.b"
                and cap
                and cap[1] == "EMERGENCY_PARSER"
            ):
                self.done = True
        elif self.operation == "coordinates":
            if all(re.search(r"\b" + axis + r":\s*[-+\d.]", line) for axis in "XYZ"):
                self.data = {
                    "raw_line": line,
                    "position_kind": "firmware_reported_not_physical_measurement",
                }
                self.done = True
        elif self.operation in {"list", "list_long", "initialize"}:
            if line.casefold() == "begin file list":
                self.list_started = True
                self.data["entries"] = []
            elif self.list_started and line.casefold() == "end file list":
                self.done = True
            elif (
                self.list_started
                and line
                and not line.lower().startswith(("ok", "echo:", "busy:"))
            ):
                match = re.fullmatch(r"(.+?)\s+(\d+)(?:\s+(.+))?", line)
                name = match[1] if match else line
                if (
                    catalog_name(name)
                    and FACTORY_NAME.fullmatch(name)
                    and ".." not in name
                ):
                    if len(self.data["entries"]) >= 2000:
                        self.error = "Directory exceeds 2000 entries"
                        return
                    self.data["entries"].append(
                        {
                            "name": name,
                            "kind": "file",
                            "size_bytes": int(match[2]) if match else None,
                            "display_name": match[3] if match and match[3] else name,
                            "source": "factory_usb_m20",
                            "geometry_available": False,
                        }
                    )
        elif self.operation == "select":
            match = re.fullmatch(
                r"File opened:\s*(.+?)\s+Size:\s*(\d+)", line, re.IGNORECASE
            )
            if match and match[1].casefold() == self.filename.casefold():
                self.data = {
                    "filename": match[1],
                    "size_bytes": int(match[2]),
                    "selected": True,
                    "printing": None,
                }
            if line.casefold() == "file selected" and self.data.get("filename"):
                self.done = True
        elif self.operation == "status":
            match = re.fullmatch(r"SD printing byte\s+(\d+)/(\d+)", line, re.IGNORECASE)
            if match and int(match[1]) <= int(match[2]):
                self.data = {
                    "byte_position": int(match[1]),
                    "total_bytes": int(match[2]),
                    "printing": None,
                    "note": "A nonzero total does not prove an active print",
                }
                self.done = True
            elif line.casefold() == "not sd printing":
                self.data = {
                    "printing": False,
                    "byte_position": None,
                    "total_bytes": None,
                }
                self.done = True
        elif self.operation == "configuration":
            if re.match(r"^(?:echo:\s*)?[MG]\d+\s+[A-Z][-+\d]", line):
                self.byte_count += len(line)
                if self.byte_count > 32768 or len(self.lines) >= 200:
                    self.error = "Configuration exceeds bounded capture"
                    return
                self.lines.append(line)
                self.last_data = time.monotonic()
                self.data = {
                    "lines": self.lines,
                    "complete": False,
                    "completion": "bounded_configuration_snapshot_not_arbitrary_ok",
                }

    def poll(self):
        if (
            self.operation == "configuration"
            and self.lines
            and time.monotonic() - self.last_data >= 0.3
        ):
            self.done = True
        return self.done


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
        self._factory_pending = None
        self._factory_snapshot = {}
        self._rsd_session_expires = 0

    def serial_factory(
        self, comm_instance, port, baudrate, read_timeout, *args, **kwargs
    ):
        # This is OctoPrint's primary connection factory, not an additional client.
        if not isinstance(port, str) or not port.startswith(
            ("/dev/ttyUSB", "/dev/ttyACM", "/dev/serial/")
        ):
            return None
        import fcntl
        import serial
        import termios

        device = serial.Serial(
            port=port,
            baudrate=baudrate or 115200,
            timeout=read_timeout,
            exclusive=True,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        try:
            fcntl.ioctl(device.fileno(), termios.TIOCEXCL)
        except Exception:
            device.close()
            raise
        return device

    def get_settings_defaults(self):
        return {"enabled": False, "factory_queries": True}

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {
            "exchange": ["operation", "sequence", "session"],
            "factory": ["operation"],
        }

    def on_event(self, event, payload):
        if event in {"Disconnected", "Connecting", "Error"}:
            with self._condition:
                self._advertised = False
                self._factory_snapshot.clear()
                self._condition.notify_all()

    def capability(self, comm_instance, capability, enabled, *args, **kwargs):
        if capability == "NEPTUNE_SD_READ":
            self._advertised = enabled is True

    def sent(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        tags = kwargs.get("tags") or next(
            (item for item in args if isinstance(item, set)), set()
        )
        with self._condition:
            pending = self._factory_pending
            if (
                pending
                and pending["tag"] in tags
                and gcode
                == FACTORY_COMMANDS[pending["transaction"].operation].split()[0]
            ):
                pending["transaction"].sent = True

    def received(self, comm_instance, line, *args, **kwargs):
        if isinstance(line, str) and len(line) <= 600:
            with self._condition:
                for operation in ("firmware", "coordinates", "status", "temperature"):
                    observed = FactoryTransaction(operation)
                    observed.sent = True
                    observed.feed(line)
                    if observed.data:
                        previous = self._factory_snapshot.get(operation, {}).get(
                            "data", {}
                        )
                        self._factory_snapshot[operation] = {
                            "data": {**previous, **observed.data},
                            "observed_at": time.time(),
                        }
                pending = self._factory_pending
                if pending:
                    pending["transaction"].feed(line)
                    self._condition.notify_all()

        if isinstance(line, str) and line.startswith("RSD1 ") and len(line) <= 768:
            try:
                frame = json.loads(line[5:])
                with self._condition:
                    if (frame.get("q"), frame.get("op")) == self._expected:
                        self._reply = line
                        if frame.get("error") or frame.get("op") in {"CLOSE", "CANCEL"}:
                            self._rsd_session_expires = 0
                        elif frame.get("op") in {"OPEN", "READ"}:
                            self._rsd_session_expires = time.monotonic() + 15
                        self._condition.notify_all()
            except (ValueError, AttributeError):
                pass
        return line  # Never swallow serial acknowledgments or unrelated responses.

    def _usb_owner(self):
        connection = self._printer.get_current_connection()
        port = connection[1] if len(connection) > 1 else None
        return isinstance(port, str) and port.startswith(
            ("/dev/ttyUSB", "/dev/ttyACM", "/dev/cu.usb", "/dev/serial/")
        )

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
                "factory_usb": {
                    "available": self._usb_owner(),
                    "observations": self._factory_snapshot,
                },
                "provider": "printer_sd",
                "transport": "octoprint_usb_owner",
                "note": "Requires experimental RSD/1 firmware, idle USB connection and explicit enablement.",
            }
        )

    def on_api_command(self, command, data):
        if command == "factory":
            return self.factory_command(data)
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

    def factory_command(self, data):
        import uuid

        operation = data.get("operation")
        if operation not in FACTORY_COMMANDS:
            flask.abort(400)
        permission = (
            Permissions.CONTROL
            if operation in {"select", "initialize"}
            else Permissions.MONITOR
        )
        if not permission.can():
            flask.abort(403)
        if (
            not self._settings.get_boolean(["factory_queries"])
            or not self._usb_owner()
            or self._printer.get_state_id() != "OPERATIONAL"
        ):
            flask.abort(
                423, "Factory queries require the sole idle OctoPrint USB owner"
            )
        reported = (
            self._factory_snapshot.get("firmware", {}).get("data", {}).get("firmware")
        )
        if (
            operation != "firmware"
            and not self._advertised
            and reported != "Robin_nano_v2.1.6.b"
        ):
            flask.abort(
                423,
                "Identify the factory firmware with M115 before using its command profile",
            )
        filename = data.get("filename")
        if operation == "select" and (
            not isinstance(filename, str)
            or not FACTORY_NAME.fullmatch(filename)
            or ".." in filename
        ):
            flask.abort(400)
        if operation in {"select", "initialize"} and (
            data.get("attended") is not True
            or data.get("printer_idle_confirmed") is not True
            or data.get("confirmation")
            != ("SELECT " + filename if operation == "select" else "INITIALIZE SD")
        ):
            flask.abort(
                422,
                "Explicit attended idle confirmation required; selection never starts printing",
            )
        if (
            operation in {"select", "initialize"}
            and time.monotonic() < self._rsd_session_expires
        ):
            flask.abort(409, "An RSD read session is active")
        if not self._exchange_lock.acquire(False):
            flask.abort(409, "Another printer transaction is pending")
        try:
            with self._condition:
                transaction = FactoryTransaction(operation, filename)
                tag = "neptune:factory:" + uuid.uuid4().hex
                self._factory_pending = {"transaction": transaction, "tag": tag}
                tags = {"source:plugin", "plugin:neptune_sd_read", tag}
                if operation == "select":
                    self._printer.select_file(
                        filename, sd=True, printAfterSelect=False, tags=tags
                    )
                else:
                    commands = (
                        ["M21", "M20"]
                        if operation == "initialize"
                        else [FACTORY_COMMANDS[operation]]
                    )
                    self._printer.commands(commands, tags=tags)
                deadline = time.monotonic() + 8
                while not transaction.poll():
                    if transaction.error:
                        flask.abort(422, transaction.error)
                    if (
                        self._printer.get_state_id() != "OPERATIONAL"
                        or not self._usb_owner()
                    ):
                        flask.abort(409, "USB owner or machine state changed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        flask.abort(
                            504,
                            "No command-specific response; an unrelated ok is not completion",
                        )
                    self._condition.wait(min(remaining, 0.1))
                result = {
                    "data": transaction.data,
                    "observed_at": time.time(),
                    "source": "factory_usb_" + FACTORY_COMMANDS[operation].split()[0],
                    "operation": operation,
                }
                self._factory_snapshot[operation] = result
                return flask.jsonify(result)
        finally:
            with self._condition:
                self._factory_pending = None
            self._exchange_lock.release()


__plugin_name__ = "Neptune Read-only Native SD"
__plugin_pythoncompat__ = ">=3.8,<4"


def __plugin_load__():
    global __plugin_implementation__, __plugin_hooks__
    __plugin_implementation__ = NeptuneSdReadPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.firmware.capabilities": __plugin_implementation__.capability,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.sent,
        "octoprint.comm.transport.serial.factory": __plugin_implementation__.serial_factory,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.received,
    }
