#!/usr/bin/env bash
set -Eeuo pipefail
[[ $(id -u) != 0 ]] || { echo 'Run as the commissioned desktop user' >&2; exit 1; }
[[ $(uname -s) == Linux && -d /sys/class/input ]] || { echo 'Raspberry Pi desktop required' >&2; exit 1; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 "$root/deploy/raspberry-pi/configure_appliance.py"
bash "$root/deploy/raspberry-pi/rotate-display.sh" 90 --persist
journal_config='[Journal]
Storage=persistent
SplitMode=uid
SystemMaxUse=128M
MaxRetentionSec=14day'
if ! printf '%s\n' "$journal_config" | cmp -s - /etc/systemd/journald.conf.d/60-neptune.conf; then
  sudo install -d -m 0755 /etc/systemd/journald.conf.d
  printf '%s\n' "$journal_config" | sudo tee /etc/systemd/journald.conf.d/60-neptune.conf >/dev/null
  sudo systemctl restart systemd-journald
fi
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo journalctl --flush
