"""Updater: version comparison, channel selection, and health gating.

The expensive failure is a headless rig that updates into a broken build and
stops capturing, so the rollback path is exercised here rather than trusted.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error

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
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda: ({
        "tag_name": "v0.2.0", "body": "notes", "name": "0.2.0",
        "html_url": "http://example/r"}, "", 0.0))
    result = updater.check(force=True)
    assert result["available"] is True
    assert result["latest"] == "0.2.0"
    assert result["target_ref"] == "v0.2.0"


def test_release_check_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "__version__", "0.2.0")
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda: (
        {"tag_name": "v0.2.0", "body": "", "name": ""}, "", 0.0))
    assert updater.check(force=True)["available"] is False


def test_check_survives_github_being_unreachable(tmp_path, monkeypatch):
    """No network must not look like an available update, or an error page."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "_fetch_latest_release",
                        lambda: (None, "Could not reach GitHub.", 0.0))
    result = updater.check(force=True)
    assert result["available"] is False
    assert "error" in result


def test_check_uses_the_cache_within_the_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    calls = []

    def fetch():
        calls.append(1)
        return {"tag_name": "v9.9.9", "body": "", "name": ""}, "", 0.0

    monkeypatch.setattr(updater, "_fetch_latest_release", fetch)
    updater.check(force=True)
    updater.check()                       # inside the TTL
    assert len(calls) == 1, "cached check still hit the network"


def test_force_bypasses_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(updater, "_fetch_latest_release",
                        lambda: (calls.append(1), ({"tag_name": "v9.9.9",
                                                    "body": "", "name": ""},
                                                   "", 0.0))[1])
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
    monkeypatch.setattr(updater, "_restart_services", lambda: (True, ""))
    monkeypatch.setattr(updater, "_restarts_took_effect",
                        lambda before, timeout_s=30: True)
    # Never healthy after the update; healthy again after the rollback.
    healthy = iter([False, True])
    monkeypatch.setattr(updater, "_wait_healthy",
                        lambda *a, **k: next(healthy, True))

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
    monkeypatch.setattr(updater, "_restart_services", lambda: (True, ""))
    monkeypatch.setattr(updater, "_restarts_took_effect",
                        lambda before, timeout_s=30: True)
    monkeypatch.setattr(updater, "_wait_healthy", lambda *a, **k: True)

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
    monkeypatch.setattr(updater, "_restart_services", lambda: (True, ""))
    monkeypatch.setattr(updater, "_restarts_took_effect",
                        lambda before, timeout_s=30: False)
    # Everything else looks fine, because the old process is still serving.
    monkeypatch.setattr(updater, "_wait_healthy", lambda *a, **k: True)

    result = updater.apply("v9.9.9", apply_now=True)
    assert result["ok"] is False, "unrestarted services were accepted as updated"
    assert checkouts == ["v9.9.9", "priorsha"]


def test_stale_status_file_is_not_healthy(monkeypatch):
    """Regression: the daemon rewrites its status every frame, so a file left
    by the *previous* process made a crash-looping daemon look alive."""
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=30, cwd=None:
                        type("R", (), {"stdout": "active", "returncode": 0,
                                       "stderr": ""})())
    restart_at = 1_000_000.0
    monkeypatch.setattr(updater, "_read_daemon_status",
                        lambda: {"updated": restart_at - 30})   # written before
    assert updater._healthy(restart_at) is False
    monkeypatch.setattr(updater, "_read_daemon_status",
                        lambda: {"updated": restart_at + 5})    # written after
    assert updater._healthy(restart_at) is True


def test_health_requires_a_streak(monkeypatch):
    """A crash-looping unit is 'active' for a moment on every cycle, so one
    lucky sample must not count."""
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    samples = iter([True, False, True, False, True, False] * 20)
    monkeypatch.setattr(updater, "_healthy", lambda since: next(samples, False))
    assert updater._wait_healthy(0.0, deadline_s=1, streak=3) is False

    monkeypatch.setattr(updater, "_healthy", lambda since: True)
    assert updater._wait_healthy(0.0, deadline_s=10, streak=3) is True


