#!/usr/bin/env python3
"""Authenticate the local kiosk without keyboard input or exposing token in argv."""
import os
import stat
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from setup_env import parse_env

def main():
    env = ROOT / ".env"
    info = env.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("Kiosk requires a private .env owned by the desktop user")
    token = parse_env(env.read_text()).get("NEPTUNE_OBSERVATORY_TOKEN", "")
    if not token or token.startswith("replace-with-"):
        raise SystemExit("Run make setup first")
    url = "http://localhost:8000"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(Path.home() / ".config/neptune-kiosk"),
            executable_path="/usr/bin/chromium",
            headless=False,
            args=["--ozone-platform=wayland", "--kiosk", "--no-first-run",
                  "--no-default-browser-check", "--password-store=basic"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            if page.url.rstrip("/") != url:
                raise RuntimeError("Unexpected kiosk navigation")
            field = page.get_by_label("Access token", exact=True)
            button = page.get_by_role("button", name="Connect dashboard", exact=True)
            # Existing authenticated sessions may not display the dialog.
            try:
                field.wait_for(state="visible", timeout=10000)
            except Exception:
                print("No access dialog detected; verify dashboard on display.", flush=True)
            else:
                if page.url.rstrip("/") != url:
                    raise RuntimeError("Unexpected kiosk origin")
                field.fill(token)
                button.click()
                field.wait_for(state="hidden", timeout=15000)
                print("Kiosk access dialog dismissed.", flush=True)
            while not page.is_closed():
                time.sleep(1)
        finally:
            context.close()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Browser automation exceptions may include sensitive input values.
        print("Kiosk bootstrap failed; check local service and display.", file=sys.stderr)
        sys.exit(1)
