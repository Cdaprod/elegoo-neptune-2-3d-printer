#!/usr/bin/env python3
"""Authenticated wireless handoff receiver. Publishes files only; never prints."""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Host scripts use the same dependency-free policy shipped with Observatory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mks-wifi/src/mks_wifi_bridge"))
import artifact_policy  # noqa: E402

MAX_BYTES = artifact_policy.MAX_BYTES


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
            raise ValueError("Partial and completed directories must share a filesystem")
        self.token = token
        self.read_timeout = 15
        self.transfer_timeout = 300
        self.slots = threading.BoundedSemaphore(2)
        self.connections = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.read_timeout)

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
        return hmac.compare_digest(header.encode(), ("Bearer " + self.server.token).encode())

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
        self.connection.settimeout(self.server.read_timeout)
        if not self.authorized():
            return self.respond(401, {"error": "Authentication required"})
        url = urlsplit(self.path)
        filename = parse_qs(url.query).get("filename", [""])[0]
        if url.path != "/receive":
            return self.respond(404, {"error": "Unknown endpoint"})
        try:
            artifact_policy.transport_kind(filename)
        except ValueError as error:
            return self.respond(422, {"error": str(error)})
        transport = self.headers.get("X-LilBitDrop-Transport", "receiver")
        if transport not in {"receiver", "ssh", "ios-share"}:
            return self.respond(422, {"error": "Unknown transport provenance"})
        if self.headers.get("Transfer-Encoding"):
            return self.respond(400, {"error": "Use one explicit Content-Length"})
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            return self.respond(411, {"error": "Content-Length required"})
        if len(lengths[0]) > 10:
            return self.respond(413, {"error": "Content-Length exceeds limit"})
        size = int(lengths[0])
        if not 0 < size <= MAX_BYTES:
            return self.respond(413, {"error": "File must be 1 byte to 50 MiB"})
        if shutil.disk_usage(self.server.root).free < size + 512 * 1024 * 1024:
            return self.respond(507, {"error": "Insufficient receiver storage reserve"})
        if self.headers.get_content_type() != "application/octet-stream":
            return self.respond(415, {"error": "Send raw engineering artifact bytes"})
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
            deadline = time.monotonic() + self.server.transfer_timeout
            with os.fdopen(fd, "wb") as output:
                while remaining:
                    budget = deadline - time.monotonic()
                    if budget <= 0:
                        raise ValueError("Transfer deadline exceeded")
                    self.connection.settimeout(min(self.server.read_timeout, budget))
                    block = self.rfile.read1(min(65536, remaining))
                    if not block:
                        raise ValueError("Interrupted upload")
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
                output.flush()
                os.fsync(output.fileno())
            if not hmac.compare_digest(digest.hexdigest(), expected):
                return self.respond(422, {"error": "SHA-256 mismatch"})
            try:
                kind = artifact_policy.validate_transport_content(temporary, filename)
            except (OSError, ValueError) as error:
                return self.respond(422, {"error": str(error)})
            # Transport-specific paths preserve independent provenance across sources.
            destination = self.server.completed
            if transport != "receiver":
                destination = destination / transport
                destination.mkdir(mode=0o750, exist_ok=True)
            target = destination / (expected + "_" + filename)
            try:
                os.link(
                    temporary, target
                )  # Atomic create-if-absent; never replaces readers' inode.
            except FileExistsError:
                try:
                    existing = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(existing, "rb") as source:
                        before = os.fstat(source.fileno())
                        valid = (
                            stat.S_ISREG(before.st_mode)
                            and before.st_size == size
                            and hmac.compare_digest(
                                hashlib.file_digest(source, "sha256").hexdigest(),
                                expected,
                            )
                        )
                        after = os.fstat(source.fileno())
                    current = target.lstat()
                    valid = (
                        valid
                        and (before.st_ino, before.st_mtime_ns, before.st_size)
                        == (after.st_ino, after.st_mtime_ns, after.st_size)
                        and (current.st_dev, current.st_ino) == (after.st_dev, after.st_ino)
                    )
                except OSError:
                    valid = False
                if not valid:
                    return self.respond(
                        409, {"error": "Existing completed artifact failed integrity"}
                    )
            for directory in {destination, self.server.completed}:
                descriptor = os.open(directory, os.O_RDONLY)
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
                    "artifact_kind": kind,
                    "transport": transport,
                    "published_mtime_ns": target.stat().st_mtime_ns,
                    "canonical_archive": "pending",
                    "processing": "not_confirmed",
                    "gallery": "not_confirmed",
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
