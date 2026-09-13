#!/usr/bin/env bash
# Does an in-place update leave the camera able to use sudo?
#
# Written after 0.5.18 bricked a camera in the field. Every unit test passed --
# 668 of them -- because they exercised the *generation* of the sudoers file and
# nothing exercised an actual update on an actual install. The update ran,
# rewrote /etc/sudoers.d/skylapse, and afterwards every privileged operation
# failed with "sudo: a password is required": services could not restart, the
# daemon stayed down, logs could not be read, and the updater could not roll
# itself back. The only way out was reflashing.
#
# The structural fault is worse than the bug: the updater needs sudo, and the
# updater is what would repair sudo. So a single mistake there is unrecoverable
# in the field, and no amount of care in review substitutes for running it.
#
# This installs the way the SD image does -- a system account, the checkout at
# /opt/skylapse owned by it -- then performs the privileged half of a real
# update and asserts sudo still works afterwards.
#
# Destructive. Runs as root, creates a user and writes to /etc. For ephemeral
# CI runners and throwaway containers only; it refuses to run somewhere that
# looks like a real camera.
set -euo pipefail

USER_NAME="${SKYLAPSE_TEST_USER:-skylapse}"
ROOT_DIR="${SKYLAPSE_TEST_ROOT:-/opt/skylapse}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

[ "$(id -u)" -eq 0 ] || fail "must run as root"
[ -f /etc/skylapse/config.yaml ] && fail "this looks like a real camera; refusing"

echo "== installing as the SD image does =="
id -u "$USER_NAME" >/dev/null 2>&1 || \
    adduser --system --group --home /var/lib/skylapse --shell /usr/sbin/nologin "$USER_NAME"

mkdir -p "$ROOT_DIR"
# --delete so a re-run is a clean install, matching a fresh flash.
rsync -a --delete --exclude venv/ --exclude web/node_modules/ \
      --exclude .pytest_cache/ --exclude __pycache__/ "$REPO/" "$ROOT_DIR/"
chown -R "$USER_NAME:$USER_NAME" "$ROOT_DIR"

# The privileged half of install.sh, verbatim. Not a paraphrase: if this
# diverges from the installer the test is worthless, so it is the same two
# commands with the same inputs.
chmod 755 "$ROOT_DIR/scripts/skylapse-admin"
sed -e "s|@SKYLAPSE_ROOT@|$ROOT_DIR|g" -e "s|@SKYLAPSE_USER@|$USER_NAME|g" \
    "$ROOT_DIR/systemd/skylapse.sudoers" > /tmp/skylapse.sudoers
visudo -cqf /tmp/skylapse.sudoers || fail "install.sh generates a malformed sudoers file"
install -m 440 -o root -g root /tmp/skylapse.sudoers /etc/sudoers.d/skylapse
rm -f /tmp/skylapse.sudoers
pass "installed"

echo
echo "== before the update =="
echo "  checkout owner: $(stat -c %U "$ROOT_DIR")"
echo "  sudoers:        $(tail -1 /etc/sudoers.d/skylapse)"

# `sudo -n <helper>` with no arguments prints usage and exits 2. What matters is
# that it is *allowed to run at all* -- a policy failure is exit 1 with "a
# password is required" on stderr, which is what the field failure looked like.
check_sudo() {
    local when="$1" out
    out="$(runuser -u "$USER_NAME" -- sudo -n "$ROOT_DIR/scripts/skylapse-admin" 2>&1 || true)"
    case "$out" in
        *"password is required"*|*"not allowed"*|*"no tty"*)
            echo "  sudo says: $out" >&2
            fail "sudo is refused $when -- this is the 0.5.18 failure" ;;
    esac
    pass "sudo works $when"
}
check_sudo "before the update"

echo
echo "== the privileged half of an in-place update =="
# Exactly what updater._build() runs when systemd/ or scripts/ has changed --
# which 0.5.18 did, and which is therefore the step under suspicion.
if runuser -u "$USER_NAME" -- sudo -n "$ROOT_DIR/scripts/skylapse-admin" reinstall-units; then
    pass "reinstall-units ran"
else
    fail "reinstall-units failed, so an update would leave the units stale"
fi

echo
echo "== after the update =="
echo "  checkout owner: $(stat -c %U "$ROOT_DIR")"
echo "  sudoers:        $(tail -1 /etc/sudoers.d/skylapse)"

# The assertion the field failure would have tripped.
check_sudo "after the update"

# And the specific way it goes wrong: reinstall-units derives the user from the
# checkout's owner while install.sh is told the name. If those ever disagree,
# the file is valid, sudo parses it happily, and it grants the wrong account --
# which reads exactly like no rule at all.
granted="$(awk '/NOPASSWD/ {print $1}' /etc/sudoers.d/skylapse | tail -1)"
[ "$granted" = "$USER_NAME" ] || \
    fail "sudoers now grants '$granted', not '$USER_NAME' -- the update changed who is authorised"
pass "sudoers still grants $USER_NAME"

# The units have to name the same account, or systemd starts the services as
# somebody who cannot read the config.
for unit in skylapse-daemon skylapse-api skylapse-netwatch; do
    [ -f "/etc/systemd/system/$unit.service" ] || fail "$unit.service was not installed"
    got="$(awk -F= '/^User=/ {print $2}' "/etc/systemd/system/$unit.service")"
    [ "$got" = "$USER_NAME" ] || fail "$unit runs as '$got', not '$USER_NAME'"
done
pass "all three units run as $USER_NAME"

echo
echo "PASS: an update leaves sudo, the sudoers entry and the units intact"
