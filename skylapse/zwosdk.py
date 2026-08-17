"""Installing ZWO's camera SDK from the settings screen, instead of over SSH.

ZWO's licence does not allow their SDK to be redistributed and their download
portal is browser-only, so the shared library the ZWO driver needs cannot ship
inside the SD image. That left a ZWO rig as the one Skylapse setup that still
required a terminal — the single thing this appliance exists to avoid.

Fetching it on demand does not redistribute anything: the user asks for it,
accepts ZWO's terms, and the file comes from the INDI project's mirror of the
same vendor binaries. All the privileged work happens in scripts/skylapse-admin
behind one sudoers entry; this module only decides whether to offer the button,
runs the helper, and restarts the daemon so it re-probes for the camera.

None of which makes ZWO a first-class target. Skylapse is built and tested
against Raspberry Pi camera modules; the ZWO driver is best-effort, verified on
one model, and may not work with yours at all. The UI says so before the button.
"""
from __future__ import annotations

import importlib.util
import logging
import platform
import subprocess
from pathlib import Path

log = logging.getLogger("skylapse.zwosdk")

# Kept in step with ZWO_SDK_VERSION in scripts/skylapse-admin, which is the one
# that actually installs it. Shown in the UI so a rig can be told apart from a
# newer one at a glance.
SDK_VERSION = "1.41"

# The armv8 build is the only one the helper installs, so it is the only one to
# offer. A 32-bit OS gets told plainly rather than handed a button that fails.
SUPPORTED_MACHINES = {"aarch64", "arm64"}

LICENSE_URL = ("https://github.com/indilib/indi-3rdparty/blob/master/"
               "libasi/license.txt")

# 4 MB over whatever Wi-Fi the camera is on, plus a udev reload. Long enough for
# a slow link, short enough that a hung download does not hold the request open
# until the browser gives up on it with nothing to show.
_INSTALL_TIMEOUT_S = 420
_RESTART_TIMEOUT_S = 90


def helper() -> str:
    return str(Path(__file__).resolve().parent.parent / "scripts" / "skylapse-admin")


def supported() -> bool:
    return platform.machine() in SUPPORTED_MACHINES


def bindings_present() -> bool:
    """The Python bindings, which ship in the image via the `zwo` extra.

    Separate from the library because they fail separately: a venv rebuilt
    without the extra leaves the SDK installed and the driver still unable to
    import, and "install the SDK" would be the wrong advice for that.
    """
    return importlib.util.find_spec("zwoasi") is not None


def status() -> dict:
    """What the settings and wizard cards render from."""
    from .daemon.drivers.zwo import sdk_path

    path = sdk_path()
    return {
        "supported": supported(),
        "machine": platform.machine(),
        "installed": bool(path),
        "path": path,
        "bindings": bindings_present(),
        "version": SDK_VERSION,
        "license_url": LICENSE_URL,
    }


def install() -> dict:
    """Run the privileged helper, then restart the daemon so it re-probes.

    A restart rather than a reboot: nothing here touches the boot config, the
    library is picked up at dlopen and the udev rules at the next hotplug. The
    daemon only probes for cameras when it opens one, though, so without the
    restart a freshly installed SDK does nothing until the next night.
    """
    if not supported():
        return {"ok": False,
                "error": f"ZWO support needs 64-bit Raspberry Pi OS "
                         f"(this system is {platform.machine()})"}

    result = subprocess.run(["sudo", "-n", helper(), "zwo-sdk"],
                            capture_output=True, text=True,
                            timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        log.warning("ZWO SDK install failed (%s): %s", result.returncode, detail)
        return {"ok": False, "error": detail[:300] or "the installer failed"}

    # Best effort by design: the SDK is installed either way, and reporting the
    # install as failed because a restart did not go through would send someone
    # to fix the wrong thing. `restarted` is surfaced so the UI can say which
    # happened.
    restarted = _restart_daemon()
    return {"ok": True, "restarted": restarted,
            "output": (result.stdout or "").strip()[:300]}


def _restart_daemon() -> bool:
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "skylapse-daemon"],
            capture_output=True, text=True, timeout=_RESTART_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not restart the daemon after the SDK install: %s", exc)
        return False
    if result.returncode != 0:
        log.warning("Could not restart the daemon after the SDK install: %s",
                    (result.stderr or "").strip())
    return result.returncode == 0
