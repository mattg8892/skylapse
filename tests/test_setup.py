"""The setup wizard: gating, the draft, and the atomic commit.

DESIGN.md guard 4 is the contract being defended: the flag is persisted with
the config, before the final screen renders, and re-entry is idempotent —
walking the wizard again on a configured camera shows current values and never
blanks them.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skylapse import config, setup
from skylapse.api import main as api


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A camera that has never been set up."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


@pytest.fixture
def configured(fresh):
    cfg = config.load()
    cfg.setup_complete = True
    cfg.location.latitude, cfg.location.longitude = 42.73, -87.78
    cfg.location.timezone = "America/Chicago"
    cfg.camera("picam-imx477").label = "Pi Camera"
    cfg.active_camera = "picam-imx477"
    config.save(cfg)
    return fresh


# -- gating ------------------------------------------------------------------

def test_a_fresh_camera_reports_setup_incomplete(fresh):
    assert fresh.get("/api/status").json()["setup_complete"] is False
    assert fresh.get("/api/setup/draft").json()["complete"] is False


def test_a_configured_camera_reports_complete(configured):
    assert configured.get("/api/status").json()["setup_complete"] is True


# -- the draft ---------------------------------------------------------------

def test_the_draft_seeds_from_current_config(configured):
    """Re-entry shows what the camera is set to, never a blank form."""
    draft = configured.get("/api/setup/draft").json()["draft"]
    assert draft["location"]["latitude"] == 42.73
    assert draft["network"]["mode"] == "auto"


def test_the_wizard_does_not_ask_about_the_camera(fresh):
    """Camera setup is Settings → Cameras. It does everything the step did, it
    is where you go anyway when a camera is replaced, and declaring a sensor the
    Pi cannot auto-detect reboots the Pi — which is not a thing to do to
    somebody halfway through first-run setup."""
    steps = fresh.get("/api/setup/draft").json()["steps"]
    assert "camera" not in steps


def test_the_capture_answers_reach_the_detected_camera(fresh, tmp_path):
    """Regression, and it predates moving the step out: the old camera screen
    only wrote camera_id when there was more than one camera to choose between,
    so on a fresh single-camera card it stayed empty and this whole screen was
    silently discarded — with the summary then reporting defaults nobody chose.
    """
    cfg = config.load()
    cfg.camera("picam-imx477").label = "Pi Camera"      # what the daemon does
    config.save(cfg)

    fresh.patch("/api/setup/draft",
                json={"capture": {"schedule": "night_only",
                                  "raw_mode": "every_frame"}})
    summary = fresh.post("/api/setup/complete", json={}).json()["summary"]
    assert summary["schedule"] == "night_only"
    assert summary["raw_mode"] == "every_frame"

    saved = config.load()
    assert saved.active_camera == "picam-imx477"
    assert saved.cameras["picam-imx477"].capture_schedule == "night_only"


def test_a_screen_only_writes_its_own_section(fresh):
    """Going Back and forward again must not blank the screens after it."""
    fresh.patch("/api/setup/draft", json={"location": {"latitude": 51.5}})
    fresh.patch("/api/setup/draft", json={"capture": {"schedule": "night_only"}})
    draft = fresh.get("/api/setup/draft").json()["draft"]
    assert draft["location"]["latitude"] == 51.5
    assert draft["capture"]["schedule"] == "night_only"


def test_the_draft_survives_a_reconnect(fresh):
    """It lives on the camera, not in the phone. The future entry path is a
    hotspot the camera is about to reconfigure — losing four screens of answers
    to a locked phone would be a poor first impression."""
    fresh.patch("/api/setup/draft", json={"location": {"latitude": 51.5}})
    assert setup.draft_path().exists()
    assert setup.load_draft()["location"]["latitude"] == 51.5


def test_a_corrupt_draft_does_not_block_setup(fresh):
    setup.draft_path().parent.mkdir(parents=True, exist_ok=True)
    setup.draft_path().write_text("{not json")
    assert fresh.get("/api/setup/draft").status_code == 200


def test_patching_is_idempotent(fresh):
    for _ in range(3):
        fresh.patch("/api/setup/draft", json={"capture": {"schedule": "night_only"}})
    assert setup.load_draft()["capture"]["schedule"] == "night_only"


# -- committing --------------------------------------------------------------

def test_completing_writes_config_and_flag_together(fresh):
    """Guard 4: one atomic save. A power cut between them would leave a camera
    that runs the wizard again at every boot."""
    fresh.patch("/api/setup/draft", json={
        "location": {"latitude": 42.73, "longitude": -87.78,
                     "timezone": "America/Chicago"},
        "camera": {"camera_id": "picam-imx477"},
        "capture": {"schedule": "night_only", "raw_mode": "off"}})
    assert fresh.post("/api/setup/complete").status_code == 200

    cfg = config.load()
    assert cfg.setup_complete is True
    assert cfg.location.latitude == 42.73
    assert cfg.cameras["picam-imx477"].capture_schedule == "night_only"


def test_completing_clears_the_draft(fresh):
    fresh.patch("/api/setup/draft", json={"location": {"latitude": 42.73}})
    fresh.post("/api/setup/complete")
    assert not setup.draft_path().exists()


def test_completing_without_touching_anything_yields_a_working_config(fresh):
    """Continue-through must produce something sane, not a half-set camera."""
    assert fresh.post("/api/setup/complete").status_code == 200
    cfg = config.load()
    assert cfg.setup_complete is True
    assert cfg.jpeg_quality > 0 and cfg.cleanup_free_gb > 0


def test_re_running_setup_never_blanks_what_is_there(configured):
    """The idempotency requirement end to end: walk it again, change one
    screen, and everything else survives."""
    configured.post("/api/setup/reset")
    configured.patch("/api/setup/draft", json={"capture": {"schedule": "always"}})
    configured.post("/api/setup/complete")

    cfg = config.load()
    assert cfg.location.latitude == 42.73, "location was blanked by a re-run"
    assert cfg.cameras["picam-imx477"].label == "Pi Camera"
    assert cfg.setup_complete is True


def test_reset_reopens_the_wizard(configured):
    assert configured.post("/api/setup/reset").status_code == 200
    assert config.load().setup_complete is False


# -- the password, applied only at the end -----------------------------------

def test_a_password_from_the_wizard_takes_effect(fresh):
    from skylapse import auth

    fresh.patch("/api/setup/draft",
                json={"security": {"password": "shedkey1",
                                   "public_live_view": True}})
    fresh.post("/api/setup/complete")

    cfg = config.load()
    assert auth.verify_password("shedkey1", cfg.auth.password_hash)
    assert cfg.auth.public_live_view is True


def test_an_abandoned_wizard_never_locks_the_camera(fresh):
    """The password is applied at the commit, not on the security screen. A
    wizard abandoned halfway must not leave a camera locked behind a password
    nobody finished choosing."""
    fresh.patch("/api/setup/draft", json={"security": {"password": "shedkey1"}})
    assert config.load().auth.password_hash == ""


def test_skipping_the_password_leaves_the_camera_open(fresh):
    """Skip is first-class (DESIGN.md). Off is the default and must stay it."""
    fresh.post("/api/setup/complete")
    assert config.load().auth.password_hash == ""


def test_the_wizard_does_not_log_you_out_of_what_you_just_set_up(fresh):
    fresh.patch("/api/setup/draft", json={"security": {"password": "shedkey1"}})
    fresh.post("/api/setup/complete")
    assert fresh.get("/api/status").status_code == 200


def test_a_too_short_password_is_refused_at_the_commit(fresh):
    fresh.patch("/api/setup/draft", json={"security": {"password": "ab"}})
    assert fresh.post("/api/setup/complete").status_code == 400
    assert config.load().setup_complete is False, "committed despite refusing"


# -- location cascade --------------------------------------------------------

def test_the_location_check_proves_the_setting_matters(configured):
    body = configured.get(
        "/api/setup/location/check?latitude=42.73&longitude=-87.78").json()
    assert body["sunset"] is not None
    assert body["kp_threshold"] >= 1
    assert body["null_island"] is False


def test_null_island_is_flagged(configured):
    """0,0 is what an unset config looks like, so it is the value most likely
    to be wrong by accident rather than because someone is on a boat."""
    body = configured.get(
        "/api/setup/location/check?latitude=0&longitude=0").json()
    assert body["null_island"] is True


def test_impossible_coordinates_are_flagged(configured):
    body = configured.get(
        "/api/setup/location/check?latitude=95&longitude=200").json()
    assert body["out_of_range"] is True


def test_the_ip_estimate_fails_cleanly_with_no_internet(configured, monkeypatch):
    """Offline is the normal case for this device. The cascade has to fall
    through to manual entry, not hang or 500."""
    import urllib.request

    def no_internet(*a, **kw):
        raise OSError("network unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", no_internet)
    assert configured.get("/api/setup/location/estimate").status_code == 503


# -- the test shot must never be able to take the capture daemon down --------

def test_a_failing_test_shot_does_not_kill_the_daemon(tmp_path, monkeypatch):
    """It did. A wrong call signature raised TypeError out of the capture
    loop, systemd restarted the daemon, and the wizard's convenience feature
    had stopped the one job the device exists to do.

    Nothing reachable from a button may escape this handler.
    """
    from skylapse.daemon.main import CaptureDaemon

    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    daemon = CaptureDaemon.__new__(CaptureDaemon)
    daemon.cfg = config.Config()
    daemon.camera_id = "picam-imx477"

    class Exploding:
        def set_controls(self, *a):
            raise TypeError("capture() takes 1 positional argument")

    daemon.driver = Exploding()
    daemon._focus_controls = lambda: (1000, 1)
    (tmp_path / "focus_cmd.json").write_text("{}")

    daemon._poll_setup_shot()             # must not raise
    assert not (tmp_path / "focus_cmd.json").exists(), "command left to loop forever"


def test_the_request_is_consumed_even_when_it_fails(tmp_path, monkeypatch):
    """Otherwise a failing shot is retried on every pass of the capture loop,
    for as long as the camera is broken."""
    from skylapse.daemon.main import CaptureDaemon

    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    daemon = CaptureDaemon.__new__(CaptureDaemon)
    daemon.cfg = config.Config()
    daemon.camera_id = "picam-imx477"
    daemon.driver = type("D", (), {"set_controls": lambda *a: None,
                                   "capture": lambda *a: (_ for _ in ()).throw(
                                       RuntimeError("no camera"))})()
    daemon._focus_controls = lambda: (1000, 1)
    (tmp_path / "focus_cmd.json").write_text("{}")

    daemon._poll_setup_shot()
    assert not (tmp_path / "focus_cmd.json").exists()


# -- Wi-Fi is optional -------------------------------------------------------

def test_a_camera_can_be_set_up_to_never_use_wifi(fresh):
    """Plenty of cameras never join a network: a shed with no coverage, a
    dark-sky site, a rig someone simply walks up to. Making that an explicit
    choice in the wizard is the difference between unsupported and supported."""
    fresh.patch("/api/setup/draft", json={"network": {"mode": "standalone"}})
    fresh.post("/api/setup/complete")
    assert config.load().network.mode == "standalone"


def test_the_wifi_choice_defaults_to_joining_a_network(fresh):
    fresh.post("/api/setup/complete")
    assert config.load().network.mode == "auto"


def test_the_network_choice_seeds_from_what_the_camera_is_doing(fresh):
    cfg = config.load()
    cfg.network.mode = "standalone"
    config.save(cfg)
    draft = fresh.get("/api/setup/draft").json()["draft"]
    assert draft["network"]["mode"] == "standalone"


def test_a_nonsense_network_mode_is_ignored(fresh):
    """Hand-edited drafts and future modes must not write a value netwatch
    would refuse to parse."""
    fresh.patch("/api/setup/draft", json={"network": {"mode": "wifi_only_maybe"}})
    fresh.post("/api/setup/complete")
    assert config.load().network.mode == "auto"


def test_the_summary_says_how_to_reach_an_access_point_camera(fresh):
    """The one screen where someone is about to lose their connection on
    purpose has to say what to join next."""
    fresh.patch("/api/setup/draft", json={"network": {"mode": "standalone"}})
    summary = fresh.post("/api/setup/complete").json()["summary"]
    assert summary["network_mode"] == "standalone"
    assert summary["hotspot_ssid"]