def test_launch_failure_is_reported(tmp_path, monkeypatch):
    """A refused systemd-run once left the status pinned at 'waiting'."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater.shutil, "which", lambda n: "/usr/bin/systemd-run")
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=60, cwd=None:
                        type("R", (), {"returncode": 1, "stdout": "",
                                       "stderr": "Unit already exists"})())
    result = updater.start("v0.2.0", apply_now=True)
    assert result["ok"] is False
    assert "already exists" in result["error"]
    assert updater.status()["state"] == "error"


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
    monkeypatch.setattr(updater, "_restart_services",
                        lambda: (False, "restart-services exited 127"))
    monkeypatch.setattr(updater, "_wait_healthy", lambda *a, **k: True)

    assert updater.apply("v9.9.9", apply_now=True)["ok"] is False
    assert checkouts == ["v9.9.9", "priorsha"]


def test_worker_launches_through_the_privileged_helper(monkeypatch):
    """The launch goes through the one script sudo authorises.

    It used to build its own `sudo systemd-run …` line, matched against a
    sudoers rule that spelled the arguments out. They had drifted, so sudo
    matched no rule and the updater failed with "a password is required" on
    every install — the whole feature, unusable from the web UI.
    """
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/systemd-run")
    cmd = updater._worker_command("v0.2.0", apply_now=True)
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("skylapse-admin"), \
        "sudo authorises the helper, not an argument list that can drift"
    assert cmd[3] == "update" and "v0.2.0" in cmd and cmd[-1] == "--now"


def test_restart_goes_through_the_helper_too(monkeypatch):
    """The other half of the same bug: the sudoers rule named three units and
    this restarts two, so the restart failed and the update rolled itself back
    over a typo in a pattern."""
    seen = []
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=300, cwd=None:
                        seen.append(cmd) or
                        type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    assert updater._restart_services() == (True, "")
    assert seen[0][:2] == ["sudo", "-n"]
    assert seen[0][2].endswith("skylapse-admin")
    assert seen[0][3] == "restart-services"


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


# -- the privileged helper ---------------------------------------------------
#
# The launch is split across a Python function and a shell script, and the last
# time a contract like that was split across two files it silently broke the
# whole feature. These cover the shell half.

import shutil as _shutil                                          # noqa: E402
import subprocess as _subprocess                                  # noqa: E402
from pathlib import Path as _Path                                 # noqa: E402

ADMIN = _Path(__file__).resolve().parents[1] / "scripts" / "skylapse-admin"
BASH = _shutil.which("bash")


def _admin(*args, **env):
    return _subprocess.run([BASH, str(ADMIN), *args], capture_output=True,
                           text=True, env={**os.environ, "SKYLAPSE_PRINT_ONLY": "1",
                                           **env})


@pytest.mark.skipif(not BASH, reason="needs bash")
class TestTheLaunchCommand:
    def test_it_runs_the_worker_as_the_service_user_outside_the_cgroup(self):
        """--uid or the checkout ends up owned by root; a transient unit or the
        worker dies with the API it was launched from."""
        out = _admin("update", "v0.3.2", "--now", SUDO_USER="skylapse").stdout
        assert "systemd-run" in out
        assert "--uid=skylapse" in out
        assert "--unit=skylapse-update" in out
        assert out.rstrip().endswith("apply v0.3.2 --now")

    def test_a_deferred_update_is_not_told_to_apply_now(self):
        out = _admin("update", "v0.3.2", SUDO_USER="skylapse").stdout
        assert out.rstrip().endswith("apply v0.3.2")

    def test_a_ref_that_could_be_an_option_is_refused(self):
        """This reaches git as root. An argument that starts with a dash is not
        a version, it is a flag someone hopes we will pass through."""
        result = _admin("update", "--upload-pack=evil", SUDO_USER="skylapse")
        assert result.returncode == 3
        assert "unsafe ref" in result.stderr

    def test_a_ref_with_shell_characters_is_refused(self):
        result = _admin("update", "v1.0;reboot", SUDO_USER="skylapse")
        assert result.returncode == 3

    def test_a_missing_ref_is_refused(self):
        assert _admin("update", SUDO_USER="skylapse").returncode == 2


@pytest.mark.skipif(not BASH, reason="needs bash")
def test_reinstall_units_substitutes_the_checkout_and_its_owner(tmp_path):
    """The updater never touched /etc, so a unit or sudoers change did not
    reach any rig that updated instead of reflashing — which is how a broken
    sudoers rule survived three releases and could not be fixed by an update."""
    units, sudoers = tmp_path / "units", tmp_path / "sudoers.d" / "skylapse"
    sudoers.parent.mkdir(parents=True)
    root = _Path(__file__).resolve().parents[1]

    result = _subprocess.run(
        [BASH, str(ADMIN), "reinstall-units"], capture_output=True, text=True,
        env={**os.environ,
             "SKYLAPSE_ROOT": root.as_posix(),
             "SKYLAPSE_UNIT_DIR": units.as_posix(),
             "SKYLAPSE_SUDOERS_PATH": sudoers.as_posix()})
    assert result.returncode == 0, result.stderr

    written = (units / "skylapse-api.service").read_text()
    assert "@SKYLAPSE_ROOT@" not in written and "@SKYLAPSE_USER@" not in written
    assert root.as_posix() in written
    # And the sudoers entry, which is the one that must never be stale: it is
    # what authorises every privileged thing the app can do.
    assert "skylapse-admin" in sudoers.read_text()


def test_build_reinstalls_units_when_they_change(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=300, cwd=None:
                        seen.append(cmd) or
                        type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())

    updater._build({"skylapse/api/main.py"})
    assert not seen, "reinstalled units for a change that touched neither"

    updater._build({"systemd/skylapse-api.service"})
    assert any("reinstall-units" in c for c in seen)


# -- not hammering GitHub ----------------------------------------------------
#
# Found the hard way: "Could not reach GitHub" on a camera whose network was
# fine. It was 403, GitHub's hourly limit for the whole address, and every
# settings page load retried and kept it there — because a failed check was the
# one answer that was never cached.

def test_a_failed_check_is_cached_so_it_stops_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    calls = []

    def fetch():
        calls.append(1)
        return None, "Could not reach GitHub.", 0.0

    monkeypatch.setattr(updater, "_fetch_latest_release", fetch)
    updater.check(force=True)
    updater.check()
    updater.check()
    assert len(calls) == 1, "a failure retried on every call — the old loop"


def test_a_rate_limit_waits_exactly_until_it_resets(tmp_path, monkeypatch):
    """Retrying before the reset cannot succeed and only spends the next hour's
    allowance, so the cache expires when GitHub says the limit does."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    reset = time.time() + 1200
    monkeypatch.setattr(updater, "_fetch_latest_release",
                        lambda: (None, "used up", reset))
    result = updater.check(force=True)
    assert result["retry_after"] == reset
    assert updater._cached_check() is not None          # still holding
    monkeypatch.setattr(updater.time, "time", lambda: reset + 1)
    assert updater._cached_check() is None              # and free again


