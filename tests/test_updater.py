"""Updater: version comparison, channel selection, and health gating.

The expensive failure is a headless rig that updates into a broken build and
stops capturing, so the rollback path is exercised here rather than trusted.
"""
from __future__ import annotations

import json

import pytest

from skylapse import config, updater


@pytest.mark.parametrize("text,expected", [
    ("v0.1.0", (0, 1, 0)),
    ("0.1.0", (0, 1, 0)),
    ("v1.2.3-beta", (1, 2, 3)),
    ("v2.0", (2, 0)),
    ("garbage", (0,)),
])
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


@pytest.mark.parametrize("candidate,current,newer", [
    ("v0.2.0", "0.1.0", True),
    ("v0.1.1", "0.1.0", True),
    ("v1.0.0", "0.9.9", True),
    ("v0.1.0", "0.1.0", False),      # same version is not an update
    ("v0.0.9", "0.1.0", False),      # never offer a downgrade
    ("v0.2", "0.2.0", False),        # 0.2 and 0.2.0 are the same release
    ("v0.10.0", "0.9.0", True),      # numeric, not lexicographic
])
def test_is_newer(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


def test_release_check_offers_a_newer_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda: {
        "tag_name": "v0.2.0", "body": "notes", "name": "0.2.0",
        "html_url": "http://example/r"})
    result = updater.check(force=True)
    assert result["available"] is True
    assert result["latest"] == "0.2.0"
    assert result["target_ref"] == "v0.2.0"


def test_release_check_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "__version__", "0.2.0")
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda: {
        "tag_name": "v0.2.0", "body": "", "name": ""})
    assert updater.check(force=True)["available"] is False


def test_check_survives_github_being_unreachable(tmp_path, monkeypatch):
    """No network must not look like an available update, or an error page."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda: None)
    result = updater.check(force=True)
    assert result["available"] is False
    assert "error" in result


def test_check_uses_the_cache_within_the_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    calls = []

    def fetch():
        calls.append(1)
        return {"tag_name": "v9.9.9", "body": "", "name": ""}

    monkeypatch.setattr(updater, "_fetch_latest_release", fetch)
    updater.check(force=True)
    updater.check()                       # inside the TTL
    assert len(calls) == 1, "cached check still hit the network"


def test_force_bypasses_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(updater, "_fetch_latest_release",
                        lambda: (calls.append(1), {"tag_name": "v9.9.9",
                                                   "body": "", "name": ""})[1])
    updater.check(force=True)
    updater.check(force=True)
    assert len(calls) == 2


def test_dev_channel_follows_main(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    cfg = config.Config()
    cfg.updates.channel = "dev"
    monkeypatch.setattr(config, "load", lambda: cfg)

    outputs = {
        "rev-parse HEAD": "aaaaaaaaaaaa",
        "rev-parse origin/main": "bbbbbbbbbbbb",
        "rev-list --count HEAD..origin/main": "3",
        "log -1 --format=%s origin/main": "newest commit",
    }

    def fake_run(cmd, timeout=300, cwd=None):
        key = " ".join(cmd[1:])
        class R:
            stdout = outputs.get(key, "")
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(updater, "_run", fake_run)
    result = updater.check(force=True)
    assert result["channel"] == "dev"
    assert result["available"] is True
    assert result["commits_behind"] == 3
    assert result["target_ref"] == "origin/main"


def test_dev_channel_at_head_offers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    cfg = config.Config()
    cfg.updates.channel = "dev"
    monkeypatch.setattr(config, "load", lambda: cfg)

    def fake_run(cmd, timeout=300, cwd=None):
        class R:
            stdout = "same-sha" if "rev-parse" in cmd else "0"
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(updater, "_run", fake_run)
    assert updater.check(force=True)["available"] is False


def test_start_refuses_a_concurrent_update(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / updater.STATUS_NAME).write_text(json.dumps({"state": "running"}))
    result = updater.start("v0.2.0")
    assert result["ok"] is False


def test_unhealthy_update_rolls_back(tmp_path, monkeypatch):
    """The whole point of the feature: a bad build must not leave the rig dark."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    checkouts = []

    def fake_run(cmd, timeout=300, cwd=None):
        class R:
            returncode = 0
            stderr = ""
            stdout = "priorsha" if cmd[:2] == ["git", "rev-parse"] else ""
        if cmd[:2] == ["git", "checkout"]:
            checkouts.append(cmd[-1])
        return R()

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater, "_changed_paths", lambda a, b: set())
    monkeypatch.setattr(updater, "_restart_services", lambda: True)
    monkeypatch.setattr(updater, "_restarts_took_effect",
                        lambda before, timeout_s=30: True)
    # Never healthy after the update; healthy again after the rollback.
    healthy = iter([False, True])
    monkeypatch.setattr(updater, "_wait_healthy",
                        lambda deadline_s=60: next(healthy, True))

    result = updater.apply("v9.9.9", apply_now=True)
    assert result["ok"] is False
    assert result["rolled_back"] is True
    assert checkouts == ["v9.9.9", "priorsha"], "did not return to the prior ref"
    assert updater.status()["state"] == "rolled_back"


