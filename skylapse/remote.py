"""Remote access via the user's own Tailscale account (free tier).

Wraps the Tailscale CLI so the web UI can drive the whole flow: install it if it
is not there, start the login and hand back the URL as a QR code, report status,
put the UI on the tailnet's HTTPS name, and turn it all off again. Nothing here
talks to any service the project operates — goal 0 intact.

Everything privileged goes through scripts/skylapse-admin, the one sudoers
entry, because the API runs as an unprivileged service and every interesting
Tailscale operation needs root.

What was wrong before, all of it found on the rig at once:

1. **Nothing installed Tailscale.** Not `install.sh`, not the SD image. So the
   card's "Tailscale isn't installed on this device" was true on every flashed
   card, and it was also the entire content of the card — a statement of fact
   with no way to act on it, in a product whose whole claim is that you never
   need a terminal.
2. **`tailscale up` needs root**, and the API is not root. Even on a machine
   where someone had installed Tailscale by hand, the button could only fail.
3. **`enable_https_serve()` was never called by anything.** A camera that got
   through the login would show an `https://…ts.net` link pointing at a host
   serving nothing on 443.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("skylapse.remote")

_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")
_pending_auth_url: str | None = None
_last_error: str = ""
_lock = threading.Lock()

# `which` follows PATH, and a systemd service's PATH is not a login shell's:
# /usr/sbin and /usr/local/bin are routinely missing from it. Tailscale's Debian
# package puts the CLI in /usr/bin, but a hand-installed one lands in
# /usr/local/bin, and reporting a present binary as absent is exactly the bug
# this module is being rewritten for.
_CLI_PATHS = ("/usr/bin/tailscale", "/usr/sbin/tailscale",
              "/usr/local/bin/tailscale")

# apt fetching and unpacking a Go binary over whatever link the camera is on.
_INSTALL_TIMEOUT_S = 900
_CLI_TIMEOUT_S = 20
# How long a login may stay open before we stop holding the process. Long
# enough to walk inside, install the phone app and sign in.
_LOGIN_TIMEOUT_S = 900


def helper() -> str:
    return str(Path(__file__).resolve().parent.parent / "scripts" / "skylapse-admin")


def cli_path() -> str:
    """Where the tailscale binary is, or "" if it genuinely is not installed."""
    found = shutil.which("tailscale")
    if found:
        return found
    for path in _CLI_PATHS:
        if Path(path).exists():
            return path
    return ""


def installed() -> bool:
    return bool(cli_path())


def can_install() -> bool:
    """Whether offering the install button would be honest.

    apt and the privileged helper are both required; a source checkout on a
    developer's laptop has neither, and a button that cannot work should not be
    drawn at all.
    """
    return bool(shutil.which("apt-get")) and Path(helper()).exists()


def _admin(*args: str, timeout: int = _CLI_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", helper(), *args],
                          capture_output=True, text=True, timeout=timeout)


def _status_json() -> tuple[dict, str]:
    """`tailscale status --json`, unprivileged first.

    Reading status usually works for any local user, so it is tried directly:
    it is polled every few seconds while a login is pending, and a sudo call per
    poll would be a lot of noise in the journal for a read. The helper is the
    fallback for the builds where the socket is root-only.
    """
    cli = cli_path()
    if cli:
        try:
            result = subprocess.run([cli, "status", "--json"], capture_output=True,
                                    text=True, timeout=_CLI_TIMEOUT_S)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout), ""
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    try:
        result = _admin("tailscale-status")
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, str(exc)
    if result.returncode != 0:
        return {}, (result.stderr or result.stdout or "").strip()[:200]
    try:
        return json.loads(result.stdout or "{}"), ""
    except json.JSONDecodeError:
        return {}, "could not read Tailscale's status"


def status() -> dict:
    """Normalized status for the UI card.

    Every branch carries enough for the card to offer an action. "Not installed"
    is a state with a button, not a dead end.
    """
    if not installed():
        return {"installed": False, "can_install": can_install(),
                "state": "not_installed", "error": _read_error()}

    data, error = _status_json()
    if error and not data:
        return {"installed": True, "can_install": False, "state": "error",
                "error": error or _read_error()}

    state = data.get("BackendState", "unknown")        # NeedsLogin|Running|Stopped
    dns = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
    with _lock:
        pending = _pending_auth_url
    return {
        "installed": True,
        "can_install": False,
        "state": state.lower(),
        "connected": state == "Running",
        "url": f"https://{dns}" if dns and state == "Running" else None,
        # Tailscale prints the login URL once, on `up`. It is also in the status
        # payload as AuthURL on some versions, so prefer whichever we have.
        "auth_url": pending or (data.get("AuthURL") or None
                                if state == "NeedsLogin" else None),
        "error": _read_error(),
    }


def _read_error() -> str:
    with _lock:
        return _last_error


def _set_error(message: str) -> None:
    global _last_error
    with _lock:
        _last_error = message
    if message:
        log.warning("remote access: %s", message)


def install() -> dict:
    """Add Tailscale's signed apt repository and install the package.

    Long: this is an apt install over the camera's own connection. The endpoint
    holds the request for it rather than backgrounding it, because the only
    useful thing the UI can do meanwhile is wait, and a background job would
    need a second status channel to say the same thing.
    """
    if installed():
        return {"ok": True, "note": "already installed"}
    if not can_install():
        return {"ok": False, "error": "this system has no apt to install it with"}
    _set_error("")
    try:
        result = _admin("tailscale-install", timeout=_INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _set_error("The install timed out. Check the camera's internet connection.")
        return {"ok": False, "error": _read_error()}
    except (OSError, subprocess.SubprocessError) as exc:
        _set_error(f"Could not start the installer: {exc}")
        return {"ok": False, "error": _read_error()}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        _set_error(detail or "The Tailscale install failed.")
        return {"ok": False, "error": _read_error()}
    return {"ok": True, "installed": installed()}


def enable() -> dict:
    """Kick off `tailscale up` and capture the interactive auth URL it prints.

    Runs in a background thread because `up` blocks until auth completes; the UI
    polls status() and shows the QR as soon as auth_url appears. Once the login
    lands, the UI is published on the tailnet's HTTPS name — without that the
    address the card then shows would resolve to nothing.
    """
    if not installed():
        return {"ok": False, "error": "tailscale is not installed"}
    _set_error("")

    def _runner() -> None:
        global _pending_auth_url
        proc = None
        try:
            proc = subprocess.Popen(["sudo", "-n", helper(), "tailscale-up"],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:                    # auth URL arrives early
                match = _AUTH_URL_RE.search(line)
                if match:
                    with _lock:
                        _pending_auth_url = match.group(0)
            code = proc.wait(timeout=_LOGIN_TIMEOUT_S)
            if code != 0:
                _set_error("Tailscale could not start. It may need to be "
                           "reinstalled, or the camera may have no internet.")
            else:
                serve()
        except subprocess.TimeoutExpired:
            # Nobody finished the login. Leaving `up` running would hold a root
            # process and a stale QR code for a URL that has since expired.
            _set_error("The login wasn't completed in time. Start it again.")
        except (OSError, subprocess.SubprocessError) as exc:
            _set_error(f"Could not start Tailscale: {exc}")
        finally:
            if proc and proc.poll() is None:
                proc.kill()
            with _lock:
                _pending_auth_url = None

    threading.Thread(target=_runner, daemon=True).start()
    return {"ok": True}


def disable() -> dict:
    """Leave the tailnet. The login is remembered, so re-enabling is one tap."""
    if not installed():
        return {"ok": False, "error": "tailscale is not installed"}
    try:
        result = _admin("tailscale-down", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False,
                "error": (result.stderr or result.stdout or "").strip()[:200]}
    return {"ok": True}


def serve() -> bool:
    """Bind the web UI onto the tailnet HTTPS URL (auto-certs). Idempotent.

    Called after a successful login rather than left for someone to discover:
    this used to exist and be called by nothing at all, so a camera that got
    through the login advertised an HTTPS address that served nothing.
    """
    try:
        result = _admin("tailscale-serve", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tailscale serve: %s", exc)
        return False
    if result.returncode != 0:
        log.warning("tailscale serve: %s", (result.stderr or "").strip())
    return result.returncode == 0


def qr_svg(url: str) -> str:
    """QR code as an SVG string for inline rendering in the settings card."""
    import io

    import segno
    buf = io.BytesIO()
    segno.make(url).save(buf, kind="svg", scale=6, dark="#e4e4e7", light=None)
    return buf.getvalue().decode()
