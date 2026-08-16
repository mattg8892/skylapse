#!/usr/bin/env bash
# Skylapse installer for an existing Raspberry Pi OS / Debian (Bookworm+).
# The flashable SD image (pi-gen) is the recommended path once released.
#
# Installs IN PLACE from this git checkout: the venv is created beside this
# script and the systemd units are pointed at it. Updating a rig is then
# `git pull && sudo systemctl restart skylapse-daemon`. Copying the tree to
# /opt (as this script used to) strands the checkout and breaks that workflow,
# which is the wrong trade on a project whose users are expected to track a
# fast-moving repo.
#
# Every step detects what is already there and skips it, so re-running after a
# `git pull` is safe and cheap.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "Run via sudo from your normal login account (needs a non-root owner"
    echo "for the checkout, the venv and the capture process)."
    exit 1
fi
as_user() { sudo -u "$RUN_USER" "$@"; }

echo "==> Checkout: $ROOT"
echo "==> Service user: $RUN_USER"

echo "==> System packages"
# nodejs/npm build the React frontend the API serves; ffmpeg renders the dawn
# timelapse. Neither was in the original list, and both are hard requirements
# for features DESIGN.md calls implemented.
MISSING=""
for p in python3-venv python3-picamera2 network-manager git ffmpeg nodejs npm; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
    echo "    installing:$MISSING"
    apt-get update
    apt-get install -y $MISSING
else
    echo "    all present, skipping"
fi

echo "==> Data directories"
# Paths per DESIGN.md. Owned by the service user so the capture loop and API
# never need root. /run/skylapse is NOT created here — the units declare
# RuntimeDirectory=skylapse and systemd recreates it with the right owner on
# every boot, which is the only way it survives a /run tmpfs wipe.
install -d -o "$RUN_USER" -g "$RUN_USER" /etc/skylapse
install -d -o "$RUN_USER" -g "$RUN_USER" /var/lib/skylapse
install -d -o "$RUN_USER" -g "$RUN_USER" /var/lib/skylapse/images

echo "==> Python environment"
if [ -x "$ROOT/venv/bin/python" ]; then
    echo "    venv present"
    # Existence is not enough. picamera2 is apt-only with no wheel, so a venv
    # built without --system-site-packages can never see it — and the Pi camera
    # driver's probe() then returns False on hardware that is working perfectly.
    # A venv created by hand before running this script is exactly that case,
    # so repair the flag rather than skipping over it.
    if grep -q "^include-system-site-packages = false" "$ROOT/venv/pyvenv.cfg" 2>/dev/null; then
        echo "    repairing: enabling system site-packages (needed for picamera2)"
        as_user sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' \
            "$ROOT/venv/pyvenv.cfg"
    fi
else
    # --system-site-packages so python3-picamera2 (apt-only, no wheel) is importable.
    as_user python3 -m venv --system-site-packages "$ROOT/venv"
fi
as_user "$ROOT/venv/bin/pip" install --quiet --upgrade pip
# Editable: the running service imports straight from the checkout, so a
# `git pull` takes effect on restart with no reinstall.
as_user "$ROOT/venv/bin/pip" install --quiet -e "$ROOT[zwo]"

echo "==> ZWO SDK"
if /sbin/ldconfig -p | grep -q libASICamera2; then
    echo "    libASICamera2 already installed, skipping"
else
    echo "    libASICamera2 NOT found."
    echo "    ZWO's download portal is browser-only and cannot be scripted."
    echo "    Fetch the aarch64 binary + udev rules, e.g. from the INDI mirror:"
    echo "      https://github.com/indilib/indi-3rdparty/tree/master/libasi"
    echo "      armv8/libASICamera2.bin -> /usr/local/lib/libASICamera2.so.<ver>"
    echo "      (symlink .so.1 and .so, then run ldconfig)"
    echo "      99-asi.rules            -> /etc/udev/rules.d/"
    echo "      then: udevadm control --reload-rules && udevadm trigger"
    echo "    Continuing — the daemon will use a Pi camera if one is present."
fi
if [ -f /etc/udev/rules.d/99-asi.rules ]; then
    echo "    udev rules already present, skipping"
fi

echo "==> Web frontend"
# The API mounts web/dist; without a build it serves nothing but /api.
if [ -d "$ROOT/web/node_modules" ]; then
    echo "    node_modules present, skipping npm install"
else
    as_user npm --prefix "$ROOT/web" install
fi
as_user npm --prefix "$ROOT/web" run build

echo "==> systemd units"
for unit in skylapse-daemon skylapse-api skylapse-netwatch; do
    sed -e "s|@SKYLAPSE_ROOT@|$ROOT|g" -e "s|@SKYLAPSE_USER@|$RUN_USER|g" \
        "$ROOT/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload

# All three, netwatch included: verified on hardware 2026-08-16, down to the
# client-connected freeze with a real phone attached. It was held back until
# then because its failure mode is taking down a working Wi-Fi link, and on a
# headless rig that costs you the box.
systemctl enable --now skylapse-daemon skylapse-api skylapse-netwatch

echo "==> Done."
echo "    capture: systemctl status skylapse-daemon"
echo "    frames:  journalctl -u skylapse-daemon -f"
echo "    web:     http://$(hostname).local/"
echo "    network: systemctl status skylapse-netwatch"
