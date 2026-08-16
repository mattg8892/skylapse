"""Manual access-point mode.

The automatic fallback in statemachine.py exists for when Wi-Fi fails. This is
the other half: someone standing at the camera with a phone, who wants the
access point *now* and does not want to wait out a connect timeout to get it.

The two properties that matter are that it sticks (it must not expire on its
own, or the network vanishes from under whoever is using it) and that it is
persisted (a power cut in the field must not quietly put the camera back on
Wi-Fi while someone is still working on it).
"""
from __future__ import annotations

import pytest
import yaml

from skylapse import config
from skylapse.netwatch.statemachine import Mode, State

from .test_netwatch_glue import FakeClock, FakeRadio, _service


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("network:\n  mode: auto\n")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    return path


def _write_network(path, **fields):
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("network", {}).update(fields)
    path.write_text(yaml.safe_dump(data))


def _running_service(monkeypatch, cfg_path, joinable=True):
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.joinable = clock, joinable
    service = _service(monkeypatch, radio, clock)
    service._now()
    service._execute(service.sm.on_boot())
    return service, radio, clock


def test_switching_to_access_point_mode_takes_effect_without_a_restart(
        monkeypatch, cfg_path):
    """Settings writes the config; netwatch picks it up on its next poll.

    Requiring a service restart to change network mode would defeat the
    control: restarting the network service is the thing you cannot safely ask
    someone to do from a phone that is about to lose its connection.
    """
    service, radio, _ = _running_service(monkeypatch, cfg_path)
    assert service.sm.ctx.state is State.CONNECTED

    _write_network(cfg_path, mode="standalone", hotspot_until=0.0)
    service._poll_config()

    assert service.sm.ctx.mode is Mode.STANDALONE
    assert service.sm.ctx.state is State.STANDALONE
    assert radio.mode == "AP"


def test_access_point_mode_stays_up_until_it_is_turned_off(monkeypatch, cfg_path):
    """With no deadline it must never lapse on its own — not after an hour, and
    not after the automatic fallback's dwell window either."""
    service, radio, clock = _running_service(monkeypatch, cfg_path)
    _write_network(cfg_path, mode="standalone", hotspot_until=0.0)
    service._poll_config()

    for _ in range(200):
        clock.advance(60)
        service._now()
        service._poll_config()
        service._poll_events()

    assert service.sm.ctx.state is State.STANDALONE, \
        "the access point lapsed on its own after 200 minutes"
    assert radio.mode == "AP"
    assert yaml.safe_load(cfg_path.read_text())["network"]["mode"] == "standalone"


def test_turning_it_off_rejoins_wifi(monkeypatch, cfg_path):
    service, radio, _ = _running_service(monkeypatch, cfg_path)
    _write_network(cfg_path, mode="standalone")
    service._poll_config()
    assert radio.mode == "AP"

    radio.activations.clear()
    _write_network(cfg_path, mode="auto")
    service._poll_config()

    assert service.sm.ctx.state is State.CONNECTED
    assert radio.joined == "netplan-wlan0-yourmomshouse"


def test_a_timed_session_expires_and_writes_the_change_back(monkeypatch, cfg_path):
    """The deadline has to be cleared from the file, not just from memory.

    Leaving a lapsed deadline on disk would put the camera straight back into
    access-point mode at the next boot, which is the opposite of what a *timed*
    session was chosen for.
    """
    service, radio, clock = _running_service(monkeypatch, cfg_path)
    deadline = clock.time() + 7200
    _write_network(cfg_path, mode="standalone", hotspot_until=deadline)
    service._poll_config()
    assert service.sm.ctx.state is State.STANDALONE

    clock.advance(deadline - 1 - clock.time())        # one second short
    service._now()
    service._poll_config()
    assert service.sm.ctx.state is State.STANDALONE, "expired a second early"

    clock.advance(2)
    service._now()
    service._poll_config()

    assert service.sm.ctx.mode is Mode.AUTO
    assert service.sm.ctx.state is State.CONNECTED
    saved = yaml.safe_load(cfg_path.read_text())["network"]
    assert saved["mode"] == "auto" and not saved["hotspot_until"]


def test_the_deadline_is_reported_for_the_badge(monkeypatch, cfg_path):
    service, _, clock = _running_service(monkeypatch, cfg_path)
    deadline = clock.time() + 7200
    _write_network(cfg_path, mode="standalone", hotspot_until=deadline)
    service._poll_config()
    service._write_status()

    import json
    status = json.loads((config.RUN_DIR / "netwatch.json").read_text())
    assert status["hotspot_until"] == deadline
    assert status["state"] == "standalone"


# -- the API surface ---------------------------------------------------------

@pytest.fixture
def client(cfg_path):
    from fastapi.testclient import TestClient

    from skylapse.api import main as api
    return TestClient(api.app)


def test_api_sticky_mode_persists_with_no_deadline(client, cfg_path):
    r = client.post("/api/network/mode", json={"mode": "hotspot"})
    assert r.status_code == 200
    saved = yaml.safe_load(cfg_path.read_text())["network"]
    assert saved["mode"] == "standalone"
    assert not saved["hotspot_until"], "sticky mode must not carry a deadline"


def test_api_timed_mode_sets_a_deadline(client, cfg_path):
    import time
    r = client.post("/api/network/mode", json={"mode": "hotspot_timed",
                                               "minutes": 120})
    saved = yaml.safe_load(cfg_path.read_text())["network"]
    assert saved["mode"] == "standalone"
    assert 7100 < saved["hotspot_until"] - time.time() <= 7200
    assert r.json()["hotspot_until"] == saved["hotspot_until"]


def test_api_auto_clears_both(client, cfg_path):
    client.post("/api/network/mode", json={"mode": "hotspot_timed", "minutes": 5})
    client.post("/api/network/mode", json={"mode": "auto"})
    saved = yaml.safe_load(cfg_path.read_text())["network"]
    assert saved["mode"] == "auto" and not saved["hotspot_until"]


def test_api_rejects_an_unknown_mode(client, cfg_path):
    assert client.post("/api/network/mode", json={"mode": "wifi_only"}).status_code == 400
    assert yaml.safe_load(cfg_path.read_text())["network"]["mode"] == "auto"


def test_api_reports_remaining_time(client, cfg_path, monkeypatch):
    monkeypatch.setattr("skylapse.api.main._wifi_ssid", lambda: "")
    client.post("/api/network/mode", json={"mode": "hotspot_timed", "minutes": 90})
    body = client.get("/api/network").json()
    assert body["mode"] == "hotspot_timed"
    assert 5300 < body["remaining_s"] <= 5400


def test_api_sticky_mode_reports_no_countdown(client, cfg_path, monkeypatch):
    """A sticky session has nothing to count down, and showing "0 left" next to
    a network that is not going anywhere would read as about to expire."""
    monkeypatch.setattr("skylapse.api.main._wifi_ssid", lambda: "")
    client.post("/api/network/mode", json={"mode": "hotspot"})
    body = client.get("/api/network").json()
    assert body["mode"] == "hotspot"
    assert body["remaining_s"] is None
