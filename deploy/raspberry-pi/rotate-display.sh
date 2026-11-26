#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$(realpath "$0")")/profiles/waveshare-7.9.env"
[[ $(id -u) != 0 ]] || { echo "Run as desktop user, not root"; exit 1; }
transform="$DISPLAY_TRANSFORM"
output="$DISPLAY_OUTPUT"
persist=false
list=false
for arg in "$@"; do
  case "$arg" in
    90|270|normal) transform="$arg";;
    HDMI-A-[0-9]*) output="$arg";;
    --persist) persist=true;;
    --list) list=true;;
    *) echo "Usage: $0 [90|270|normal] [HDMI-A-N] [--persist] or --list"; exit 1;;
  esac
done
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
if [[ -z ${WAYLAND_DISPLAY:-} ]]; then
  sockets=()
  for s in "$XDG_RUNTIME_DIR"/wayland-*; do [[ ! -S "$s" ]] || sockets+=("$s"); done
  [[ ${#sockets[@]} == 1 ]] || { echo "Select WAYLAND_DISPLAY explicitly"; exit 1; }
  export WAYLAND_DISPLAY="$(basename "${sockets[0]}")"
fi
command -v wlr-randr >/dev/null || { echo "Install wlr-randr first"; exit 1; }
if $list; then exec wlr-randr; fi
[[ "$output" =~ ^HDMI-A-[0-9]+$ ]] || exit 1
wlr-randr --output "$output" --transform "$transform"
if $persist; then
  # XDG autostart runs inside the maintenance desktop session, not Cage.
  dir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  mkdir -p "$dir"
  cat > "$dir/neptune-display.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Neptune display orientation
Exec=/usr/bin/wlr-randr --output $output --transform $transform
Terminal=false
EOF
  echo "Saved desktop login rotation to $dir/neptune-display.desktop"
else
  echo "Session-only change."
fi
wlr-randr
echo "Verify all four touch corners."