def test_the_error_says_what_to_do_about_it(tmp_path, monkeypatch):
    """A category is not an answer. This one sent someone hunting a network
    fault that did not exist."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)

    class Limited(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("u", 403, "rate limited", {}, None)
            self.headers = {"X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": str(int(time.time() + 600))}

    def raise_limited(*a, **kw):
        raise Limited()

    monkeypatch.setattr(updater.urllib.request, "urlopen", raise_limited)
    release, error, reset = updater._fetch_latest_release()
    assert release is None and reset > time.time()
    assert "hourly limit" in error and "frees up at" in error


def test_auto_check_off_never_touches_the_network(tmp_path, monkeypatch):
    """The switch existed in config and was wired to nothing at all."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    cfg = config.Config()
    cfg.updates.auto_check = False
    monkeypatch.setattr(config, "load", lambda: cfg)
    calls = []
    monkeypatch.setattr(updater, "_fetch_latest_release",
                        lambda: calls.append(1) or (None, "", 0.0))

    result = updater.check()
    assert not calls, "checked despite auto_check being off"
    assert result["auto_check"] is False


def test_the_button_still_works_with_auto_check_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    cfg = config.Config()
    cfg.updates.auto_check = False
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(updater, "_fetch_latest_release",
                        lambda: ({"tag_name": "v9.9.9", "body": "", "name": ""},
                                 "", 0.0))
    assert updater.check(force=True)["latest"] == "9.9.9"


# -- why a rollback happened -------------------------------------------------

