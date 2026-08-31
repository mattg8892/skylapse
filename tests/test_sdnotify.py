"""Nothing may outlive the loop that is supposed to be controlling it.

The failure this exists for: the camera left the network with the dew heater's
LED still lit, and stayed that way until the power was pulled by hand. The SD
card had died. Every safeguard built into the heater -- the capped test pulse,
the finally block, off() on daemon exit -- assumes the daemon is alive to
enforce it, and none of them ran.

Three layers, each covering what the one before cannot:

  1. The daemon drives the pin low on startup, so a restart clears a latched
     heater whatever left it that way.
  2. systemd restarts the daemon if the loop stops reporting in, so a process
     that is alive but no longer going round gets restarted at all. Restart=
     always does not cover this: systemd sees a healthy service.
  3. The firmware drives the pin low at boot, because between power-on and
     Linux configuring the pin it is an input, the gate floats, and no software
     of any kind is running to prevent it.
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

from skylapse.daemon import sdnotify as watchdog

REPO = Path(__file__).resolve().parents[1]


# -- layer 1: a restart clears a latched heater ------------------------------

def test_the_heater_pin_is_driven_low_before_anything_else(monkeypatch):
    """Construction, not the first tick. The gap between a restart and the
    first sensor reading is exactly where a latched pin would keep heating."""
    from skylapse.daemon import dewheater

    calls = []

    class FakePin:
        def __init__(self, *a, **kw): pass
        @property
        def value(self): return False
        @value.setter
        def value(self, v): calls.append(v)

    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice": FakePin}))
    monkeypatch.setattr(dewheater, "find_sensor", lambda: None)

    dewheater.DewHeater(18, 5.0, 8.0)
    assert calls and calls[0] is False, \
        "the pin must be driven low on construction, before any reading"


def test_the_pin_goes_low_even_with_no_sensor(monkeypatch):
    """A missing sensor disables the feature. It must not also skip the one
    action that makes a latched heater safe -- that is the case where nobody is
    watching at all."""
    from skylapse.daemon import dewheater

    calls = []

    class FakePin:
        def __init__(self, *a, **kw): pass
        @property
        def value(self): return False
        @value.setter
        def value(self, v): calls.append(v)

    monkeypatch.setitem(__import__("sys").modules, "gpiozero",
                        type("M", (), {"OutputDevice": FakePin}))
    monkeypatch.setattr(dewheater, "find_sensor", lambda: None)

    heater = dewheater.DewHeater(18, 5.0, 8.0)
    assert heater.available is False
    assert False in calls


# -- layer 2: systemd restarts a loop that stopped turning -------------------

def test_a_platform_without_unix_sockets_is_also_a_no_op(monkeypatch):
    """The dev machine is Windows, which has no AF_UNIX. Production is Linux,
    but a module that throws AttributeError on a developer's machine is a trap
    for whoever runs the tests next.

    Removing the attribute is the honest way to test this -- setting it to None
    makes hasattr() true and the guard passes straight through it.
    """
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/whatever.sock")
    monkeypatch.delattr(watchdog.socket, "AF_UNIX", raising=False)
    assert watchdog.ping() is False


def test_no_notify_socket_is_a_silent_no_op(monkeypatch):
    """Dev runs and tests are not under systemd. This must not raise or log
    per frame."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert watchdog.ping() is False
    assert watchdog.enabled() is False


HAS_UNIX_SOCKETS = hasattr(socket, "AF_UNIX")
needs_unix = pytest.mark.skipif(not HAS_UNIX_SOCKETS,
                                reason="no AF_UNIX on this platform")


@needs_unix
def test_a_ping_reaches_the_socket(tmp_path, monkeypatch):
    """The datagram is written by hand rather than via python-systemd, which is
    a compiled dependency on a camera in a field. So it is worth proving the
    bytes actually arrive."""
    path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert watchdog.ping() is True
        assert server.recv(64) == b"WATCHDOG=1"
    finally:
        server.close()


