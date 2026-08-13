"""Remote access via the user's own Tailscale account (free tier).

Wraps exactly three CLI interactions so the web UI can drive the whole flow:
status (are we in? what's our HTTPS name?), enable (start auth, return the
login URL for the QR code), and serve (bind the UI to the tailnet HTTPS URL).
Nothing here talks to any service the project operates — goal 0 intact.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading

log = logging.getLogger("skylapse.remote")

_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")
_pending_auth_url: str | None = None
_lock = threading.Lock()


def installed() -> bool:
    return shutil.which("tailscale") is not None


def status() -> dict:
    """Normalized status for the UI card."""
    if not installed():
        return {"installed": False, "state": "not_installed"}
    try:
        raw = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=10).stdout
        data = json.loads(raw or "{}")
    except Exception:
        return {"installed": True, "state": "error"}
    state = data.get("BackendState", "unknown")           # NeedsLogin | Running | Stopped
    dns = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
    with _lock:
        pending = _pending_auth_url
    return {
        "installed": True,
        "state": state.lower(),
        "connected": state == "Running",
        "url": f"https://{dns}" if dns and state == "Running" else None,
        "auth_url": pending if state == "NeedsLogin" else None,
    }


def enable() -> dict:
    """Kick off `tailscale up` and capture the interactive auth URL it prints.
    Runs in a background thread because `up` blocks until auth completes;
    the UI polls status() and shows the QR as soon as auth_url appears.
    """
    if not installed():
        return {"ok": False, "error": "tailscale not installed"}

    def _runner() -> None:
        global _pending_auth_url
        try:
            proc = subprocess.Popen(["tailscale", "up"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
            for line in proc.stdout:                       # auth URL arrives early
                m = _AUTH_URL_RE.search(line)
                if m:
                    with _lock:
                        _pending_auth_url = m.group(0)
            proc.wait(timeout=600)                         # blocks until user approves
        except Exception as exc:
            log.warning("tailscale up: %s", exc)
        finally:
            with _lock:
                _pending_auth_url = None

    threading.Thread(target=_runner, daemon=True).start()
    return {"ok": True}


def qr_svg(url: str) -> str:
    """QR code as an SVG string for inline rendering in the settings card."""
    import segno
    import io
    buf = io.BytesIO()
    segno.make(url).save(buf, kind="svg", scale=6, dark="#e4e4e7", light=None)
    return buf.getvalue().decode()


def enable_https_serve() -> bool:
    """Bind the web UI onto the tailnet HTTPS URL (auto-certs). Idempotent."""
    try:
        subprocess.run(["tailscale", "serve", "--bg", "80"],
                       capture_output=True, timeout=15)
        return True
    except Exception:
        return False
