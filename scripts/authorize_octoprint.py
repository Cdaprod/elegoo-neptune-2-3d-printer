#!/usr/bin/env python3
"""Obtain an OctoPrint-issued application key with its supported approval workflow.

No OctoPrint passwords or generated keys are printed, logged or passed as CLI args.
Only the local OctoPrint instance on 127.0.0.1 is contacted. Printer TCP is untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from setup_env import ENV, parse_env, replace_values, save

APP_NAME = "Neptune Observatory"
API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
PLACEHOLDER = "replace-with-octoprint-application-api-key"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_url(port: str) -> str:
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("OCTOPRINT_HOST_PORT must be a valid TCP port")
    return f"http://127.0.0.1:{int(port)}"


def safe_plugin_url(base: str, value: str) -> str:
    url = urljoin(base, value)
    parsed, expected = urlsplit(url), urlsplit(base)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or not parsed.path.startswith("/plugin/appkeys/")
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("OctoPrint returned an unexpected application-key URL")
    return url


def request(opener, url: str, *, method: str = "GET", payload=None, key=None):
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-Api-Key"] = key
    req = Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with opener.open(req, timeout=4) as response:
            status, location = response.status, response.headers.get("Location", "")
            raw = response.read(16385)
    except HTTPError as exc:
        status, location = exc.code, exc.headers.get("Location", "")
        raw = exc.read(16385)
        exc.close()
    if len(raw) > 16384:
        raise ValueError("OctoPrint returned an oversized authorization response")
    try:
        body = json.loads(raw) if raw else {}
    except (UnicodeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        raise ValueError("OctoPrint returned an invalid authorization object")
    return status, location, body


def valid_key(opener, base: str, key: str) -> bool:
    if not API_KEY_PATTERN.fullmatch(key) or key == PLACEHOLDER:
        return False
    try:
        status, _, _ = request(opener, base + "/api/connection", key=key)
        return status == 200
    except (OSError, URLError, ValueError, TimeoutError):
        return False


def authorize(opener, base: str, *, sleep=time.sleep, open_browser=webbrowser.open) -> str:
    # Startup readiness: this is a local HTTP check, not a printer connection.
    for _ in range(60):
        try:
            status, _, _ = request(opener, base + "/plugin/appkeys/probe")
            if status == 204:
                break
            # Initial OctoPrint startup can temporarily return 404 before the
            # plugin routes are registered. Retry until the readiness window ends.
        except (OSError, URLError, TimeoutError):
            pass
        sleep(1)
    else:
        raise RuntimeError(
            "OctoPrint Application Keys plugin did not become available; "
            "confirm OctoPrint is running and its bundled plugin is enabled"
        )

    status, location, response = request(
        opener, base + "/plugin/appkeys/request", method="POST", payload={"app": APP_NAME}
    )
    if status != 201:
        raise RuntimeError(f"OctoPrint refused the application-key request (HTTP {status})")

    app_token = response.get("app_token")
    if not isinstance(app_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", app_token):
        raise RuntimeError("OctoPrint authorization did not provide a valid request token")
    # Prefer the response's Location, but restrict it to the same local plugin endpoint.
    poll_url = safe_plugin_url(base, location or f"/plugin/appkeys/request/{app_token}")
    if urlsplit(poll_url).path != f"/plugin/appkeys/request/{app_token}":
        raise RuntimeError("Unexpected OctoPrint authorization polling endpoint")
    auth_dialog = response.get("auth_dialog")
    if not isinstance(auth_dialog, str) or not auth_dialog:
        raise RuntimeError("OctoPrint did not return an approval dialog")
    dialog_url = safe_plugin_url(base, auth_dialog)
    print("OctoPrint requires a one-time approval for Neptune Observatory.")
    print("Open this local link and approve the request with your OctoPrint account:")
    print(dialog_url, flush=True)
    try:
        open_browser(dialog_url)
    except (OSError, RuntimeError):
        pass

    for _ in range(180):
        status, _, result = request(opener, poll_url)
        if status == 200:
            key = result.get("api_key")
            if not isinstance(key, str) or not API_KEY_PATTERN.fullmatch(key):
                raise RuntimeError("OctoPrint approval returned an invalid application key")
            if not valid_key(opener, base, key):
                raise RuntimeError("OctoPrint issued a key, but its API access check failed")
            return key
        if status == 404:
            raise RuntimeError("OctoPrint authorization denied or expired")
        if status != 202:
            raise RuntimeError(f"OctoPrint authorization returned HTTP {status}")
        sleep(1)
    raise RuntimeError("OctoPrint approval was not completed; retry make up")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    if not ENV.exists():
        parser.error("root .env is missing; run make setup")
    content = ENV.read_text(encoding="utf-8")
    settings = parse_env(content)
    key = settings.get("OCTOPRINT_API_KEY", "").strip()
    if args.check_config:
        if not API_KEY_PATTERN.fullmatch(key) or key == PLACEHOLDER:
            parser.error("OctoPrint application key not provisioned; run make up")
        print("OctoPrint key is configured (value hidden)")
        return 0
    try:
        base = local_url(os.environ.get("OCTOPRINT_HOST_PORT") or settings.get("OCTOPRINT_HOST_PORT", "5001"))
        opener = build_opener(NoRedirects())
        if valid_key(opener, base, key):
            print("Existing Neptune Observatory application key validated with OctoPrint.")
            return 0
        print("No valid OctoPrint application key; requesting a new one.")
        key = authorize(opener, base)
        save(replace_values(content, {"OCTOPRINT_API_KEY": key}))
        print("OctoPrint-issued application key verified and saved to private root .env.")
        return 0
    except (OSError, URLError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"OctoPrint authorization failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