@needs_unix
def test_an_abstract_socket_address_is_translated(monkeypatch):
    """A leading '@' is the abstract namespace and must go on the wire as NUL.
    Getting this wrong fails silently, and a watchdog that never pings is worse
    than no watchdog: it restarts a healthy camera on a timer."""
    sent = {}

    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect(self, addr): sent["addr"] = addr
        def sendall(self, data): sent["data"] = data

    monkeypatch.setenv("NOTIFY_SOCKET", "@systemd/notify")
    monkeypatch.setattr(watchdog.socket, "socket", lambda *a, **kw: FakeSock())
    assert watchdog.ping() is True
    assert sent["addr"].startswith("\0"), "abstract address not translated"
    assert "@" not in sent["addr"]


@needs_unix
def test_a_broken_socket_does_not_take_the_daemon_down(monkeypatch):
    """Failing to tell systemd we are alive is not a reason to stop being
    alive."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/skylapse-test.sock")
    monkeypatch.setattr(watchdog, "_warned", False)
    assert watchdog.ping() is False          # no exception


def test_the_ping_interval_is_half_the_timeout(monkeypatch):
    """The convention, so one missed ping is not fatal."""
    monkeypatch.setenv("WATCHDOG_USEC", "600000000")
    assert watchdog.interval_s() == 300.0


def test_the_loop_reports_in_every_iteration():
    """Placed before the work, so the ping means "still going round" rather
    than "finished a frame" — a camera failing to expose is still a daemon
    worth leaving alone."""
    src = (REPO / "skylapse" / "daemon" / "main.py").read_text(encoding="utf-8")
    body = src.split("def _loop", 1)[1]
    head = body.split("\n")[:8]
    assert any("sdnotify.ping()" in line for line in head), \
        "the ping must be at the top of the loop, not buried after the work"


def test_the_unit_configures_the_watchdog():
    unit = (REPO / "systemd" / "skylapse-daemon.service").read_text(encoding="utf-8")
    assert "WatchdogSec=" in unit
    assert "NotifyAccess=main" in unit, \
        "without NotifyAccess systemd discards the pings and kills a healthy daemon"
    assert "Restart=always" in unit, "the watchdog is pointless without a restart"


def test_the_watchdog_timeout_clears_a_legitimate_night_frame():
    """A 40s exposure on top of a control settle allowed 90s, and the loop pings
    once per iteration rather than mid-frame. A timeout that fires on a slow
    frame would restart the camera all night."""
    from skylapse.daemon.drivers.picam import SETTLE_MAX_S
    unit = (REPO / "systemd" / "skylapse-daemon.service").read_text(encoding="utf-8")
    seconds = int(next(line for line in unit.splitlines()
                       if line.startswith("WatchdogSec=")).split("=")[1])
    worst_frame = SETTLE_MAX_S + 40 + 30       # settle + exposure + write/analyse
    assert seconds > worst_frame * 2, \
        f"WatchdogSec={seconds} leaves too little margin over a {worst_frame}s frame"


# -- layer 3: the firmware holds the pin before Linux exists -----------------

def test_enabling_i2c_also_makes_the_heater_pin_safe_at_boot():
    """The window no software can cover: power-on to Linux configuring the pin.
    The gate floats, and a camera with a dead SD card sat with the heater lit
    and nothing running to have lit it."""
    admin = (REPO / "scripts" / "skylapse-admin").read_text(encoding="utf-8")
    block = admin.split("i2c-enable)", 1)[1].split(";;", 1)[0]
    assert "op,dl" in block, \
        "the boot config must drive the heater pin low from the firmware"


BASH = __import__("shutil").which("bash")


@pytest.mark.skipif(not BASH, reason="needs bash")
def test_the_boot_config_edit_is_idempotent():
    """Run twice, one line. An append that repeats grows config.txt every time
    someone touches the dew heater settings."""
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.txt"
        cfg.write_text("[all]\ndtoverlay=vc4-kms-v3d\n")
        env = {"PATH": "/usr/bin:/bin", "SKYLAPSE_CONFIG_TXT": str(cfg)}
        for _ in range(2):
            subprocess.run([BASH, str(REPO / "scripts" / "skylapse-admin"), "i2c-enable"],
                           capture_output=True, text=True, env=env)
        text = cfg.read_text()
        assert text.count("gpio=18=op,dl") == 1, text
        assert text.count("dtparam=i2c_arm=on") == 1, text