def test_successful_update_does_not_roll_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    checkouts = []

    def fake_run(cmd, timeout=300, cwd=None):
        class R:
            returncode = 0
            stderr = ""
            stdout = "priorsha" if cmd[:2] == ["git", "rev-parse"] else ""
        if cmd[:2] == ["git", "checkout"]:
            checkouts.append(cmd[-1])
        return R()

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater, "_changed_paths", lambda a, b: set())
    monkeypatch.setattr(updater, "_restart_services", lambda: True)
    monkeypatch.setattr(updater, "_restarts_took_effect",
                        lambda before, timeout_s=30: True)
    monkeypatch.setattr(updater, "_wait_healthy", lambda deadline_s=60: True)

    assert updater.apply("v0.2.0", apply_now=True)["ok"] is True
    assert checkouts == ["v0.2.0"]
    assert updater.status()["state"] == "done"


def test_a_restart_that_never_happened_is_not_healthy(tmp_path, monkeypatch):
    """Regression: a silently-failed restart left the OLD process running the
    OLD code, which passed every health check and let a broken build through.
    """
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    checkouts = []

    def fake_run(cmd, timeout=300, cwd=None):
        class R:
            returncode = 0
            stderr = ""
            stdout = "priorsha" if cmd[:2] == ["git", "rev-parse"] else ""
        if cmd[:2] == ["git", "checkout"]:
            checkouts.append(cmd[-1])
        return R()

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater, "_changed_paths", lambda a, b: set())
    monkeypatch.setattr(updater, "_restart_services", lambda: True)
    monkeypatch.setattr(updater, "_restarts_took_effect",
                        lambda before, timeout_s=30: False)
    # Everything else looks fine, because the old process is still serving.
    monkeypatch.setattr(updater, "_wait_healthy", lambda deadline_s=60: True)

    result = updater.apply("v9.9.9", apply_now=True)
    assert result["ok"] is False, "unrestarted services were accepted as updated"
    assert checkouts == ["v9.9.9", "priorsha"]


def test_restarts_took_effect_detects_an_unchanged_unit(monkeypatch):
    """The identity token must actually change, or the gate is decorative."""
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    monkeypatch.setattr(updater, "service_start_id", lambda unit: "same")
    assert updater._restarts_took_effect({u: "same" for u in updater.SERVICES},
                                         timeout_s=1) is False

    monkeypatch.setattr(updater, "service_start_id", lambda unit: "new")
    assert updater._restarts_took_effect({u: "old" for u in updater.SERVICES},
                                         timeout_s=1) is True


def test_failed_restart_command_rolls_back(tmp_path, monkeypatch):
    """apply() used to ignore the restart's return value entirely."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    checkouts = []

    def fake_run(cmd, timeout=300, cwd=None):
        class R:
            returncode = 0
            stderr = ""
            stdout = "priorsha" if cmd[:2] == ["git", "rev-parse"] else ""
        if cmd[:2] == ["git", "checkout"]:
            checkouts.append(cmd[-1])
        return R()

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater, "_changed_paths", lambda a, b: set())
    monkeypatch.setattr(updater, "_restart_services", lambda: False)
    monkeypatch.setattr(updater, "_wait_healthy", lambda deadline_s=60: True)

    assert updater.apply("v9.9.9", apply_now=True)["ok"] is False
    assert checkouts == ["v9.9.9", "priorsha"]


def test_worker_escapes_the_api_cgroup(monkeypatch):
    """Regression: a plain child of skylapse-api is killed when that unit
    restarts, which is the one thing the updater must do."""
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/systemd-run")
    cmd = updater._worker_command("v0.2.0", apply_now=True)
    assert cmd[0] == "sudo" and "systemd-run" in cmd
    assert any(a.startswith("--uid=") for a in cmd), "would run the checkout as root"
    assert cmd[-1] == "--now" and "v0.2.0" in cmd


def test_worker_falls_back_without_systemd_run(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)
    cmd = updater._worker_command("v0.2.0", apply_now=False)
    assert cmd[0] != "sudo"
    assert cmd[-2:] == ["apply", "v0.2.0"]


def test_build_only_runs_what_changed(tmp_path, monkeypatch):
    """pip install and npm build are minutes on a Pi; most updates need neither."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    ran = []
    monkeypatch.setattr(updater, "_run",
                        lambda cmd, timeout=300, cwd=None: ran.append(cmd[0]))

    updater._build({"skylapse/daemon/main.py"})
    assert ran == [], "rebuilt for a change that touched neither deps nor web"

    updater._build({"pyproject.toml"})
    assert any("pip" in c for c in ran)

    ran.clear()
    updater._build({"web/src/App.jsx"})
    assert ran == ["npm"]
