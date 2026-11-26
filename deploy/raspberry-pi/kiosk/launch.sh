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
url="http://localhost:8000"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv="$HOME/.local/share/neptune-kiosk/venv"
[[ -x "$venv/bin/python" ]] || { echo "Run make kiosk-setup first" >&2; exit 1; }
echo "Waiting for Observatory on $WAYLAND_DISPLAY..."
for ((i=0;i<60;i++)); do
  if curl -fsS --max-time 2 -o /dev/null "$url"; then
    echo "Launching authenticated local kiosk..."
    exec "$venv/bin/python" "$script_dir/authenticated.py"
  fi
  sleep 2
done
echo "Observatory did not become ready: $url" >&2
exit 1
