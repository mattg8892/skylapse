"""Reading the camera's own logs, without a terminal.

Every post-mortem on this project has run into the same wall. There is no SSH
on the rig, so the only way to see a log was to already have one -- and worse,
Raspberry Pi OS ships journald at Storage=auto, which is persistent only if
/var/log/journal exists, and it does not. So the journal lived in /run.

Which means the reboot that recovered the camera destroyed the record of why it
needed recovering. Every time. A camera died overnight with its dew heater
still lit and there was nothing left to read.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


def test_logs_go_through_the_privileged_helper(client, monkeypatch):
    """The API is unprivileged and cannot read another unit's journal. Adding
    the service user to systemd-journal would grant that permanently, for every
    unit on the box -- the helper keeps it to one audited path."""
    calls = []
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "some log line\n", ""))
    body = client.get("/api/logs?unit=skylapse-daemon&lines=50").json()
    assert body["text"] == "some log line\n"
    cmd = calls[0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("skylapse-admin")
    assert cmd[3] == "logs" and cmd[4] == "skylapse-daemon"


def test_an_unknown_unit_is_refused_without_shelling_out(client, monkeypatch):
    """This value reaches a sudo command line. A closed list, checked before
    the subprocess, not after."""
    calls = []
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    assert client.get("/api/logs?unit=sshd").status_code == 400
    assert not calls, "shelled out with an unvalidated unit name"


def test_the_line_count_is_bounded(client, monkeypatch):
    """A camera with a persistent journal has weeks of history and 512MB of
    RAM. An unbounded request is a way to kill the API from a URL bar."""
    seen = {}
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw: seen.update(lines=cmd[5]) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    client.get("/api/logs?lines=999999")
    assert int(seen["lines"]) <= 5000


def test_logs_download_as_a_file(client, monkeypatch):
    """For attaching to a bug report, which is the case that matters when the
    person reading it is not the person with the camera."""
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw:
                        subprocess.CompletedProcess(cmd, 0, "line\n", ""))
    r = client.get("/api/logs/download?unit=skylapse-daemon")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.text == "line\n"


def test_a_helper_failure_is_reported(client, monkeypatch):
    monkeypatch.setattr(api.subprocess, "run",
                        lambda cmd, *a, **kw:
                        subprocess.CompletedProcess(cmd, 1, "", "no journal"))
    assert client.get("/api/logs").status_code == 500


# -- the reason none of this worked before -----------------------------------

def test_the_installer_makes_the_journal_persistent():
    """Without this the rest of the file is decoration: the logs exist only
    until the reboot that someone performs to fix the problem."""
    installer = (REPO / "install.sh").read_text(encoding="utf-8")
    assert "logs-persist" in installer


def test_the_journal_is_capped():
    """This is an SD card, and one just died. Logging must never be the thing
    that wears it out."""
    admin = (REPO / "scripts" / "skylapse-admin").read_text(encoding="utf-8")
    block = admin.split("logs-persist)", 1)[1].split(";;", 1)[0]
    assert "Storage=persistent" in block
    assert "SystemMaxUse=" in block, "an uncapped journal will fill the card"


def test_the_helper_refuses_units_that_are_not_ours():
    """The unit name reaches journalctl as root."""
    admin = (REPO / "scripts" / "skylapse-admin").read_text(encoding="utf-8")
    # Not split on ";;" -- this action contains an inner `case`, and its own
    # ";;" cuts the block before the very check being asserted on here.
    block = admin.split("\n  logs)", 1)[1][:1200]
    assert "unknown unit" in block
    assert "skylapse-daemon" in block
