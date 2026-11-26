#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
unit=neptune-kiosk.service
dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
case "${1:-start}" in
 install)
  mkdir -p "$dir" "${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  cat > "$dir/$unit" <<EOF
[Unit]
Description=Neptune Observatory touchscreen kiosk
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$root
ExecStart=/bin/bash "$root/deploy/raspberry-pi/kiosk/launch.sh"
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

EOF
  systemctl --user daemon-reload
  ;;
 enable)
  bash "$0" install
  cat > "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/neptune-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Neptune Observatory kiosk
Exec=/bin/bash "$root/deploy/raspberry-pi/kiosk/manage.sh" start
Terminal=false
EOF
  bash "$0" start
  ;;
 disable)
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/neptune-kiosk.desktop"
  systemctl --user stop "$unit"
  ;;
 start)
  bash "$0" install
  systemctl --user start "$unit"
  if ! tmux has-session -t =neptune-kiosk 2>/dev/null; then
    tmux new-session -d -s neptune-kiosk 'journalctl --user -u neptune-kiosk.service -n 100 -f'
  fi
  echo "Kiosk started in background; use make kiosk-logs to attach."
  ;;
 stop) systemctl --user stop "$unit" ;;
 restart) systemctl --user restart "$unit" ;;
 status) systemctl --user status "$unit" --no-pager ;;
 logs)
  if ! tmux has-session -t =neptune-kiosk 2>/dev/null; then
    tmux new-session -d -s neptune-kiosk 'journalctl --user -u neptune-kiosk.service -n 100 -f'
  fi
  if [[ -n ${TMUX:-} ]]; then
    tmux switch-client -t =neptune-kiosk
  else
    tmux attach-session -t =neptune-kiosk
  fi
  ;;
 *) echo "Unknown kiosk action" >&2; exit 2 ;;
esac
