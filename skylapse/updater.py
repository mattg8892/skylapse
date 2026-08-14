"""In-app updates for Skylapse itself.

Scope, deliberately narrow: this updates the Skylapse checkout and nothing
else. It never touches apt, the OS, firmware, or any other package. A camera
that reboots into a broken userland because its imaging app decided to upgrade
the system is worse than one running last month's build.

Two things shape the implementation:

1. **The update restarts the API**, so it cannot run inside the API process —
   the restart would kill the updater mid-flight, possibly between `git
   checkout` and `pip install`. Applying therefore re-execs this module as a
   detached process (`python -m skylapse.updater apply <ref>`) that outlives
   both services and reports progress through a status file.
2. **A failed update on a headless rig is expensive.** The prior ref is
   recorded before anything changes, and if the daemon is not active and
   answering within HEALTH_TIMEOUT_S of the restart, the update rolls itself
   back and restarts again. The failure mode to avoid is a camera that quietly
   stops capturing until someone walks out to it.
"""
from __future__ import annotations

import getpass
import json
import logging
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__, config

log = logging.getLogger("skylapse.updater")

REPO = "mattg8892/skylapse"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_TTL_S = 24 * 3600          # daily; the on-demand endpoint passes force
HEALTH_TIMEOUT_S = 60            # how long the new build gets to prove itself
HEALTH_POLL_S = 3
DEFER_POLL_S = 300               # how often a deferred update re-checks for day
STATUS_NAME = "update.json"
CHECK_NAME = "update_check.json"
SERVICES = ("skylapse-daemon", "skylapse-api")


def repo_root() -> Path:
    """The git checkout we are running from."""
    return Path(__file__).resolve().parents[1]


def _run(cmd: list[str], timeout: int = 300, cwd: Path | None = None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=str(cwd or repo_root()))


# -- version comparison -----------------------------------------------------

def parse_version(text: str) -> tuple[int, ...]:
    """'v0.2.1' -> (0, 2, 1). Unparseable parts sort as 0 rather than raising:
    a malformed tag upstream must not break the check."""
    cleaned = (text or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for chunk in cleaned.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """Strictly newer, padded so 0.2 and 0.2.0 compare equal."""
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# -- checking ---------------------------------------------------------------

def _cached_check() -> dict | None:
    path = config.RUN_DIR / CHECK_NAME
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - data.get("checked_at", 0) > CHECK_TTL_S:
        return None
    return data


def _fetch_latest_release() -> dict | None:
    """Unauthenticated GitHub releases API. Rate limits are generous relative
    to one call a day, and a public repo needs no token."""
    req = urllib.request.Request(
        RELEASES_URL, headers={"Accept": "application/vnd.github+json",
                               "User-Agent": f"skylapse/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.debug("Release check failed: %s", exc)
        return None


def _dev_channel_check() -> dict:
    """Dev channel follows origin/main instead of releases. For development
    units like the bench rig, where waiting for a tag defeats the point."""
    _run(["git", "fetch", "--quiet", "origin", "main"], timeout=120)
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote = _run(["git", "rev-parse", "origin/main"]).stdout.strip()
    behind = _run(["git", "rev-list", "--count", "HEAD..origin/main"]).stdout.strip()
    subject = _run(["git", "log", "-1", "--format=%s", "origin/main"]).stdout.strip()
    return {
        "channel": "dev",
        "current": __version__,
        "current_ref": head[:7],
        "latest": f"main@{remote[:7]}",
        "target_ref": "origin/main",
        "available": bool(remote) and head != remote,
        "commits_behind": int(behind or 0),
        "notes": f"Latest commit on main: {subject}" if subject else "",
        "checked_at": time.time(),
    }


def check(force: bool = False) -> dict:
    """What is available, from whichever channel this unit follows."""
    cfg = config.load()
    if cfg.updates.channel == "dev":
        result = _dev_channel_check()
    else:
        if not force:
            cached = _cached_check()
            if cached:
                return cached
        release = _fetch_latest_release()
        if release is None:
            return {"channel": "release", "current": __version__,
                    "available": False, "error": "Could not reach GitHub",
                    "checked_at": time.time()}
        tag = release.get("tag_name", "")
        result = {
            "channel": "release",
            "current": __version__,
            "latest": tag.lstrip("v"),
            "target_ref": tag,
            "available": is_newer(tag, __version__),
            "notes": release.get("body", ""),
            "title": release.get("name", ""),
            "url": release.get("html_url", ""),
            "checked_at": time.time(),
        }
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / CHECK_NAME).write_text(json.dumps(result))
    return result


# -- status -----------------------------------------------------------------

def status() -> dict:
    path = config.RUN_DIR / STATUS_NAME
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"state": "idle"}


def _set_status(**fields) -> None:
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / STATUS_NAME).write_text(json.dumps(
        {"at": time.time(), **fields}))


