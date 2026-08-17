"""Declaring a camera sensor by hand, from the wizard.

Raspberry Pi OS detects cameras by reading an EEPROM. Third-party boards — the
very common HQ/IMX477 clones among them — do not carry one, so on a stock image
such a camera is simply invisible, and the only fix was editing
/boot/firmware/config.txt over SSH. That is precisely what an appliance image
exists to avoid. Found on this project's own hardware.

The script under test writes to a root-owned boot file, so it is exercised
directly rather than reimplemented here: a bug in it costs a card that does not
come back up.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api

ADMIN = Path(__file__).resolve().parents[1] / "scripts" / "skylapse-admin"
BASH = shutil.which("bash")


@pytest.fixture
def boot_config(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text("[all]\ncamera_auto_detect=1\ndtoverlay=vc4-kms-v3d\n")
    return path


def run_admin(boot_config, *args):
    return subprocess.run([BASH, str(ADMIN), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin",
                               "SKYLAPSE_CONFIG_TXT": str(boot_config)})


@pytest.mark.skipif(not BASH, reason="needs bash")
class TestTheScript:
    def test_an_unknown_sensor_is_refused(self, boot_config):
        """This value reaches a root-owned boot file. Anything not on the list
        is refused rather than written and discovered at the next boot, when the
        camera is on a pole and the only feedback is that it did not return."""
        before = boot_config.read_text()
        result = run_admin(boot_config, "camera-overlay", "; rm -rf /")
        assert result.returncode != 0
        assert boot_config.read_text() == before

    def test_both_ports_are_declared(self, boot_config):
        """Which connector a camera is in is not something anyone should have
        to know. The empty port fails its probe harmlessly — that is what a
        working rig's log looks like."""
        assert run_admin(boot_config, "camera-overlay", "imx477").returncode == 0
        text = boot_config.read_text()
        assert "dtoverlay=imx477,cam0" in text
        assert "dtoverlay=imx477,cam1" in text

    def test_auto_detect_is_turned_off(self, boot_config):
        """It takes precedence over an explicit overlay, so a declared sensor
        cannot bind while it is on."""
        run_admin(boot_config, "camera-overlay", "imx477")
        assert "camera_auto_detect=0" in boot_config.read_text()
        assert "camera_auto_detect=1" not in boot_config.read_text()

    def test_running_twice_does_not_duplicate(self, boot_config):
        """Someone will press it twice. A boot config with the same overlay
        four times is not obviously harmful, but it is the kind of mess that
        makes the next problem harder to read."""
        run_admin(boot_config, "camera-overlay", "imx477")
        run_admin(boot_config, "camera-overlay", "imx477")
        assert boot_config.read_text().count("dtoverlay=imx477,cam0") == 1

    def test_the_original_is_kept(self, boot_config):
        run_admin(boot_config, "camera-overlay", "imx477")
        backup = boot_config.with_suffix(".txt.skylapse-backup")
        assert backup.exists()
        assert "camera_auto_detect=1" in backup.read_text()

    def test_the_backup_is_not_overwritten_by_a_second_run(self, boot_config):
        """The first run's backup is the one worth having — it is the only copy
        of what the camera booted with before Skylapse touched anything."""
        run_admin(boot_config, "camera-overlay", "imx477")
        run_admin(boot_config, "camera-overlay", "imx219")
        backup = boot_config.with_suffix(".txt.skylapse-backup")
        assert "camera_auto_detect=1" in backup.read_text()
        assert "imx477" not in backup.read_text()

    def test_a_second_sensor_can_be_added(self, boot_config):
        """Picking the wrong one first must not be a dead end."""
        run_admin(boot_config, "camera-overlay", "imx477")
        run_admin(boot_config, "camera-overlay", "imx708")
        text = boot_config.read_text()
        assert "dtoverlay=imx708,cam0" in text and "dtoverlay=imx477,cam0" in text


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


def test_the_api_refuses_an_unknown_sensor(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api.subprocess, "run",
                        lambda *a, **kw: calls.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    assert client.post("/api/setup/camera/overlay",
                       json={"sensor": "hacker"}).status_code == 400
    assert not calls, "shelled out with an unvalidated sensor name"


def test_the_api_enables_and_reboots(client, monkeypatch):
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "enabled", "")

    monkeypatch.setattr(api.subprocess, "run", fake)
    assert client.post("/api/setup/camera/overlay",
                       json={"sensor": "imx477"}).status_code == 200
    assert any("camera-overlay" in c and "imx477" in c for c in calls)
    assert any("reboot" in c for c in calls), "never restarted, so nothing changes"


def test_a_failed_write_does_not_reboot(client, monkeypatch):
    """Rebooting after a failed edit is a reboot for nothing, and it takes the
    page down with it."""
    calls = []

    def fake(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "no sudo rights")

    monkeypatch.setattr(api.subprocess, "run", fake)
    assert client.post("/api/setup/camera/overlay",
                       json={"sensor": "imx477"}).status_code == 500
    assert not any("reboot" in c for c in calls)


def test_the_offered_sensors_match_what_the_script_accepts():
    """The UI list and the script's closed list must not drift: an option the
    script rejects is a button that fails after someone has committed to it."""
    allowed = ADMIN.read_text().split("SENSORS=")[1].split("\n")[0]
    for value, _ in api.CAMERA_SENSORS:
        assert value in allowed, f"{value} is offered but the script refuses it"
