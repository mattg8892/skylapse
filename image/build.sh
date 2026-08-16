#!/usr/bin/env bash
# Build the flashable Skylapse SD image.
#
# Takes the official Raspberry Pi OS Lite (64-bit) image and installs Skylapse
# into it, rather than building a root filesystem from scratch with pi-gen.
# That choice buys three things that would otherwise have to be reimplemented
# and then maintained:
#
#   * Raspberry Pi Imager's customisation — hostname, SSH keys, Wi-Fi, locale.
#     Imager writes those to the boot partition and Pi OS's firstboot service
#     consumes them, so any image derived from Pi OS honours them for free.
#   * Filesystem auto-expand on first boot, which is Pi OS's init_resize.
#   * Kernel, firmware and camera stack updates, by rebuilding on a newer base.
#
# Runs on an arm64 Linux host so the chroot is native — no qemu, no binfmt.
# GitHub's free arm64 runners for public repos are exactly that, which keeps
# the project's no-cost-to-anyone constraint intact.
#
# Usage:  sudo image/build.sh [output.img]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO_ROOT/skylapse.img}"
WORK="${WORK:-$(mktemp -d)}"

# The image is installed into rather than built, so it needs headroom for the
# venv, node_modules and the built frontend. Measured on a real install: about
# 1.4 GB, so 3 GB of slack is comfortable without bloating the download —
# empty space compresses to almost nothing in the .xz.
GROW_MB="${GROW_MB:-3072}"

SKYLAPSE_USER="skylapse"
INSTALL_DIR="/opt/skylapse"

BASE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"

log() { echo "==> $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
[ "$(uname -m)" = "aarch64" ] || {
    echo "This must run on arm64 so the chroot is native (uname -m says $(uname -m))."
    echo "On GitHub Actions use runs-on: ubuntu-24.04-arm."
    exit 1
}

for tool in xz losetup parted resize2fs curl rsync; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 1; }
done

cleanup() {
    set +e
    if [ -n "${MNT:-}" ] && mountpoint -q "$MNT" 2>/dev/null; then
        for d in dev/pts dev proc sys boot/firmware; do
            umount -l "$MNT/$d" 2>/dev/null
        done
        umount -l "$MNT" 2>/dev/null
    fi
    [ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
    return 0
}
trap cleanup EXIT

log "Fetching Raspberry Pi OS Lite (64-bit)"
curl -fL --retry 3 -o "$WORK/base.img.xz" "$BASE_URL"
xz -dT0 "$WORK/base.img.xz"
mv "$WORK/base.img" "$WORK/skylapse.img"

log "Growing the image by ${GROW_MB}MB for the install"
truncate -s "+${GROW_MB}M" "$WORK/skylapse.img"
LOOP="$(losetup -f --show -P "$WORK/skylapse.img")"
# Partition 2 is the root filesystem on every Pi OS image.
parted -s "$LOOP" resizepart 2 100%
partprobe "$LOOP" || true
sleep 2
e2fsck -fy "${LOOP}p2" || true
resize2fs "${LOOP}p2"

MNT="$WORK/mnt"
mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"
mkdir -p "$MNT/boot/firmware"
mount "${LOOP}p1" "$MNT/boot/firmware"

log "Preparing the chroot"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"

# apt starts daemons as it installs them. In a chroot that is at best noise and
# at worst a hang, so refuse every start for the duration of the build.
cat > "$MNT/usr/sbin/policy-rc.d" <<'POLICY'
#!/bin/sh
exit 101
POLICY
chmod +x "$MNT/usr/sbin/policy-rc.d"

log "Copying the checkout into $INSTALL_DIR"
mkdir -p "$MNT$INSTALL_DIR"
# .git goes too: the in-app updater is `git fetch` + `git checkout`, so an
# image without it could never update itself, which is most of the point of
# shipping one. venv and node_modules are rebuilt inside the image.
rsync -a --delete \
    --exclude venv/ --exclude web/node_modules/ --exclude web/dist/ \
    --exclude .pytest_cache/ --exclude __pycache__/ \
    "$REPO_ROOT/" "$MNT$INSTALL_DIR/"

log "Installing"
chroot "$MNT" /bin/bash -eux <<CHROOT
export DEBIAN_FRONTEND=noninteractive

# A dedicated service account rather than the login user. Pi OS images have had
# no default user since Bookworm — the login account is created on first boot
# from Imager's settings, and may be named anything or not exist at all. The
# services cannot depend on it.
if ! id -u $SKYLAPSE_USER >/dev/null 2>&1; then
    adduser --system --group --home /var/lib/skylapse --shell /usr/sbin/nologin $SKYLAPSE_USER
fi
# video/render for the camera stack; the rest are best-effort and only exist on
# some images, so a missing group must not fail the build.
for grp in video render i2c spi gpio plugdev netdev; do
    getent group \$grp >/dev/null && adduser $SKYLAPSE_USER \$grp || true
done

chown -R $SKYLAPSE_USER:$SKYLAPSE_USER $INSTALL_DIR

SKYLAPSE_IMAGE_BUILD=1 SKYLAPSE_USER=$SKYLAPSE_USER $INSTALL_DIR/install.sh

# After install.sh, because Pi OS Lite ships no git and install.sh is what
# apt-installs it. git refuses to operate on a repo owned by another user, and
# the in-app updater is git fetch + git checkout, so without this the image
# could never update itself.
git config --system --add safe.directory $INSTALL_DIR

# The promised address is skylapse.local. Imager can override the hostname, but
# somebody who flashes the image and clicks straight past customisation should
# still land where the documentation says they will.
echo skylapse > /etc/hostname
sed -i "s/^127.0.1.1.*/127.0.1.1\tskylapse/" /etc/hosts

apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache /home/*/.cache
CHROOT

log "Cleaning up"
rm -f "$MNT/usr/sbin/policy-rc.d" "$MNT/etc/resolv.conf"
touch "$MNT/etc/resolv.conf"
# Zeroing the free space costs a few minutes and takes gigabytes off the .xz,
# because unwritten blocks compress to nothing while stale ones do not.
dd if=/dev/zero of="$MNT/ZEROFILL" bs=4M status=none || true
rm -f "$MNT/ZEROFILL"
sync

cleanup
trap - EXIT

mv "$WORK/skylapse.img" "$OUT"
log "Built $OUT ($(du -h "$OUT" | cut -f1))"
