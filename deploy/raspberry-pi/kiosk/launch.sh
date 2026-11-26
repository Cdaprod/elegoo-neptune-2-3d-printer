#!/usr/bin/env bash
set -Eeuo pipefail
[[ $(id -u) != 0 ]] || { echo "Run as desktop user, not root" >&2; exit 1; }
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z ${WAYLAND_DISPLAY:-} ]]; then
  sockets=()
  for s in "$XDG_RUNTIME_DIR"/wayland-*; do
    [[ ! -S "$s" ]] || sockets+=("$s")
  done
  [[ ${#sockets[@]} == 1 ]] || { echo "Set WAYLAND_DISPLAY to the active desktop socket" >&2; exit 1; }
  export WAYLAND_DISPLAY="$(basename "${sockets[0]}")"
fi
url="${NEPTUNE_KIOSK_URL:-http://localhost:8000}"
for ((i=0;i<60;i++)); do
  if curl -fsS --max-time 2 -o /dev/null "$url"; then
    exec chromium --ozone-platform=wayland --kiosk --no-first-run --no-default-browser-check --disable-session-crashed-bubble --user-data-dir="$HOME/.config/neptune-kiosk" "$url"
  fi
  sleep 2
done
echo "Observatory did not become ready: $url" >&2
exit 1
