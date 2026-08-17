"""Remote access: installing Tailscale, signing in, and never dead-ending.

Three bugs found together on the rig, all of which this covers:

1. Nothing installed Tailscale — not install.sh, not the SD image — so the card
   said "not installed" on every flashed camera and offered nothing to do about
   it. On a product that promises no terminal, a factual dead end is a bug.
2. `tailscale up` needs root and the API is not root, so even a hand-installed
   Tailscale could only fail.
3. `enable_https_serve()` was called by nothing, so a camera that got through
   the login advertised an HTTPS address serving nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skylapse import config, remote
from skylapse.api import main as api

ADMIN = Path(__file__).resolve().parents[1] / "scripts" / "skylapse-admin"
BASH = shutil.which("bash")


@pytest.fixture(autouse=True)
def clean_module_state():
    """The auth URL and last error are module-level, as the UI polls for them."""
    remote._pending_auth_url = None
    remote._last_error = ""
    yield
    remote._pending_auth_url = None
    remote._last_error = ""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


# -- detection ---------------------------------------------------------------

def test_a_binary_off_the_service_path_is_still_found(monkeypatch, tmp_path):
    """The reported bug. A systemd service's PATH is not a login shell's, and
    /usr/sbin and /usr/local/bin are routinely missing from it — so `which`
    alone reported a perfectly present Tailscale as absent."""
    installed = tmp_path / "tailscale"
    installed.write_text("#!/bin/sh\n")
    monkeypatch.setattr(remote.shutil, "which", lambda _: None)
    monkeypatch.setattr(remote, "_CLI_PATHS", (str(installed),))
    assert remote.installed()
    assert remote.cli_path() == str(installed)


def test_not_installed_still_offers_a_way_forward(client, monkeypatch):
    """The card's whole failure was stating a fact with no action attached."""
    monkeypatch.setattr(remote, "cli_path", lambda: "")
    monkeypatch.setattr(remote, "can_install", lambda: True)
    body = client.get("/api/remote/status").json()
    assert body["installed"] is False
    assert body["can_install"] is True, "no install path offered — a dead end"


def test_an_install_button_is_not_offered_where_it_cannot_work(monkeypatch):
    """A button that must fail should not be drawn at all."""
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    assert remote.can_install() is False


# -- status ------------------------------------------------------------------

def _status_run(payload, returncode=0):
    def fake(cmd, *a, **kw):
        return subprocess.CompletedProcess(cmd, returncode, json.dumps(payload), "")
    return fake


def test_a_connected_camera_reports_its_https_name(client, monkeypatch):
    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(remote.subprocess, "run", _status_run(
        {"BackendState": "Running", "Self": {"DNSName": "skylapse.tail1234.ts.net."}}))
    body = client.get("/api/remote/status").json()
    assert body["connected"] is True
    assert body["url"] == "https://skylapse.tail1234.ts.net"


def test_a_status_we_cannot_read_is_reported_not_swallowed(client, monkeypatch):
    """Previously any failure here collapsed to state=error with nothing to
    show, which looks identical to not being set up."""
    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")

    def fake(cmd, *a, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "is tailscaled running?")

    monkeypatch.setattr(remote.subprocess, "run", fake)
    body = client.get("/api/remote/status").json()
    assert body["state"] == "error"
    assert "tailscaled" in body["error"]


def test_status_falls_back_to_the_helper_when_the_socket_is_root_only(monkeypatch):
    """Reading status usually works unprivileged, so it is tried directly to
    keep the poll cheap. Where it does not, sudo has to cover it."""
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        if cmd[0] == "sudo":
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"BackendState": "NeedsLogin"}), "")
        return subprocess.CompletedProcess(cmd, 1, "", "permission denied")

    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(remote.subprocess, "run", fake)
    assert remote.status()["state"] == "needslogin"
    assert any(c[0] == "sudo" for c in calls)


# -- installing --------------------------------------------------------------

def test_installing_goes_through_the_privileged_helper(client, monkeypatch):
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "tailscale-install ready", "")

    monkeypatch.setattr(remote, "cli_path", lambda: "")
    monkeypatch.setattr(remote, "can_install", lambda: True)
    monkeypatch.setattr(remote.subprocess, "run", fake)
    assert client.post("/api/remote/install").status_code == 200
    assert any("tailscale-install" in c for c in calls)
    assert calls[0][:2] == ["sudo", "-n"], "the API is not root; this needs sudo"


