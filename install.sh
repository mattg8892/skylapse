#!/usr/bin/env bash
# Skylapse installer for an existing Raspberry Pi OS (Bookworm+).
# The flashable SD image (pi-gen) is the recommended path once released.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }

echo "==> Installing system packages"
apt-get update
apt-get install -y python3-venv python3-picamera2 network-manager git

echo "==> Creating skylapse user + directories"
id skylapse &>/dev/null || useradd -r -s /usr/sbin/nologin -G video skylapse
mkdir -p /opt/skylapse /etc/skylapse /var/lib/skylapse/images
chown -R skylapse:skylapse /var/lib/skylapse

echo "==> Installing Skylapse"
python3 -m venv /opt/skylapse/venv --system-site-packages
/opt/skylapse/venv/bin/pip install --upgrade pip
/opt/skylapse/venv/bin/pip install "$(dirname "$0")[zwo]"

echo "==> Fetching ZWO SDK"
# TODO: download libASICamera2.so from ZWO's developer page and install udev rules.
echo "    (manual for now — see README hardware notes)"

echo "==> Installing services"
cp "$(dirname "$0")"/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now skylapse-daemon skylapse-netwatch skylapse-api

echo "==> Done. Open http://skylapse.local"
