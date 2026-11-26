#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$(realpath "$0")")/profiles/waveshare-7.9.env"
[[ $(id -u) != 0 ]] || { echo "Run as the desktop user, not root"; exit 1; }
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [[ -z ${WAYLAND_DISPLAY:-} ]]; then
  sockets=()
  for s in "$XDG_RUNTIME_DIR"/wayland-*; do [[ ! -S "$s" ]] || sockets+=("$s"); done
  [[ ${#sockets[@]} == 1 ]] || { echo "Select the desktop WAYLAND_DISPLAY explicitly"; exit 1; }
  export WAYLAND_DISPLAY="$(basename "${sockets[0]}")"
fi
command -v wlr-randr >/dev/null || { echo "Install wlr-randr first"; exit 1; }
if [[ ${1:-} == --list ]]; then exec wlr-randr; fi
transform="${1:-$DISPLAY_TRANSFORM}"
case "$transform" in 90|270|normal) ;; *) echo "Use 90, 270, normal or --list"; exit 1;; esac
output="${2:-}"
if [[ -z "$output" ]]; then
  mapfile -t outputs < <(wlr-randr | awk '/^HDMI-A-[0-9]+ / {print $1}')
  [[ ${#outputs[@]} == 1 ]] || { echo "Specify HDMI output explicitly"; exit 1; }
  output="${outputs[0]}"
fi
wlr-randr --output "$output" --transform "$transform"
wlr-randr
echo "Session-only change. Verify all four touch corners."
