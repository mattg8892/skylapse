"""USB export.

The tests that matter here are the negative ones. A Raspberry Pi's boot SD
card reports `rm: 1` (removable), so any filter built on removability alone
would cheerfully offer the running system's own card as an export target.
These pin the rule that actually keeps it out.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from skylapse import config, usbexport

# A real-shaped lsblk tree: boot SD (removable! not USB) plus a USB stick.
LSBLK = {
    "blockdevices": [
        {"name": "mmcblk0", "path": "/dev/mmcblk0", "size": 250000000000,
         "type": "disk", "tran": None, "rm": True, "mountpoint": None,
         "label": None, "fstype": None, "pkname": None,
         "children": [
             {"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "size": 536870912,
              "type": "part", "tran": None, "rm": True,
              "mountpoint": "/boot/firmware", "label": "bootfs",
              "fstype": "vfat", "pkname": "mmcblk0"},
             {"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "size": 249000000000,
              "type": "part", "tran": None, "rm": True, "mountpoint": "/",
              "label": "rootfs", "fstype": "ext4", "pkname": "mmcblk0"},
         ]},
        {"name": "sda", "path": "/dev/sda", "size": 64000000000, "type": "disk",
         "tran": "usb", "rm": True, "mountpoint": None, "label": None,
         "fstype": None, "pkname": None,
         "children": [
             {"name": "sda1", "path": "/dev/sda1", "size": 64000000000,
              "type": "part", "tran": "usb", "rm": True, "mountpoint": None,
              "label": "FIELD", "fstype": "exfat", "pkname": "sda"},
         ]},
    ]
}


@pytest.fixture()
def fake_lsblk(monkeypatch):
    def run(cmd, timeout=20):
        if cmd[0] == "lsblk" and "-J" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(LSBLK), "")
        if cmd[0] == "findmnt":
            return subprocess.CompletedProcess(cmd, 0, "/dev/mmcblk0p2\n", "")
        if cmd[0] == "lsblk":                      # PKNAME lookup
            return subprocess.CompletedProcess(cmd, 0, "mmcblk0\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")
    monkeypatch.setattr(usbexport, "_run", run)


def test_boot_sd_is_never_offered(fake_lsblk):
    devices = [d["device"] for d in usbexport.list_drives()]
    assert "/dev/mmcblk0p1" not in devices
    assert "/dev/mmcblk0p2" not in devices, "the running root filesystem was offered"


def test_usb_stick_is_offered(fake_lsblk):
    drives = usbexport.list_drives()
    assert [d["device"] for d in drives] == ["/dev/sda1"]
    assert drives[0]["label"] == "FIELD"
    assert drives[0]["fstype"] == "exfat"


def test_usb_boot_disk_is_excluded(monkeypatch):
    """A Pi booted from USB: the stick is on a USB transport AND is root."""
    def run(cmd, timeout=20):
        if cmd[0] == "lsblk" and "-J" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(LSBLK), "")
        if cmd[0] == "findmnt":
            return subprocess.CompletedProcess(cmd, 0, "/dev/sda1\n", "")
        if cmd[0] == "lsblk":
            return subprocess.CompletedProcess(cmd, 0, "sda\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")
    monkeypatch.setattr(usbexport, "_run", run)
    assert usbexport.list_drives() == []


def test_unknown_root_disk_fails_closed(monkeypatch):
    """If we cannot identify the boot disk we cannot prove a drive is safe."""
    def run(cmd, timeout=20):
        if cmd[0] == "lsblk" and "-J" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(LSBLK), "")
        return subprocess.CompletedProcess(cmd, 1, "", "boom")
    monkeypatch.setattr(usbexport, "_run", run)
    assert usbexport.list_drives() == []


def test_unformatted_partition_is_skipped(monkeypatch):
    tree = json.loads(json.dumps(LSBLK))
    tree["blockdevices"][1]["children"][0]["fstype"] = None
    def run(cmd, timeout=20):
        if cmd[0] == "lsblk" and "-J" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(tree), "")
        if cmd[0] == "findmnt":
            return subprocess.CompletedProcess(cmd, 0, "/dev/mmcblk0p2\n", "")
        return subprocess.CompletedProcess(cmd, 0, "mmcblk0\n", "")
    monkeypatch.setattr(usbexport, "_run", run)
    assert usbexport.list_drives() == []


# -- planning ---------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_ROOT", tmp_path)
    night = tmp_path / "cam" / "2026-08-13"
    night.mkdir(parents=True)
    (night / "timelapse_2026-08-13.mp4").write_bytes(b"m" * 1000)
    for i in range(3):
        (night / f"img_2026081{i}_120000.jpg").write_bytes(b"j" * 100)
        (night / f"img_2026081{i}_120000.json").write_bytes(b"{}")
        (night / f"img_2026081{i}_120000.dng").write_bytes(b"d" * 5000)
    return night


def test_plan_timelapse_only(store):
    job = usbexport.plan("cam", ["2026-08-13"],
                         {"timelapse": True, "jpegs": False, "raws": False})
    assert job["files_total"] == 1
    assert job["bytes_total"] == 1000


def test_plan_jpegs_include_sidecars(store):
    job = usbexport.plan("cam", ["2026-08-13"],
                         {"timelapse": False, "jpegs": True, "raws": False})
    assert job["files_total"] == 6, "sidecars must travel with their frames"


def test_plan_raws(store):
    job = usbexport.plan("cam", ["2026-08-13"],
                         {"timelapse": False, "jpegs": False, "raws": True})
    assert job["bytes_total"] == 15000


def test_plan_destination_layout(store):
    job = usbexport.plan("cam", ["2026-08-13"],
                         {"timelapse": True, "jpegs": False, "raws": False})
    _, rel = job["items"][0]
    assert rel == "Skylapse/cam/2026-08-13/timelapse_2026-08-13.mp4"


def test_plan_ignores_missing_nights(store):
    job = usbexport.plan("cam", ["1999-01-01"], {"timelapse": True})
    assert job["files_total"] == 0


def test_start_refuses_when_the_drive_is_too_small(store, tmp_path, monkeypatch):
    target = tmp_path / "stick"
    target.mkdir()
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(usbexport, "_drive",
                        lambda dev: {"device": dev, "mountpoint": str(target)})
    monkeypatch.setattr(usbexport.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 10, "total": 10, "used": 0})())

    result = usbexport.start("/dev/sda1", "cam", ["2026-08-13"],
                             {"timelapse": True, "jpegs": True, "raws": True})
    assert result["ok"] is False
    assert "space" in result["error"].lower()
    assert result["required_bytes"] > result["free_bytes"]
    assert not list(target.rglob("*")), "refused export must not write anything"