def test_a_failed_install_says_why(client, monkeypatch):
    def fake(cmd, *a, **kw):
        return subprocess.CompletedProcess(
            cmd, 6, "", "could not fetch the Tailscale signing key for trixie")

    monkeypatch.setattr(remote, "cli_path", lambda: "")
    monkeypatch.setattr(remote, "can_install", lambda: True)
    monkeypatch.setattr(remote.subprocess, "run", fake)
    r = client.post("/api/remote/install")
    assert r.status_code == 500
    assert "signing key" in r.json()["detail"]
    # And it persists into the card, so the reason survives the failed request.
    assert "signing key" in client.get("/api/remote/status").json()["error"]


def test_installing_when_it_is_already_there_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(remote.subprocess, "run",
                        lambda *a, **kw: calls.append(a))
    assert remote.install()["ok"] is True
    assert not calls


# -- signing in --------------------------------------------------------------

class FakeProc:
    """Stands in for `sudo skylapse-admin tailscale-up`, which prints a login
    URL and then blocks until the login completes."""

    def __init__(self, lines, code=0):
        self.stdout = iter(lines)
        self._code = code
        self.killed = False

    # Patching Popen patches it for subprocess.run too, which drives it as a
    # context manager. Tests that stub the login therefore avoid calling
    # anything else that shells out.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self, timeout=None):
        return self._code

    def poll(self):
        return self._code

    def kill(self):
        self.killed = True


def test_enable_captures_the_login_url_and_publishes_https(monkeypatch):
    """The URL is the whole point — it becomes the QR code. And serve() must
    run once the login lands, or the address the card then shows serves
    nothing."""
    served = []
    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(remote, "serve", lambda: served.append(True) or True)
    monkeypatch.setattr(remote.subprocess, "Popen", lambda *a, **kw: FakeProc([
        "To authenticate, visit:\n",
        "\thttps://login.tailscale.com/a/1234abcd\n",
    ]))
    remote.enable()
    for _ in range(50):                     # the runner is a thread
        if served:
            break
        time.sleep(0.02)
    assert served, "logged in but never published the UI on the tailnet"


def test_enable_is_refused_when_tailscale_is_missing(client, monkeypatch):
    monkeypatch.setattr(remote, "cli_path", lambda: "")
    r = client.post("/api/remote/enable")
    assert r.status_code == 409
    assert "not installed" in r.json()["detail"]


def test_a_login_nobody_completes_does_not_leave_root_running(monkeypatch):
    """`tailscale up` blocks forever waiting for a login. Left alone that is a
    root process and a QR code for a URL that has since expired."""
    proc = FakeProc(["https://login.tailscale.com/a/1234abcd\n"])

    def wait(timeout=None):
        raise subprocess.TimeoutExpired("tailscale", timeout or 0)

    proc.wait = wait
    proc.poll = lambda: None
    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(remote.subprocess, "Popen", lambda *a, **kw: proc)
    remote.enable()
    for _ in range(50):
        if proc.killed:
            break
        time.sleep(0.02)
    assert proc.killed
    # Read straight from the module rather than through status(), which would
    # shell out through the Popen this test has replaced.
    assert "again" in remote._read_error()


def test_disable_leaves_the_tailnet(client, monkeypatch):
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(remote, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(remote.subprocess, "run", fake)
    assert client.post("/api/remote/disable").status_code == 200
    assert any("tailscale-down" in c for c in calls)


# -- the helper --------------------------------------------------------------

@pytest.mark.skipif(not BASH, reason="needs bash")
class TestTheScript:
    def run(self, *args, **env):
        return subprocess.run([BASH, str(ADMIN), *args], capture_output=True,
                              text=True, env={**os.environ, **env})

    def test_an_unidentifiable_os_is_refused(self):
        """The codename is interpolated into a download URL. Anything that is
        not a plain release name is a system we do not know how to serve, and
        guessing would put an unknown apt source on the camera."""
        result = self.run("tailscale-install", SKYLAPSE_OS_CODENAME="../../evil",
                          PATH="/usr/bin:/bin")
        assert result.returncode == 4
        assert "cannot identify" in result.stderr

    def test_the_verbs_are_a_closed_list(self):
        result = self.run("tailscale-uninstall-everything", PATH="/usr/bin:/bin")
        assert result.returncode == 2
        assert "usage:" in result.stderr