# -- applying ---------------------------------------------------------------

def _worker_command(target_ref: str, apply_now: bool) -> list[str]:
    """How to launch the worker so it survives restarting skylapse-api.

    start_new_session alone is not enough: a child of the API inherits its
    systemd cgroup, and stopping that unit kills everything in it — including
    the updater, mid-flight. systemd-run puts the worker in its own transient
    unit, outside that cgroup. --uid keeps it running as the service user so
    the checkout does not end up owned by root.
    """
    inner = [sys.executable, "-m", "skylapse.updater", "apply", target_ref]
    if apply_now:
        inner.append("--now")
    if shutil.which("systemd-run"):
        return ["sudo", "-n", "systemd-run", "--collect", "--quiet",
                f"--uid={getpass.getuser()}",
                f"--working-directory={repo_root()}",
                "--unit=skylapse-update", *inner]
    return inner


def start(target_ref: str, apply_now: bool = False) -> dict:
    """Spawn the detached worker that actually performs the update."""
    current = status()
    if current.get("state") in ("running", "waiting"):
        return {"ok": False, "error": "An update is already in progress"}
    _set_status(state="waiting", target=target_ref,
                message="Update queued" if not apply_now else "Starting")
    cmd = _worker_command(target_ref, apply_now)
    detached = cmd[0] != "sudo"       # only the fallback needs its own session
    subprocess.Popen(cmd, cwd=str(repo_root()), start_new_session=detached,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "target": target_ref, "deferred": not apply_now}


def _changed_paths(from_ref: str, to_ref: str) -> set[str]:
    out = _run(["git", "diff", "--name-only", f"{from_ref}..{to_ref}"]).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def service_start_id(unit: str) -> str:
    """A token that changes if and only if the unit actually restarted.

    MainPID plus the monotonic start timestamp. Without this the health check
    cannot tell a successful restart from one that never happened — and a
    daemon that is still running the *old* code from memory looks perfectly
    healthy, which is exactly how a broken build once sailed through.
    """
    out = _run(["systemctl", "show", unit, "-p", "MainPID",
                "-p", "ExecMainStartTimestampMonotonic"], timeout=30).stdout
    return " ".join(sorted(line.strip() for line in out.splitlines() if line.strip()))


def _restart_services() -> bool:
    result = _run(["sudo", "-n", "systemctl", "restart", *SERVICES], timeout=120)
    if result.returncode != 0:
        log.error("Service restart failed (%s): %s",
                  result.returncode, (result.stderr or "").strip())
    return result.returncode == 0


