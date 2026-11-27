#!/usr/bin/env python3
"""Authenticated wireless handoff receiver. Publishes files only; never prints."""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 50 * 1024 * 1024
NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_. -]{0,160}\.(?:gcode|gco|gc|g)", re.IGNORECASE
)


class Receiver(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, token):
        if len(token) < 32:
            raise ValueError("Receiver token must contain at least 32 characters")
        self.root = Path(root)
        self.partial = self.root / "partial"
        self.completed = self.root / "completed"
        for directory in (self.partial, self.completed):
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.partial.stat().st_dev != self.completed.stat().st_dev:
            raise ValueError(
                "Partial and completed directories must share a filesystem"
            )
        self.token = token
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    # Never log URLs, authorization headers or uploaded content.
    def log_message(self, *_args):
        pass

    def respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(
            header.encode(), ("Bearer " + self.server.token).encode()
        )

    def do_GET(self):
        if not self.authorized():
            return self.respond(401, {"error": "Authentication required"})
        if self.path != "/health":
            return self.respond(404, {"error": "Unknown endpoint"})
        self.respond(
            200,
            {
                "service": "lilbitdrop_handoff",
                "schema_version": 1,
                "completed_publication": "atomic_rename",
                "printing": False,
            },
        )

    def do_POST(self):
        self.connection.settimeout(15)
        if not self.authorized():
            return self.respond(401, {"error": "Authentication required"})
        url = urlsplit(self.path)
        filename = parse_qs(url.query).get("filename", [""])[0]
        if url.path != "/receive":
            return self.respond(404, {"error": "Unknown endpoint"})
        if not NAME.fullmatch(filename) or ".." in filename:
            return self.respond(422, {"error": "Supply a plain G-code filename"})
        if self.headers.get("Transfer-Encoding"):
            return self.respond(400, {"error": "Use one explicit Content-Length"})
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            return self.respond(411, {"error": "Content-Length required"})
        size = int(lengths[0])
        if not 0 < size <= MAX_BYTES:
            return self.respond(413, {"error": "File must be 1 byte to 50 MiB"})
        if shutil.disk_usage(self.server.root).free < size + 512 * 1024 * 1024:
            return self.respond(507, {"error": "Insufficient receiver storage reserve"})
        if self.headers.get_content_type() != "application/octet-stream":
            return self.respond(415, {"error": "Send raw G-code bytes"})
        expected = self.headers.get("X-Content-SHA256", "").lower()
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            return self.respond(422, {"error": "X-Content-SHA256 required"})
        if not self.server.slots.acquire(blocking=False):
            return self.respond(429, {"error": "Receiver busy"})
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix="receive-", suffix=".part", dir=self.server.partial
            )
            os.fchmod(fd, 0o640)
            digest = hashlib.sha256()
            remaining = size
            with os.fdopen(fd, "wb") as output:
                while remaining:
                    block = self.rfile.read(min(65536, remaining))
                    if not block:
                        raise ValueError("Interrupted upload")
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
                output.flush()
                os.fsync(output.fileno())
            if not hmac.compare_digest(digest.hexdigest(), expected):
                return self.respond(422, {"error": "SHA-256 mismatch"})
            # Stable name preserves sender filename and makes retries idempotent.
            target = self.server.completed / (expected + "_" + filename)
            os.replace(temporary, target)
            descriptor = os.open(self.server.completed, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.respond(
                201,
                {
                    "schema_version": 1,
                    "sha256": expected,
                    "filename": filename,
                    "size": size,
                    "status": "received_pending_ingestion",
                    "printing": False,
                },
            )
        except (OSError, ValueError):
            self.respond(400, {"error": "Upload interrupted or could not be persisted"})
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            self.server.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--root", default="/srv/neptune/incoming")
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    token_path = Path(args.token_file)
    if token_path.stat().st_mode & 0o077:
        parser.error("Token file must be private (0600)")
    token = token_path.read_text().strip()
    server = Receiver((args.host, args.port), args.root, token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
