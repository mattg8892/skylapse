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

# SKYLAPSE_IMAGE_BUILD=1 is the SD image build (image/build.sh), running inside
# a chroot with no systemd, no SUDO_USER and nothing to start. It changes
# exactly two things: the service user is named rather than inherited, and
# units are enabled without being started.
IMAGE_BUILD="${SKYLAPSE_IMAGE_BUILD:-0}"
RUN_USER="${SKYLAPSE_USER:-${SUDO_USER:-}}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "Run via sudo from your normal login account (needs a non-root owner"
    echo "for the checkout, the venv and the capture process), or set"
    echo "SKYLAPSE_USER to the account the services should run as."
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
# build-essential/python3-dev are for pidng, which PyPI ships as a source
# tarball with no wheel — so the DNG writer needs a compiler at install time.
# Everything else resolves to an aarch64 wheel. This was found by checking a
# working rig and noticing build-essential was marked *manual* there: it had
# been installed by hand months earlier, so the installer had never actually
# been run anywhere without it, and would have failed on the first genuinely
# fresh card.
# iw and rfkill are what netwatch shells out to for the radio's mode and its
# regulatory domain. network-manager brings nmcli but not those, and Pi OS Lite
# ships neither — which on the first SD image meant netwatch could not raise
# its access point, so a camera with no Wi-Fi could not be reached at all.
# curl is what the ZWO SDK installer fetches with. Pi OS ships it, but the
# helper's failure without it is a button in the web UI that does nothing.
for p in python3-venv python3-dev build-essential python3-picamera2 \
         network-manager iw rfkill git curl ffmpeg nodejs npm; do
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
#
# The [zwo] extra is the Python bindings only — a small pure-Python package, no
# vendor code. They go in unconditionally so that installing ZWO support later
# is one download rather than a rebuild of the venv on a camera in the field.
#
# [dewheater] for the same reason, and because leaving it out was worse than a
# rebuild: the sensor probe is raw smbus2 and works without it, so the card
# reported "sensor found" on a camera that could never take a single reading.
# A half-installed feature that looks installed is the expensive kind.
as_user "$ROOT/venv/bin/pip" install --quiet -e "$ROOT[zwo,dewheater]"

echo "==> Persistent logs"
# Pi OS journald defaults to Storage=auto, which is persistent only if
# /var/log/journal exists -- and it does not. So the journal lives in /run and
# every reboot erases it. On a camera with no terminal that is the difference
# between diagnosing a failure and guessing at it, and it bit this project
# repeatedly: the machine stops, it gets power-cycled to recover it, and the
# act of recovering it destroys the only record of why.
#
# Capped inside the helper, because this is an SD card and logging must never
# be the thing that wears it out.
"$ROOT/scripts/skylapse-admin" logs-persist ||     echo "    (could not make the journal persistent; logs will not survive a reboot)"

echo "==> ZWO SDK (optional)"
# Not installed here, and not installed by default anywhere: Skylapse targets Pi
# camera modules, and ZWO is a best-effort second. The library also cannot be
# redistributed, so it is fetched on request — from Settings -> Cameras, which
# is the path a flashed card has and this one may as well share rather than
# maintaining a second way to do the same thing.
if /sbin/ldconfig -p | grep -q libASICamera2; then
    echo "    libASICamera2 already installed"
else
    echo "    not installed. Pi camera modules need nothing; if you have a ZWO"
    echo "    camera, install support from Settings -> Cameras in the web UI,"
    echo "    or run: sudo $ROOT/scripts/skylapse-admin zwo-sdk"
fi

echo "==> Web frontend"
# The API mounts web/dist; without a build it serves nothing but /api.
if [ -d "$ROOT/web/node_modules" ]; then
    echo "    node_modules present, skipping npm install"
else
    as_user npm --prefix "$ROOT/web" install
fi
as_user npm --prefix "$ROOT/web" run build

echo "==> privileged helper"
# The service user needs root for exactly three things: writing a camera
# overlay into the boot config, restarting its own units after an update, and
# rebooting. One script, a closed argument list, one sudoers file — rather than
# a wildcard rule, which would be root with extra steps. Validated before
# installing, because a malformed sudoers file locks everyone out of sudo.
chmod 755 "$ROOT/scripts/skylapse-admin"
sed -e "s|@SKYLAPSE_ROOT@|$ROOT|g" -e "s|@SKYLAPSE_USER@|$RUN_USER|g" \
    "$ROOT/systemd/skylapse.sudoers" > /tmp/skylapse.sudoers
if visudo -cqf /tmp/skylapse.sudoers; then
    install -m 440 -o root -g root /tmp/skylapse.sudoers /etc/sudoers.d/skylapse
else
    echo "    refusing to install a malformed sudoers file"
    exit 1
fi
rm -f /tmp/skylapse.sudoers

echo "==> systemd units"
for unit in skylapse-daemon skylapse-api skylapse-netwatch; do
    sed -e "s|@SKYLAPSE_ROOT@|$ROOT|g" -e "s|@SKYLAPSE_USER@|$RUN_USER|g" \
        "$ROOT/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
# All three, netwatch included: verified on hardware 2026-08-16, down to the
# client-connected freeze with a real phone attached. It was held back until
# then because its failure mode is taking down a working Wi-Fi link, and on a
# headless rig that costs you the box.
UNITS="skylapse-daemon skylapse-api skylapse-netwatch"
if [ "$IMAGE_BUILD" = "1" ]; then
    # A chroot has no running systemd, so daemon-reload and --now both fail.
    # Enabling is only symlink work, and does apply.
    systemctl enable $UNITS
else
    systemctl daemon-reload
    systemctl enable --now $UNITS
fi

if [ "$IMAGE_BUILD" = "1" ]; then
    echo "==> Image build: installed and enabled, not started."
    exit 0
fi

echo "==> Done."
echo "    capture: systemctl status skylapse-daemon"
echo "    frames:  journalctl -u skylapse-daemon -f"
echo "    web:     http://$(hostname).local/"
echo "    network: systemctl status skylapse-netwatch"
