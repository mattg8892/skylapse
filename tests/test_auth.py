"""Optional single password (DESIGN.md, "Access control").

The failure that matters is not someone guessing the password. It is a camera
on a pole that will not let its owner in — so the tests that earn their keep
here are the ones about staying reachable: off by default, the SPA always
served, a corrupt hash not bricking the device, and removing the password
actually removing it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skylapse import auth, config
from skylapse.api import main as api


@pytest.fixture
def open_camera(tmp_path, monkeypatch):
    """A camera with no password — the default a fresh install ships with."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


@pytest.fixture
def locked_camera(open_camera):
    open_camera.post("/api/auth/password", json={"password": "sekrit123"})
    open_camera.cookies.clear()
    return open_camera


# -- off by default ----------------------------------------------------------

def test_a_fresh_camera_asks_for_nothing(open_camera):
    """Off by default is the whole design. A camera on a LAN nobody else
    touches should not demand a login to look at the sky."""
    assert config.load().auth.password_hash == ""
    assert open_camera.get("/api/status").status_code == 200
    assert open_camera.get("/api/config").status_code == 200


def test_login_on_an_open_camera_is_not_an_error(open_camera):
    """Someone who bookmarked the login page should not meet a failure."""
    assert open_camera.post("/api/auth/login", json={"password": "x"}).status_code == 200


# -- the password ------------------------------------------------------------

def test_the_password_is_never_stored_in_the_clear(locked_camera):
    saved = config.load().auth.password_hash
    assert saved and "sekrit123" not in saved
    assert saved.startswith("$2"), "not a bcrypt hash"


def test_the_hash_round_trips(locked_camera):
    hashed = config.load().auth.password_hash
    assert auth.verify_password("sekrit123", hashed)
    assert not auth.verify_password("sekrit124", hashed)


def test_a_long_password_is_refused_rather_than_silently_truncated(open_camera):
    """bcrypt ignores everything past 72 bytes. Accepting a 100-character
    password and quietly using the first 72 would make two different passwords
    equivalent without telling anyone."""
    r = open_camera.post("/api/auth/password", json={"password": "a" * 100})
    assert r.status_code == 400


def test_a_corrupt_hash_does_not_brick_the_camera(open_camera):
    """A truncated config write, an editor, a bad merge. Whatever the cause,
    the answer is "wrong password", not an exception that 500s every request
    including the one that would fix it."""
    cfg = config.load()
    cfg.auth.password_hash = "not-a-bcrypt-hash"
    config.save(cfg)
    assert auth.verify_password("anything", "not-a-bcrypt-hash") is False
    assert open_camera.post("/api/auth/login",
                            json={"password": "anything"}).status_code == 401


# -- what the password protects ---------------------------------------------

def test_the_api_is_locked_without_a_session(locked_camera):
    assert locked_camera.get("/api/status").status_code == 401
    assert locked_camera.get("/api/config").status_code == 401
    assert locked_camera.post("/api/keeper").status_code == 401


def test_the_spa_is_always_served(locked_camera):
    """The login screen is part of the SPA. A camera that refused to serve its
    own login page would be unrecoverable from a phone."""
    assert locked_camera.get("/api/auth/status").status_code == 200


def test_logging_in_unlocks_it(locked_camera):
    assert locked_camera.post("/api/auth/login",
                              json={"password": "sekrit123"}).status_code == 200
    assert locked_camera.get("/api/status").status_code == 200


def test_the_wrong_password_does_not(locked_camera):
    assert locked_camera.post("/api/auth/login",
                              json={"password": "nope"}).status_code == 401
    assert locked_camera.get("/api/status").status_code == 401


# -- public live view --------------------------------------------------------

def test_public_live_view_shows_the_frame_and_nothing_else(locked_camera):
    locked_camera.post("/api/auth/login", json={"password": "sekrit123"})
    locked_camera.post("/api/auth/password",
                       json={"current": "sekrit123", "password": "sekrit123",
                             "public_live_view": True})
    locked_camera.cookies.clear()

    assert locked_camera.get("/api/status").status_code == 200
    assert locked_camera.get("/api/config").status_code == 401, "settings leaked"
    assert locked_camera.post("/api/keeper").status_code == 401, "controls leaked"


