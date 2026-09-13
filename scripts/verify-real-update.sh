#!/usr/bin/env bash
# Run a real in-place update between two releases and see what survives.
#
# The narrower check next door (verify-update-path.sh) exercises the privileged
# step in isolation and passes -- which is useful, because it eliminates the
# obvious suspect, and useless for finding what actually happened to 0.5.18.
#
# A real update does more than that step: it fetches, checks out, reinstalls
# Python dependencies, rebuilds the frontend, reinstalls the units, and
# restarts the services. On the camera that broke, the frontend afterwards
# served zero bytes and every sudo call was refused -- so something in that
# sequence, not the sudoers write on its own, is the thing to find.
#
# This drives the updater's own apply() rather than a re-implementation of it.
# A test that reimplements the code under test cannot find a bug in that code.
#
# Destructive. Ephemeral CI runners and throwaway VMs only.
set -euo pipefail

FROM_REF="${1:?usage: verify-real-update.sh <from-ref> <to-ref>}"
TO_REF="${2:?usage: verify-real-update.sh <from-ref> <to-ref>}"
USER_NAME="${SKYLAPSE_TEST_USER:-skylapse}"
ROOT_DIR="${SKYLAPSE_TEST_ROOT:-/opt/skylapse}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }
note() { echo "   $*"; }

[ "$(id -u)" -eq 0 ] || fail "must run as root"
[ -f /etc/skylapse/config.yaml ] && fail "this looks like a real camera; refusing"

echo "== installing $FROM_REF the way the SD image does =="
id -u "$USER_NAME" >/dev/null 2>&1 || \
    adduser --system --group --home /var/lib/skylapse --shell /usr/sbin/nologin "$USER_NAME"

rm -rf "$ROOT_DIR"
# A full clone with tags: the updater is git fetch + git checkout, and a
# shallow one would put the install in a state where it could never update --
# which is itself a thing worth not shipping.
git clone --quiet "$REPO" "$ROOT_DIR"
git -C "$ROOT_DIR" fetch --tags --quiet origin || true
git -C "$ROOT_DIR" checkout --quiet --force "$FROM_REF"
git config --system --add safe.directory "$ROOT_DIR"

install -d -o "$USER_NAME" -g "$USER_NAME" /etc/skylapse /var/lib/skylapse \
        /var/lib/skylapse/images
chown -R "$USER_NAME:$USER_NAME" "$ROOT_DIR"

# The privileged half of install.sh, verbatim.
chmod 755 "$ROOT_DIR/scripts/skylapse-admin"
sed -e "s|@SKYLAPSE_ROOT@|$ROOT_DIR|g" -e "s|@SKYLAPSE_USER@|$USER_NAME|g" \
    "$ROOT_DIR/systemd/skylapse.sudoers" > /tmp/skylapse.sudoers
visudo -cqf /tmp/skylapse.sudoers || fail "install.sh generates a malformed sudoers file"
install -m 440 -o root -g root /tmp/skylapse.sudoers /etc/sudoers.d/skylapse
rm -f /tmp/skylapse.sudoers
for unit in skylapse-daemon skylapse-api skylapse-netwatch; do
    sed -e "s|@SKYLAPSE_ROOT@|$ROOT_DIR|g" -e "s|@SKYLAPSE_USER@|$USER_NAME|g" \
        "$ROOT_DIR/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload

# The venv and the frontend, as install.sh builds them. The update rebuilds
# both, so they have to exist first or the rebuild is not a rebuild.
runuser -u "$USER_NAME" -- python3 -m venv --system-site-packages "$ROOT_DIR/venv"
runuser -u "$USER_NAME" -- "$ROOT_DIR/venv/bin/pip" install --quiet --upgrade pip
runuser -u "$USER_NAME" -- "$ROOT_DIR/venv/bin/pip" install --quiet -e "$ROOT_DIR"
runuser -u "$USER_NAME" -- npm --prefix "$ROOT_DIR/web" install --silent
runuser -u "$USER_NAME" -- npm --prefix "$ROOT_DIR/web" run build --silent
pass "installed $FROM_REF"

before_bundle="$(ls "$ROOT_DIR/web/dist/assets/"*.js 2>/dev/null | head -1)"
note "frontend bundle: $(basename "${before_bundle:-none}")"
note "sudoers:         $(tail -1 /etc/sudoers.d/skylapse)"

check_sudo() {
    local when="$1" out
    out="$(runuser -u "$USER_NAME" -- sudo -n "$ROOT_DIR/scripts/skylapse-admin" 2>&1 || true)"
    case "$out" in
        *"password is required"*|*"not allowed"*|*"no tty"*)
            echo "   sudo says: $out" >&2
            fail "sudo is refused $when" ;;
    esac
    pass "sudo works $when"
}
check_sudo "on a fresh install"

echo
echo "== the real update, $FROM_REF -> $TO_REF =="
# apply() runs in the detached worker on a camera. Here it runs inline, as the
# service user, which is the same privilege and the same code.
set +e
runuser -u "$USER_NAME" -- env SKYLAPSE_ROOT="$ROOT_DIR" \
    "$ROOT_DIR/venv/bin/python" -c "
import logging, sys
logging.basicConfig(level=logging.INFO, format='   updater: %(message)s')
sys.path.insert(0, '$ROOT_DIR')
from skylapse import updater
print('   ->', updater.apply('$TO_REF', apply_now=True))
"
update_rc=$?
set -e
note "updater exited $update_rc"

echo
echo "== what survived =="
note "version:  $(git -C "$ROOT_DIR" describe --tags --always 2>/dev/null)"
note "owner:    $(stat -c %U "$ROOT_DIR")"
note "sudoers:  $(tail -1 /etc/sudoers.d/skylapse 2>/dev/null || echo 'MISSING')"

after_bundle="$(ls "$ROOT_DIR/web/dist/assets/"*.js 2>/dev/null | head -1)"
if [ -z "$after_bundle" ]; then
    fail "no frontend bundle after the update -- the web interface would serve nothing,
which is exactly what the camera did: index.html present, zero bytes of app"
fi
note "frontend: $(basename "$after_bundle") ($(stat -c %s "$after_bundle") bytes)"
[ "$(stat -c %s "$after_bundle")" -gt 10000 ] || fail "frontend bundle is suspiciously small"
pass "the frontend was rebuilt"

check_sudo "after a real update"

granted="$(awk '/NOPASSWD/ {print $1}' /etc/sudoers.d/skylapse | tail -1)"
[ "$granted" = "$USER_NAME" ] || \
    fail "sudoers now grants '$granted', not '$USER_NAME'"
pass "sudoers still grants $USER_NAME"

echo
echo "PASS: $FROM_REF -> $TO_REF leaves a working camera"
