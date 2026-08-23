"""Restarting the camera from the camera.

The privileged helper has had a `reboot` verb since the camera-overlay flow
needed one, but nothing in Settings could reach it. So the dew heater's I2C
switch said "enabled; reboot to finish" and offered no way to do it, and the
advice became "pull the power".

That is not the same operation. A cold pull skips the filesystem sync, and
doing it seconds after a boot-config write is a good way to corrupt the card.
The rig it happened on did not come back.
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


def test_reboot_goes_through_the_privileged_helper(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "rebooting", ""))
    assert client.post("/api/system/reboot").status_code == 200
    assert calls, "did not shell out at all"
    cmd = calls[0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("skylapse-admin"), \
        "sudo authorises the helper, not an argument list that can drift"
    assert cmd[3] == "reboot"


def test_a_refused_reboot_is_reported_not_swallowed(client, monkeypatch):
    """A button that silently does nothing is worse than no button: the user
    waits for a restart that is not coming, then pulls the power anyway."""
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw:
                        subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required"))
    r = client.post("/api/system/reboot")
    assert r.status_code == 500
    assert "password" in r.json()["detail"]


def test_the_i2c_switch_says_when_a_reboot_is_needed(client, monkeypatch):
    """As a flag, not a sentence.

    The UI has to branch on this to offer the restart, and branching on the
    wording of a human-readable note is how that quietly stops working the
    next time the note is reworded.
    """
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw:
                        subprocess.CompletedProcess(cmd, 0, "i2c enabled; needs a reboot", ""))
    monkeypatch.setattr(api.Path, "exists", lambda self: False)
    body = client.post("/api/dewheater/i2c").json()
    assert body["needs_reboot"] is True
    assert body["i2c_ready"] is False


def test_no_reboot_is_claimed_when_the_bus_came_up_live(client, monkeypatch):
    """Sometimes the controller is already on and only the module was missing.
    Asking for a restart that is not needed costs a minute of sky."""
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw:
                        subprocess.CompletedProcess(cmd, 0, "i2c ready", ""))
    monkeypatch.setattr(api.Path, "exists", lambda self: True)
    body = client.post("/api/dewheater/i2c").json()
    assert body["needs_reboot"] is False
    assert body["i2c_ready"] is True


def test_the_helper_schedules_the_reboot_rather_than_running_it_inline():
    """`/sbin/reboot` called directly would kill the API mid-response, which
    looks like a crash from the browser. The helper hands it to systemd on a
    short timer so the response gets out first."""
    from pathlib import Path
    admin = (Path(__file__).resolve().parents[1] / "scripts" / "skylapse-admin"
             ).read_text(encoding="utf-8")
    body = admin.split("reboot)", 1)[1][:400]
    assert "systemd-run" in body and "--on-active" in body, \
        "the reboot must be scheduled, not run inline"
