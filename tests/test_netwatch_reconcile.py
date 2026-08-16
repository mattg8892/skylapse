"""Netwatch has to keep checking that the radio still matches its own picture.

Measured on the rig: NetworkManager restarted, nmcli was unavailable for about
two seconds, the hotspot failed to come up with "No Wi-Fi interface", and
nothing ever tried again. Netwatch sat in HOTSPOT reporting an access point
that did not exist.

That is the whole failure this subsystem exists to prevent. With no known
network in range — the exact case where the hotspot is the only way in — the
camera is unreachable until somebody power-cycles it, and it will be on a pole
in the dark when it happens.
"""
from __future__ import annotations

from skylapse.netwatch.statemachine import Mode, State

from .test_netwatch_glue import FakeClock, FakeRadio, _service


def _in_hotspot(monkeypatch, joinable=False):
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.joinable = clock, joinable
    service = _service(monkeypatch, radio, clock)
    service._now()
    service._execute(service.sm.on_boot())
    assert service.sm.ctx.state is State.HOTSPOT
    assert radio.mode == "AP"
    return service, radio, clock


def test_a_hotspot_that_failed_to_come_up_is_raised_again(monkeypatch):
    service, radio, _ = _in_hotspot(monkeypatch)

    radio.mode = "managed"              # nmcli was down when we tried to raise it
    radio.activations.clear()
    service._now()
    service._poll_events()

    assert radio.mode == "AP", \
        "netwatch reported an access point it had never managed to start"
    assert "Skylapse-Setup" in radio.activations


def test_wifi_appearing_underneath_the_fallback_is_accepted(monkeypatch):
    """NetworkManager restarting and autoconnecting is the common way in.

    Reported from the rig: after an NM restart the radio was a client on the
    house network while netwatch still said 'hotspot' — so the dashboard badge,
    the connection screen, and the api all described a camera that did not
    exist.
    """
    service, radio, _ = _in_hotspot(monkeypatch)

    radio.mode, radio.joined = "managed", "netplan-wlan0-yourmomshouse"
    service._now()
    service._poll_events()

    assert service.sm.ctx.state is State.CONNECTED


def test_a_deliberate_access_point_wins_over_wifi_that_appeared_by_itself(monkeypatch):
    """Standalone is somebody's explicit choice. An autoconnect that happened
    while they were mid-setup must not silently overrule it and take the
    network out from under their phone."""
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    service = _service(monkeypatch, radio, clock)
    service._now()
    service._execute(service.sm.on_user_pick_standalone())
    assert service.sm.ctx.state is State.STANDALONE

    radio.mode, radio.joined = "managed", "netplan-wlan0-yourmomshouse"
    service._now()
    service._poll_events()

    assert service.sm.ctx.state is State.STANDALONE
    assert radio.mode == "AP", "the user's choice was overruled by an autoconnect"


def test_reconciling_does_not_disturb_a_healthy_hotspot(monkeypatch):
    """The repair must be a no-op when nothing is wrong — re-raising a working
    access point would drop every phone attached to it, once per poll."""
    service, radio, _ = _in_hotspot(monkeypatch)
    radio.activations.clear()

    for _ in range(10):
        service._now()
        service._poll_events()

    assert radio.activations == [], "re-raised an access point that was already up"
    assert radio.mode == "AP"
