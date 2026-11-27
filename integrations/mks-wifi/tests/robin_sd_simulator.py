"""Deterministic RSD/1 firmware fixture, never connected to physical hardware."""

import hashlib
import json
import zlib

from mks_wifi_bridge.robin_sd_protocol import CHUNK, command
from mks_wifi_bridge.robin_sd_read import MAX_SD_BYTES


class Firmware:
    def __init__(self, files, *, state="idle"):
        self.files = dict(files)
        self.state = state
        self.mounted = True
        self.sessions = {}
        self.next_session = 1
        self.history = []
        self.corrupt = False
        self.epoch = 1

    async def capabilities(self):
        return {"can_read": self.mounted and self.state == "idle", "simulated": True}

    def token(self, name):
        return hashlib.sha256(self.files[name] + str(self.epoch).encode()).hexdigest()

    async def exchange(self, request):
        wire = command(**request)
        self.history.append(wire)
        op, q, s = request["operation"], request["sequence"], request["session"]
        reply = {"v": 1, "op": op, "q": q, "s": s}
        error = None
        if not self.mounted:
            self.sessions.clear()
            error = "MEDIA_REMOVED"
        elif self.state != "idle":
            self.sessions.clear()
            error = "BUSY"
        elif op == "CAPABILITIES":
            reply.update(chunk=CHUNK, max_bytes=MAX_SD_BYTES, hash="sha256")
        elif op == "LIST":
            names = sorted(self.files)
            cursor = request.get("offset", 0)
            if cursor >= len(names):
                reply["end"] = True
            else:
                reply.update(name=names[cursor], size=len(self.files[names[cursor]]))
        elif op in {"STAT", "OPEN"}:
            name = request.get("filename")
            if name not in self.files:
                error = "NOT_FOUND"
            else:
                reply.update(size=len(self.files[name]), token=self.token(name))
                if op == "OPEN":
                    if self.sessions:
                        error = "BUSY"
                    else:
                        s = self.next_session
                        self.next_session += 1
                        self.sessions[s] = {
                            "name": name,
                            "offset": 0,
                            "q": q,
                            "token": self.token(name),
                        }
                        reply["s"] = s
        elif s not in self.sessions:
            error = "SESSION"
        else:
            session = self.sessions[s]
            if q <= session["q"]:
                error = "SEQUENCE"
            elif self.token(session["name"]) != session["token"]:
                del self.sessions[s]
                error = "CHANGED"
            elif op in {"CLOSE", "CANCEL"}:
                if op == "CLOSE":
                    reply["sha256"] = hashlib.sha256(self.files[session["name"]]).hexdigest()
                del self.sessions[s]
            elif op == "READ":
                offset, count = request.get("offset", 0), request.get("count", 0)
                if offset != session["offset"] or not 0 < count <= CHUNK:
                    error = "OFFSET"
                else:
                    data = self.files[session["name"]][offset : offset + count]
                    reply.update(
                        offset=offset, data=data.hex(), crc=zlib.crc32(data), token=session["token"]
                    )
                    if self.corrupt:
                        reply["crc"] ^= 1
                    session.update(offset=offset + len(data), q=q)
        if error:
            reply["error"] = error
        return "RSD1 " + json.dumps(reply, separators=(",", ":"))
