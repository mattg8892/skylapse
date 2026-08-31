"""Telling systemd the capture loop is still turning.

Not to be confused with watchdog.py next door, which is the *stall* watch: that
one runs inside the loop and notices when frames stop arriving, so it can send
an alert. This one is the opposite direction -- it reports outwards, so that
something above the daemon can act when the loop itself stops.

Between them they cover different failures, and the distinction is the whole
point. There is already a stall check inside the loop, and `Restart=always`
brings the daemon back if the process dies. Neither covers what actually
happened: a process still alive, still holding its file handles and its GPIO,
and not going round any more. systemd saw a healthy service. The stall check
never ran, because running was the thing that stopped.

So the loop reports in, and systemd restarts it if the reports stop. That is
the only supervisor left when the thing being supervised is the thing that
failed -- and on a restart the dew heater's pin is driven low again, so a
heater cannot outlive the loop that was meant to be controlling it.

Deliberately dependency-free: this writes the notification datagram itself
rather than pulling in python-systemd, which is a compiled package and one more
thing to install on a camera in a field. Outside systemd NOTIFY_SOCKET is
unset and every call here is a no-op, so tests and dev runs are unaffected.
"""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("skylapse.watchdog")

_warned = False


def _notify(message: str) -> bool:
    """Send one datagram to systemd's notification socket. True if it went."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False                      # not running under systemd
    if not hasattr(socket, "AF_UNIX"):
        return False                      # not a platform with unix sockets
    # A leading '@' means the abstract namespace, which is a NUL byte on the
    # wire. Getting this wrong fails silently and the watchdog never fires.
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        global _warned
        if not _warned:                   # once, not every frame
            log.warning("Could not notify systemd: %s", exc)
            _warned = True
        return False


def ping() -> bool:
    """One 'still going round' from the capture loop."""
    return _notify("WATCHDOG=1")


def enabled() -> bool:
    """Whether systemd is actually watching.

    WATCHDOG_USEC is set only when the unit has WatchdogSec. Worth being able
    to check, because a watchdog everyone believes in and nothing is running is
    worse than none at all.
    """
    return bool(os.environ.get("NOTIFY_SOCKET")
                and os.environ.get("WATCHDOG_USEC"))


def interval_s() -> float | None:
    """How often systemd wants to hear from us -- half the configured timeout,
    which is the convention, so one missed ping is not fatal."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return int(usec) / 2_000_000
    except ValueError:
        return None