def test_a_rollback_records_which_gate_failed(monkeypatch, tmp_path):
    """0.5.4 and 0.5.5 both rolled back and `/api/update/status` said only
    "Rolled back to the previous version". On a rig with no SSH that is a dead
    end: the journal is unreachable, and the rollback has already restored the
    working copy, so the broken build cannot even be inspected afterwards.

    The three health gates fail for very different reasons and the fix differs
    each time, so the status has to say which one it was.
    """
    from skylapse import config
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=300, cwd=None:
                        type("R", (), {"returncode": 0, "stderr": "",
                                       "stdout": "priorsha"})())
    monkeypatch.setattr(updater, "_changed_paths", lambda a, b: set())
    monkeypatch.setattr(updater, "_restart_services",
                        lambda: (False, "restart-services exited 127: bad interpreter"))
    monkeypatch.setattr(updater, "_wait_healthy", lambda *a, **k: True)

    result = updater.apply("v9.9.9", apply_now=True)
    assert result["ok"] is False
    assert "127" in result["reason"]
    assert "127" in updater.status()["reason"], \
        "the reason must survive into the status file the web UI reads"


def test_each_gate_gives_a_distinguishable_reason(monkeypatch, tmp_path):
    """Restart-failed, restarted-but-stale, and started-but-never-healthy are
    three different bugs. Telling them apart is the whole point."""
    from skylapse import config
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=300, cwd=None:
                        type("R", (), {"returncode": 0, "stderr": "",
                                       "stdout": "priorsha"})())
    monkeypatch.setattr(updater, "_changed_paths", lambda a, b: set())

    monkeypatch.setattr(updater, "_restart_services", lambda: (True, ""))
    monkeypatch.setattr(updater, "_restarts_took_effect", lambda *a, **k: False)
    monkeypatch.setattr(updater, "_wait_healthy", lambda *a, **k: True)
    stale = updater.apply("v9.9.9", apply_now=True)["reason"]

    monkeypatch.setattr(updater, "_restarts_took_effect", lambda *a, **k: True)
    healthy = iter([False, True])          # unhealthy after update, fine after rollback
    monkeypatch.setattr(updater, "_wait_healthy", lambda *a, **k: next(healthy))
    unhealthy = updater.apply("v9.9.9", apply_now=True)["reason"]

    assert "old code was still running" in stale
    assert "healthy frame" in unhealthy
    assert stale != unhealthy


def test_exit_127_names_the_line_ending_as_the_likely_cause(monkeypatch):
    """The helper failing to execute at all has had exactly one cause here.
    Saying so turns a half-day of bisecting into a one-line fix."""
    monkeypatch.setattr(updater, "_run", lambda cmd, timeout=300, cwd=None:
                        type("R", (), {"returncode": 127, "stdout": "",
                                       "stderr": "bad interpreter"})())
    ok, detail = updater._restart_services()
    assert ok is False
    assert "CRLF" in detail and "skylapse-admin" in detail


# -- installer / updater parity ----------------------------------------------

def test_the_updater_installs_the_same_extras_as_the_installer():
    """A camera that was updated and one that was freshly imaged must end up
    with the same packages.

    They did not. install.sh installed `.[zwo]`, the updater installed plain
    `.`, and the dew heater's libraries were in a third extra that neither
    installed. The sensor probe is raw smbus2 and works without them, so the
    settings card reported "sensor found" on a camera that could never take a
    reading -- for two releases, while the fault was assumed to be wiring.
    """
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    installer = (root / "install.sh").read_text(encoding="utf-8")
    updater_src = (root / "skylapse" / "updater.py").read_text(encoding="utf-8")

    def extras(text):
        found = set()
        for match in re.finditer(r"\[([a-z,\s]+)\]", text):
            parts = {p.strip() for p in match.group(1).split(",")}
            if parts <= {"zwo", "dewheater"} and parts:
                found |= parts
        return found

    from_installer = extras(installer)
    from_updater = extras(updater_src)
    assert from_installer, "no extras found in install.sh -- has the parser drifted?"
    assert from_installer == from_updater, (
        f"install.sh installs {sorted(from_installer)} but the updater installs "
        f"{sorted(from_updater)}; an updated camera and an imaged one would differ")


def test_every_declared_extra_is_actually_installed_somewhere():
    """An extra nobody installs is a feature that silently cannot work."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^(\w+)\s*=\s*\[", pyproject, re.M)) - {
        "dependencies", "requires", "classifiers", "keywords", "authors",
        "packages", "include", "exclude", "where", "namespaces", "scripts"}
    installer = (root / "install.sh").read_text(encoding="utf-8")
    for extra in declared:
        assert extra in installer, (
            f"pyproject declares the [{extra}] extra but install.sh never "
            f"installs it, so anything depending on it cannot work")
