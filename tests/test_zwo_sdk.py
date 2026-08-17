"""Installing ZWO's SDK from the web UI instead of over SSH.

ZWO's licence forbids redistributing their library and their download portal is
browser-only, so it cannot ship in the SD image — which made a ZWO rig the one
Skylapse setup that still needed a terminal. The button that fixes that
downloads an unsigned third-party binary and installs it into /usr/local/lib as
root, so the interesting tests here are the ones about refusing: a bad checksum,
an architecture we have never run, a request that never accepted the licence.

The script is exercised directly rather than reimplemented, for the same reason
the camera-overlay tests do it: a bug in it lands in a system directory on a
camera that is usually on a roof.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skylapse import config, zwosdk
from skylapse.api import main as api

ADMIN = Path(__file__).resolve().parents[1] / "scripts" / "skylapse-admin"
BASH = shutil.which("bash")
CURL = shutil.which("curl")

LIB_PAYLOAD = b"\x7fELF not really a library, but it hashes like one"
RULES_PAYLOAD = b'SUBSYSTEMS=="usb", ATTR{idVendor}=="03c3", MODE="0666"\n'


def sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def mirror(tmp_path):
    """A stand-in for the INDI mirror, served to the script over file://.

    The script's URL is overridable precisely so this can run: the real fetch
    is 4 MB from GitHub, which is neither a thing to depend on in a test nor a
    thing to skip testing.
    """
    root = tmp_path / "mirror"
    (root / "armv8").mkdir(parents=True)
    (root / "armv8" / "libASICamera2.bin").write_bytes(LIB_PAYLOAD)
    (root / "99-asi.rules").write_bytes(RULES_PAYLOAD)
    return root


@pytest.fixture
def install_dirs(tmp_path):
    return tmp_path / "lib", tmp_path / "udev"


def run_admin(mirror, install_dirs, *, lib_sha=None, rules_sha=None,
              arch="aarch64"):
    lib_dir, udev_dir = install_dirs
    # The ambient environment rather than a minimal one: this verb shells out to
    # curl, which lives somewhere different on every machine that runs the
    # suite, and a test that fails because it could not find curl proves
    # nothing about the script.
    env = {
        **os.environ,
        "SKYLAPSE_ARCH": arch,
        # as_uri() so this works from Windows too, where curl is the native
        # build and does not understand a /c/... path.
        "SKYLAPSE_ZWO_BASE_URL": mirror.as_uri(),
        "SKYLAPSE_ZWO_LIB_SHA256": lib_sha or sha256(LIB_PAYLOAD),
        "SKYLAPSE_ZWO_RULES_SHA256": rules_sha or sha256(RULES_PAYLOAD),
        # as_posix() for the same reason as as_uri() above: a Windows path with
        # backslashes reaches bash as escape sequences, and the script's
        # "already installed?" check then silently never matches.
        "SKYLAPSE_LIB_DIR": lib_dir.as_posix(),
        "SKYLAPSE_UDEV_DIR": udev_dir.as_posix(),
    }
    return subprocess.run([BASH, str(ADMIN), "zwo-sdk"],
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(not BASH or not CURL, reason="needs bash and curl")
class TestTheScript:
    def test_it_installs_the_library_and_the_rules(self, mirror, install_dirs):
        result = run_admin(mirror, install_dirs)
        assert result.returncode == 0, result.stderr
        lib_dir, udev_dir = install_dirs
        # The versioned file plus both links: the driver dlopens the bare
        # .so name and the loader resolves the soname, so a missing link is a
        # camera that is present and cannot be opened.
        assert (lib_dir / f"libASICamera2.so.{zwosdk.SDK_VERSION}").read_bytes() \
            == LIB_PAYLOAD
        assert (lib_dir / "libASICamera2.so.1").exists()
        assert (lib_dir / "libASICamera2.so").exists()
        assert (udev_dir / "99-asi.rules").read_bytes() == RULES_PAYLOAD

    def test_a_mismatched_checksum_installs_nothing(self, mirror, install_dirs):
        """The whole reason the download is pinned. A mirror that changed under
        us, or anything on the wire, must not end up in /usr/local/lib — and
        must not leave a partial file there either, because the loader would
        find it and fail at dlopen, which reads as broken hardware."""
        result = run_admin(mirror, install_dirs, lib_sha="deadbeef")
        assert result.returncode != 0
        assert "checksum mismatch" in result.stderr
        lib_dir, _ = install_dirs
        assert not lib_dir.exists() or not any(lib_dir.iterdir())

    def test_the_rules_are_checksummed_too(self, mirror, install_dirs):
        result = run_admin(mirror, install_dirs, rules_sha="deadbeef")
        assert result.returncode != 0
        _, udev_dir = install_dirs
        assert not udev_dir.exists() or not any(udev_dir.iterdir())

    def test_running_it_again_is_a_no_op(self, mirror, install_dirs):
        """Someone will press the button twice, and the settings screen offers
        it for as long as the daemon has not re-probed."""
        assert run_admin(mirror, install_dirs).returncode == 0
        again = run_admin(mirror, install_dirs)
        assert again.returncode == 0
        assert "already installed" in again.stdout

    def test_a_32_bit_system_is_refused(self, mirror, install_dirs):
        """Only the armv8 build is installed, so only aarch64 is offered. A
        button that downloads the wrong architecture and then reports a camera
        fault is worse than one that is honestly absent."""
        result = run_admin(mirror, install_dirs, arch="armv7l")
        assert result.returncode == 4
        assert "64-bit" in result.stderr

    def test_an_unknown_verb_is_refused(self):
        result = subprocess.run([BASH, str(ADMIN), "zwo-sdk-please"],
                                capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin"})
        assert result.returncode == 2


# -- the module and the endpoints -------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


def test_status_reports_what_the_card_needs(client, monkeypatch):
    monkeypatch.setattr(zwosdk.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr("skylapse.daemon.drivers.zwo.sdk_path", lambda: "")
    body = client.get("/api/setup/zwo").json()
    assert body["supported"] is True
    assert body["installed"] is False
    assert body["version"] == zwosdk.SDK_VERSION
    assert body["license_url"].startswith("https://")


def test_status_reports_an_sdk_that_is_already_there(client, monkeypatch):
    """Read through the driver's own lookup rather than a second copy of the
    path list: a settings screen offering to install something already present
    is how two notions of "installed" announce themselves."""
    monkeypatch.setattr("skylapse.daemon.drivers.zwo.sdk_path",
                        lambda: "/usr/local/lib/libASICamera2.so")
    body = client.get("/api/setup/zwo").json()
    assert body["installed"] is True
    assert body["path"].endswith("libASICamera2.so")


def test_installing_without_accepting_the_licence_is_refused(client, monkeypatch):
    """The licence is the user's to accept. Nothing is downloaded until they
    have, so the request has to carry the acceptance rather than the server
    assuming it."""
    calls = []
    monkeypatch.setattr(zwosdk.subprocess, "run",
                        lambda *a, **kw: calls.append(a))
    assert client.post("/api/setup/zwo/install", json={}).status_code == 400
    assert not calls, "downloaded the SDK without the licence being accepted"


def test_a_32_bit_system_never_shells_out(client, monkeypatch):
    calls = []
    monkeypatch.setattr(zwosdk.platform, "machine", lambda: "armv7l")
    monkeypatch.setattr(zwosdk.subprocess, "run",
                        lambda *a, **kw: calls.append(a))
    r = client.post("/api/setup/zwo/install", json={"accept_terms": True})
    assert r.status_code == 500
    assert "64-bit" in r.json()["detail"]
    assert not calls


def test_install_restarts_the_daemon(client, monkeypatch):
    """The daemon only probes for a camera when it opens one, so without a
    restart a freshly installed SDK does nothing until the next night."""
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "zwo-sdk ready", "")

    monkeypatch.setattr(zwosdk.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(zwosdk.subprocess, "run", fake)
    body = client.post("/api/setup/zwo/install",
                       json={"accept_terms": True}).json()
    assert body["ok"] and body["restarted"]
    assert any("zwo-sdk" in c for c in calls)
    assert any("restart" in c for c in calls)


def test_a_failed_install_does_not_restart_the_daemon(client, monkeypatch):
    """Restarting capture to load something that was never installed is an
    outage in exchange for nothing."""
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 6, "", "checksum mismatch")

    monkeypatch.setattr(zwosdk.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(zwosdk.subprocess, "run", fake)
    r = client.post("/api/setup/zwo/install", json={"accept_terms": True})
    assert r.status_code == 500
    assert "checksum" in r.json()["detail"]
    assert not any("restart" in c for c in calls)


def test_a_failed_restart_still_counts_as_installed(client, monkeypatch):
    """The SDK is on disk either way. Reporting the install as failed because
    systemctl did not answer sends someone to fix the wrong thing."""
    def fake(cmd, *a, **kw):
        code = 1 if "restart" in cmd else 0
        return subprocess.CompletedProcess(cmd, code, "zwo-sdk ready", "no sudo")

    monkeypatch.setattr(zwosdk.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(zwosdk.subprocess, "run", fake)
    body = client.post("/api/setup/zwo/install",
                       json={"accept_terms": True}).json()
    assert body["ok"] is True
    assert body["restarted"] is False


def test_the_script_and_the_module_agree_on_the_version():
    """The version names the file the script writes and the one the UI reports.
    They drift the moment someone bumps the pin in one place."""
    line = [l for l in ADMIN.read_text().splitlines()
            if l.startswith("ZWO_SDK_VERSION=")][0]
    assert zwosdk.SDK_VERSION in line
