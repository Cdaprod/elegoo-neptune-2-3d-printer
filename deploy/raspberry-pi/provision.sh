#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Provisioning failed at line $LINENO" >&2' ERR
source /etc/os-release
[[ $(dpkg --print-architecture) == arm64 ]] || { echo "ARM64 required"; exit 1; }
codename="${VERSION_CODENAME:?Missing OS codename}"
sudo apt-get update
sudo apt-get install -y git curl ca-certificates gnupg jq rsync tmux htop python3 python3-venv make usbutils evtest libinput-tools wlr-randr cage chromium
for p in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
  if [[ $(dpkg-query -W -f='${Status}' "$p" 2>/dev/null || true) == "install ok installed" ]]; then
    echo "Conflicting package $p installed; resolve manually"; exit 1
  fi
done
curl -fsSL "https://download.docker.com/linux/debian/dists/$codename/Release" >/dev/null
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $codename
Components: stable
Architectures: arm64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
echo "Use sudo docker, or explicitly grant trusted user Docker group access (root-equivalent)."
