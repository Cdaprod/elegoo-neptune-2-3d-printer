#!/usr/bin/env python3
"""Authenticated iOS Share Sheet upload bridge for LilBitDrop.

Run on the Raspberry Pi behind an explicitly configured TLS reverse proxy.
This bridge never stages or prints a received file. Native AirDrop is not used.
"""

import argparse
import hashlib
import hmac
import http.client
import json
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

# Host scripts use the same dependency-free policy shipped with Observatory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mks-wifi/src/mks_wifi_bridge"))
import artifact_policy  # noqa: E402

MAX_BYTES = artifact_policy.MAX_BYTES


class ShareServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, receiver_port=8091):
        if address[0] != "127.0.0.1":
            raise ValueError("Share bridge requires IPv4 loopback")
        if len(token) < 32:
            raise ValueError("Token must be at least 32 characters")
        self.token = token
        self.receiver_port = receiver_port
        self.slots = threading.BoundedSemaphore(2)
        self.connections = threading.BoundedSemaphore(8)
        super().__init__(address, ShareHandler)

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


class ShareHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass

    def respond(self, status, message):
        payload = json.dumps(message).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.connection.settimeout(15)
        # This service must be bound to loopback behind a reviewed TLS gateway.
        url = urlsplit(self.path)
        if url.path != "/share":
            return self.respond(404, {"error": "Unknown endpoint"})
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + self.server.token).encode()):
            return self.respond(401, {"error": "Authentication required"})
        filename = parse_qs(url.query).get("filename", [""])[0]
        try:
            artifact_policy.transport_kind(filename)
        except ValueError as error:
            return self.respond(422, {"error": str(error)})
        if self.headers.get("Transfer-Encoding"):
            return self.respond(400, {"error": "Explicit Content-Length required"})
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 10:
            return self.respond(411, {"error": "Content-Length required"})
        length = int(lengths[0])
        if not 0 < length <= MAX_BYTES:
            return self.respond(413, {"error": "File must be 1 byte to 50 MiB"})
        if self.headers.get_content_type() != "application/octet-stream":
            return self.respond(415, {"error": "Send raw file bytes"})
        if not self.server.slots.acquire(blocking=False):
            return self.respond(429, {"error": "Receiver busy"})
        try:
            digest = hashlib.sha256()
            remaining = length
            deadline = time.monotonic() + 300
            with tempfile.TemporaryFile() as body:
                while remaining:
                    budget = deadline - time.monotonic()
                    if budget <= 0:
                        raise TimeoutError("Upload timed out")
                    self.connection.settimeout(min(15, budget))
                    block = self.rfile.read1(min(65536, remaining))
                    if not block:
                        raise ValueError("Incomplete upload")
                    body.write(block)
                    digest.update(block)
                    remaining -= len(block)
                body.seek(0)
                conn = http.client.HTTPConnection("127.0.0.1", self.server.receiver_port, timeout=30)
                try:
                    conn.putrequest("POST", "/receive?" + urlencode({"filename": filename}))
                    conn.putheader("Authorization", "Bearer " + self.server.token)
                    conn.putheader("Content-Type", "application/octet-stream")
                    conn.putheader("Content-Length", str(length))
                    conn.putheader("X-Content-SHA256", digest.hexdigest())
                    conn.putheader("X-LilBitDrop-Transport", "ios-share")
                    conn.endheaders()
                    while True:
                        block = body.read(65536)
                        if not block:
                            break
                        conn.send(block)
                    result = conn.getresponse()
                    payload = result.read(65537)
                    if len(payload) > 65536:
                        raise ValueError("Receiver response too large")
                    receipt = json.loads(payload)
                    if result.status == 422:
                        return self.respond(422, {"error": "Receiver rejected artifact"})
                    if (
                        result.status != 201
                        or receipt.get("sha256") != digest.hexdigest()
                        or receipt.get("size") != length
                        or receipt.get("filename") != filename
                        or receipt.get("status") != "received_pending_ingestion"
                        or receipt.get("printing") is not False
                    ):
                        raise ValueError("Receiver did not confirm safe receipt")
                    self.respond(201, receipt)
                finally:
                    conn.close()
        except (OSError, ValueError, TimeoutError, json.JSONDecodeError):
            self.respond(502, {"error": "Upload rejected or receiver unavailable"})
        finally:
            self.server.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--receiver-port", type=int, default=8091)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("Share bridge must bind to loopback behind a TLS gateway")
    path = Path(args.token_file)
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        parser.error("Token file must be regular and private (0600)")
    token = path.read_text().strip()
    server = ShareServer((args.host, args.port), token, args.receiver_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