def test_public_live_view_never_permits_a_write(locked_camera):
    """Read-only by construction: a GET on a named path. Not "GET is safe"."""
    locked_camera.post("/api/auth/login", json={"password": "sekrit123"})
    locked_camera.post("/api/auth/password",
                       json={"current": "sekrit123", "password": "sekrit123",
                             "public_live_view": True})
    locked_camera.cookies.clear()
    assert locked_camera.put("/api/config", json={"jpeg_quality": 50}).status_code == 401


# -- changing and removing ---------------------------------------------------

def test_changing_the_password_needs_the_current_one(locked_camera):
    locked_camera.post("/api/auth/login", json={"password": "sekrit123"})
    assert locked_camera.post("/api/auth/password",
                              json={"password": "newpass1"}).status_code == 403
    assert auth.verify_password("sekrit123", config.load().auth.password_hash)


def test_removing_the_password_reopens_the_camera(locked_camera):
    locked_camera.post("/api/auth/login", json={"password": "sekrit123"})
    r = locked_camera.post("/api/auth/password",
                           json={"current": "sekrit123", "password": ""})
    assert r.status_code == 200
    locked_camera.cookies.clear()
    assert config.load().auth.password_hash == ""
    assert locked_camera.get("/api/status").status_code == 200


def test_removing_the_password_also_drops_public_live_view(locked_camera):
    """It is a sub-toggle of a protection that no longer exists; leaving it set
    would quietly re-enable a restriction if a password were set again later."""
    locked_camera.post("/api/auth/login", json={"password": "sekrit123"})
    locked_camera.post("/api/auth/password",
                       json={"current": "sekrit123", "password": "sekrit123",
                             "public_live_view": True})
    locked_camera.post("/api/auth/password",
                       json={"current": "sekrit123", "password": ""})
    assert config.load().auth.public_live_view is False


def test_setting_a_new_password_invalidates_old_sessions(locked_camera):
    """The reason to change a password is usually that someone had it."""
    locked_camera.post("/api/auth/login", json={"password": "sekrit123"})
    stale = dict(locked_camera.cookies)
    locked_camera.post("/api/auth/password",
                       json={"current": "sekrit123", "password": "different1"})

    locked_camera.cookies.clear()
    for name, value in stale.items():
        locked_camera.cookies.set(name, value)
    assert locked_camera.get("/api/status").status_code == 401


def test_the_setter_is_not_logged_out_of_their_own_camera(open_camera):
    r = open_camera.post("/api/auth/password", json={"password": "sekrit123"})
    assert r.status_code == 200
    assert open_camera.get("/api/status").status_code == 200


# -- the session token -------------------------------------------------------

def test_a_token_survives_a_restart(locked_camera):
    """Stateless by design: someone who logged in at the shed door in October
    must not be logged out because the api was updated in November."""
    secret = config.load().auth.session_secret
    token = auth.issue_token(secret)
    assert auth.token_valid(token, secret)


def test_a_token_expires(locked_camera):
    secret = config.load().auth.session_secret
    token = auth.issue_token(secret, now=0)
    assert auth.token_valid(token, secret, now=1)
    assert not auth.token_valid(token, secret, now=auth.SESSION_SECONDS + 2)


@pytest.mark.parametrize("forged", [
    "", "garbage", "9999999999.", ".sig", "9999999999.wrongsignature",
    "notanumber.sig",
])
def test_a_forged_token_is_rejected(forged):
    secret = auth.new_secret()
    assert not auth.token_valid(forged, secret)


def test_a_token_signed_with_another_secret_is_rejected():
    """Two cameras on one network, or the same camera after a password change."""
    token = auth.issue_token(auth.new_secret())
    assert not auth.token_valid(token, auth.new_secret())


def test_an_expiry_cannot_be_edited_without_the_signature():
    secret = auth.new_secret()
    token = auth.issue_token(secret, now=0)
    _, _, signature = token.partition(".")
    assert not auth.token_valid(f"99999999999.{signature}", secret, now=1)