def _restarts_took_effect(before: dict[str, str], timeout_s: int = 30) -> bool:
    """Confirm every service really came back as a new process."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if all(service_start_id(u) != before.get(u) for u in SERVICES):
            return True
        time.sleep(2)
    stale = [u for u in SERVICES if service_start_id(u) == before.get(u)]
    log.error("Services did not restart: %s", ", ".join(stale))
    return False


def _healthy() -> bool:
    """Active under systemd AND actually answering. Either alone is not enough:
    a daemon can be 'active' while wedged before its first frame."""
    active = _run(["systemctl", "is-active", "skylapse-daemon"],
                  timeout=30).stdout.strip()
    if active != "active":
        return False
    try:
        req = urllib.request.Request("http://localhost/api/status")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except Exception:
        return False
    return bool(body.get("daemon"))


def _wait_healthy(deadline_s: int = HEALTH_TIMEOUT_S) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if _healthy():
            return True
        time.sleep(HEALTH_POLL_S)
    return False


def _build(changed: set[str]) -> None:
    """Reinstall and rebuild only what actually changed — a pip install and an
    npm build are minutes on a Pi, and most updates touch neither."""
    root = repo_root()
    if any(p == "pyproject.toml" for p in changed):
        _set_status(state="running", message="Installing Python dependencies")
        _run([str(root / "venv" / "bin" / "pip"), "install", "-e", "."],
             timeout=1800)
    if any(p.startswith("web/") for p in changed):
        _set_status(state="running", message="Building the web interface")
        _run(["npm", "--prefix", str(root / "web"), "run", "build"], timeout=1800)


def apply(target_ref: str, apply_now: bool = False) -> dict:
    """Perform the update. Runs in the detached worker, never in the API."""
    root = repo_root()

    if not apply_now:
        # Default to a daytime idle window: a restart drops frames, and the one
        # time you must not drop frames is the middle of a clear night.
        from .daemon.scheduler import period
        _set_status(state="waiting", target=target_ref,
                    message="Waiting for a daytime window to restart")
        while period(config.load()) != "day":
            time.sleep(DEFER_POLL_S)

    prior = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    _set_status(state="running", target=target_ref, prior=prior[:7],
                message="Fetching")
    _run(["git", "fetch", "--tags", "--quiet", "origin"], timeout=300)

    changed = _changed_paths("HEAD", target_ref)
    _set_status(state="running", target=target_ref, prior=prior[:7],
                message=f"Checking out {target_ref}")
    checkout = _run(["git", "checkout", "--force", target_ref], timeout=300)
    if checkout.returncode != 0:
        _set_status(state="error", target=target_ref,
                    message=f"Checkout failed: {checkout.stderr.strip()}")
        return {"ok": False}

    _build(changed)

    _set_status(state="running", target=target_ref, prior=prior[:7],
                message="Restarting services")
    before = {u: service_start_id(u) for u in SERVICES}
    restarted = _restart_services()

    # All three must hold. Dropping any one of them lets a broken build pass:
    # a failed restart leaves the old process happily serving old code, which
    # reads as healthy by every other measure.
    if restarted and _restarts_took_effect(before) and _wait_healthy():
        new_head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
        _set_status(state="done", target=target_ref, prior=prior[:7],
                    head=new_head[:7], message="Update complete")
        log.info("Updated to %s", target_ref)
        return {"ok": True}

    # Unhealthy: put it back exactly as it was, including the install and build
    # steps, then restart again. Rolling back the code but not the deps would
    # leave a mismatch that is harder to diagnose than the original failure.
    log.error("Daemon unhealthy after update; rolling back to %s", prior[:7])
    _set_status(state="rolling_back", target=target_ref, prior=prior[:7],
                message="Update unhealthy — rolling back")
    _run(["git", "checkout", "--force", prior], timeout=300)
    _build(changed)
    _restart_services()
    recovered = _wait_healthy()
    _set_status(state="rolled_back" if recovered else "error",
                target=target_ref, prior=prior[:7],
                message="Rolled back to the previous version" if recovered
                        else "Rollback restarted, but the daemon is still unhealthy")
    return {"ok": False, "rolled_back": True, "recovered": recovered}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.INFO)
    if len(argv) >= 2 and argv[0] == "apply":
        apply(argv[1], apply_now="--now" in argv)
        return 0
    if argv and argv[0] == "check":
        print(json.dumps(check(force=True), indent=2))
        return 0
    print("usage: python -m skylapse.updater [check | apply <ref> [--now]]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
