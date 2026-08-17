"""Netwatch's NetworkManager glue.

Every string matched here was captured from a real NetworkManager on this rig,
because the whole class of bug this file guards against is code that reads
plausible but wrong things out of nmcli. Guard 3 in particular is load-bearing:
blacklisting on the wrong signal locks a camera out of its own network.
"""
from __future__ import annotations

import pytest

from skylapse.netwatch.service import classify_join_failure


# Captured verbatim from `nmcli con up` against a profile with a wrong PSK.
WRONG_PASSWORD = (
    "Passwords or encryption keys are required to access the wireless network "
    "'yourmomshouse'.\n"
    "Error: Connection activation failed: Secrets were required, but not provided\n"
)

MISSING_NETWORK = "Error: No network with SSID 'ThisNetworkDoesNotExist99' found.\n"

# What NM says when a saved profile blocks a re-join with a new password.
PROFILE_CONFLICT = "Error: 802-11-wireless-security.key-mgmt: property is missing.\n"


def test_wrong_password_is_an_auth_failure():
    """NM never says "bad password" — it asks for secrets again, because the
    supplicant disconnects and it concludes the stored key must be wrong."""
    assert classify_join_failure(WRONG_PASSWORD) == "auth"


def test_missing_network_is_not_an_auth_failure():
    """Out of range must not accumulate strikes: guard 3 would eventually
    blacklist the network the camera lives on."""
    assert classify_join_failure(MISSING_NETWORK) == "not_found"


def test_profile_conflict_is_not_an_auth_failure():
    assert classify_join_failure(PROFILE_CONFLICT) == "other"


def test_empty_output_is_not_an_auth_failure():
    assert classify_join_failure("") == "other"
    assert classify_join_failure(None) == "other"


def test_classification_is_case_insensitive():
    assert classify_join_failure("SECRETS WERE REQUIRED, BUT NOT PROVIDED") == "auth"


@pytest.mark.parametrize("output,expected", [
    ("device (wlan0): state change: need-auth -> failed (reason 'no-secrets')", "auth"),
    ("Error: Connection activation failed: (7) Secrets were required", "auth"),
    ("Error: Timeout 90 sec expired.", "other"),
])
def test_real_world_shapes(output, expected):
    assert classify_join_failure(output) == expected


# -- guard 3: strikes and the blacklist -------------------------------------

def test_auth_failures_accumulate_to_a_blacklist():
    from skylapse.netwatch.statemachine import AUTH_FAIL_LIMIT, NetStateMachine

    sm = NetStateMachine()
    for strike in range(AUTH_FAIL_LIMIT):
        assert not sm.ctx.network_blacklisted("yourmomshouse"), \
            f"blacklisted after only {strike} strikes"
        sm.on_wifi_attempt_failed(ssid="yourmomshouse", auth_failure=True)
    assert sm.ctx.network_blacklisted("yourmomshouse")


def test_non_auth_failures_never_blacklist():
    """A network that was simply out of range for a while must stay usable."""
    from skylapse.netwatch.statemachine import NetStateMachine

    sm = NetStateMachine()
    for _ in range(20):
        sm.on_wifi_attempt_failed(ssid="yourmomshouse", auth_failure=False)
    assert not sm.ctx.network_blacklisted("yourmomshouse")


def test_a_blacklisted_network_is_not_offered_by_the_rescan():
    from skylapse.netwatch.statemachine import (AUTH_FAIL_LIMIT, Action,
                                                NetStateMachine, State)

    sm = NetStateMachine()
    sm.ctx.state = State.HOTSPOT
    sm.ctx.hotspot_started_at = 0          # dwell satisfied
    sm.ctx.now = 10_000
    for _ in range(AUTH_FAIL_LIMIT):
        sm.ctx.auth_failures["yourmomshouse"] = \
            sm.ctx.auth_failures.get("yourmomshouse", 0) + 1
    sm.ctx.state = State.HOTSPOT
    assert sm.on_background_rescan_found_network("yourmomshouse") is Action.NONE


def test_user_try_again_clears_the_blacklist():
    """They may have just fixed the password."""
    from skylapse.netwatch.statemachine import NetStateMachine, State

    sm = NetStateMachine()
    sm.ctx.state = State.HOTSPOT
    sm.ctx.hotspot_started_at = 0
    sm.ctx.now = 10_000
    sm.ctx.auth_failures["yourmomshouse"] = 99
    sm.on_user_try_again()
    assert not sm.ctx.network_blacklisted("yourmomshouse")


# -- faults the first hardware run exposed -----------------------------------
#
# Invisible to every test written before the code met a real radio.

class FakeRadio:
    """Enough of nmcli/iw to drive the service through a fallback and back.

    Deliberately models the two behaviours that broke it: a profile only joins
    when `con up` is called on it by name (NetworkManager will not autoconnect
    a device it has flagged as user-disconnected), and time only advances when
    something blocks.
    """

    def __init__(self, clock):
        self.clock = clock
        self.mode = "managed"        # managed | AP
        self.joined = ""
        self.joinable = True         # whether the house network will accept us
        self.fail_delay = 90         # a doomed join burns the whole grace window
        self.stations = 0            # phones attached, while we are an AP
        self.country = "US"          # regulatory domain; "00" = none set
        self.activations = []        # every `con up` this test provoked

    def nmcli(self, *args, **kw):
        import subprocess
        if args[:2] == ("con", "up"):
            self.activations.append(args[2])
            if args[2] == "Skylapse-Setup":
                self.clock.advance(2)
                self.mode, self.joined = "AP", ""
            elif self.joinable:
                self.clock.advance(2)
                self.mode, self.joined = "managed", args[2]
            else:
                self.clock.advance(self.fail_delay)
                return subprocess.CompletedProcess(args, 4, "",
                                                   "Error: Connection activation failed.\n")
        elif args[:2] == ("con", "down"):
            self.mode, self.joined = "managed", ""
        elif args[:1] == ("dev",) and "wifi" in args and "list" in args:
            return subprocess.CompletedProcess(args, 0, "yourmomshouse\n", "")
        elif "802-11-wireless.ssid" in args:
            return subprocess.CompletedProcess(args, 0, "yourmomshouse\n", "")
        elif args[:2] == ("-t", "-f") and args[2] == "NAME,TYPE":
            return subprocess.CompletedProcess(
                args, 0, "netplan-wlan0-yourmomshouse:802-11-wireless\n", "")
        elif args[:2] == ("-t", "-f") and args[2] == "DEVICE,TYPE":
            return subprocess.CompletedProcess(args, 0, "wlan0:wifi\n", "")
        elif args[:2] == ("-t", "-f") and args[2] == "DEVICE,CONNECTION":
            name = self.joined or ("Skylapse-Setup" if self.mode == "AP" else "")
            return subprocess.CompletedProcess(args, 0, f"wlan0:{name}\n", "")
        elif args[:2] == ("-t", "-f") and args[2] == "DEVICE,TYPE,STATE":
            state = "connected" if self.joined or self.mode == "AP" else "disconnected"
            return subprocess.CompletedProcess(
                args, 0, f"wlan0:wifi:{state}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def iw(self, *args, **kw):
        if "reg" in args:
            # "00" is the world domain a Pi sits in until a country is set, and
            # every channel in it is no-IR: no access point can start there.
            return f"country {self.country}: DFS-FCC\n"
        if "info" in args:
            return f"Interface wlan0\n\ttype {self.mode}\n"
        # `station dump` only ever lists anything on an access point; on a
        # client interface it lists the AP we joined, which is why the real
        # probe gates on the radio's mode.
        return "".join(f"Station aa:bb:cc:dd:ee:{i:02x}\n"
                       for i in range(self.stations if self.mode == "AP" else 0))


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds

    def sleep(self, seconds):
        self.t += seconds


def _service(monkeypatch, radio, clock):
    from skylapse.netwatch import service as svc

    monkeypatch.setattr(svc.time, "time", clock.time)
    monkeypatch.setattr(svc.time, "sleep", clock.sleep)
    monkeypatch.setattr(svc.subprocess, "run",
                        lambda cmd, **kw: radio.nmcli(*cmd[1:], **kw)
                        if cmd[0] == "nmcli" else radio.iw(*cmd[1:]))
    monkeypatch.setattr(svc, "_iw", lambda *a, **kw: radio.iw(*a))
    # Activation is a Popen in production so the watchdog keeps getting fed;
    # the outcome is all this test cares about.
    monkeypatch.setattr(svc.NetwatchService, "_activate",
                        lambda self, name, deadline: radio.nmcli("con", "up", name),
                        raising=False)
    return svc.NetwatchService()


def test_hotspot_dwell_is_measured_from_when_the_hotspot_started(monkeypatch):
    """Guard 1's 300s dwell used to start counting from the beginning of the
    failed Wi-Fi attempt, not from the moment the hotspot came up.

    The attempt blocks for up to WIFI_GRACE and the machine's clock was
    stamped once per poll, so the recorded start time was ~90s in the past.
    Measured on the rig, a 300s guard released the hotspot after 209s — which
    means a phone mid-setup can have the network pulled out from under it.
    """
    from skylapse.netwatch.statemachine import HOTSPOT_MIN_DWELL

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    radio.joinable = False                       # the house network is gone
    service = _service(monkeypatch, radio, clock)

    attempt_began = clock.time()
    service._now()
    service._execute(service.sm.on_boot())       # tries, fails, raises hotspot

    started = service.sm.ctx.hotspot_started_at
    assert started >= attempt_began + radio.fail_delay, (
        f"the hotspot's start time predates the end of the attempt that "
        f"provoked it by {attempt_began + radio.fail_delay - started:.0f}s, so "
        f"the dwell guard is already that far spent before the AP is even up")

    service.sm.ctx.now = started + HOTSPOT_MIN_DWELL - 1
    assert not service.sm.ctx.hotspot_dwell_ok()
    service.sm.ctx.now = started + HOTSPOT_MIN_DWELL
    assert service.sm.ctx.hotspot_dwell_ok()


def test_recovery_activates_the_profile_instead_of_waiting_for_autoconnect(monkeypatch):
    """The route home has to call `con up` by name.

    Bringing the hotspot down leaves the device flagged "disconnected by user
    or client", and NetworkManager will not autoconnect a device in that
    state. The old code lowered the hotspot and waited out the grace window
    for an autoconnect that provably never came — verified against the NM
    journal, which logged nothing at all for the full 92 seconds.
    """
    from skylapse.netwatch.statemachine import State

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    radio.joinable = False
    service = _service(monkeypatch, radio, clock)

    service._now()
    service._execute(service.sm.on_boot())
    assert service.sm.ctx.state is State.HOTSPOT

    radio.joinable = True                        # the house network is back
    radio.activations.clear()
    service._now()
    service._try_known_networks(preferred="yourmomshouse")

    assert "netplan-wlan0-yourmomshouse" in radio.activations, \
        "recovery waited for an autoconnect that NetworkManager never issues"
    assert service.sm.ctx.state is State.CONNECTED


def test_the_network_the_rescan_found_is_tried_first(monkeypatch):
    """The rescan knows which SSID it saw; spending grace window on the others
    first is how a short window gets used up before reaching the right one."""
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    service = _service(monkeypatch, radio, clock)

    service._now()
    service._try_known_networks(preferred="yourmomshouse")
    assert radio.activations[0] == "netplan-wlan0-yourmomshouse"


def test_wifi_only_mode_backs_off_instead_of_retrying_flat_out(monkeypatch):
    """wifi_only has no hotspot to fall back to, so a failure is answered with
    another attempt — which the service used to run immediately, from inside
    the attempt that had just failed. That is unbounded recursion with no pause
    between tries, hammering nmcli for as long as Wi-Fi is down.

    Handing the retry back to the poll loop with a delay is also the only thing
    that makes guard 1's backoff schedule reachable: on the automatic path the
    second step is never used, because a second consecutive failure raises the
    hotspot instead.
    """
    from skylapse.netwatch.statemachine import BACKOFF_STEPS, Mode

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.joinable = clock, False
    service = _service(monkeypatch, radio, clock)
    service.sm.ctx.mode = Mode.WIFI_ONLY

    service._now()
    service._try_known_networks()

    assert radio.activations.count("netplan-wlan0-yourmomshouse") == 1, \
        "retried inside the failed attempt instead of scheduling one"
    # The first step belongs to the loss itself, which is counted before any
    # attempt has failed; by the time an attempt fails we are on the second.
    assert service._retry_at == clock.time() + BACKOFF_STEPS[1]

    # Each further failure escalates, and the schedule caps rather than growing
    # without bound.
    delays = []
    for _ in range(len(BACKOFF_STEPS) + 2):
        clock.t = service._retry_at
        service._now()
        service._poll_events()          # fires the retry, which fails again
        delays.append(round(service._retry_at - clock.time()))

    assert delays == sorted(delays), f"backoff did not escalate: {delays}"
    assert max(delays) == BACKOFF_STEPS[-1], f"never reached the cap: {delays}"
    assert delays[-1] == BACKOFF_STEPS[-1], f"grew past its cap: {delays}"


def test_the_client_count_is_cleared_when_the_hotspot_comes_down(monkeypatch):
    """Reported from the rig: the camera was back on Wi-Fi, in managed mode,
    and still reporting one device attached to the access point.

    The count was only refreshed while the hotspot was up, so the last reading
    stood forever afterwards. It is not cosmetic -- the client-freeze guard
    reads this field to decide whether it may leave the hotspot.
    """
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    service = _service(monkeypatch, radio, clock)

    service._now()
    service._execute(service.sm.on_user_pick_standalone())
    radio.stations = 1
    service._poll_events()
    assert service.sm.ctx.hotspot_clients == 1

    # Back to Wi-Fi: the phone is gone with the network it was attached to.
    service._now()
    service._execute(service.sm.on_user_try_again())
    service._poll_events()

    assert service.sm.ctx.hotspot_clients == 0, \
        "still reporting a phone attached to a network that no longer exists"


# -- the radio has to be legally allowed to transmit -------------------------
#
# Two silent blockers stop a fresh Pi serving an access point: rfkill, and the
# world regulatory domain, where every channel is no-IR — no initiating
# radiation — which is exactly what an AP does. It matters more here than
# anywhere else, because the hotspot is how a camera with no network is reached
# at all. A camera that cannot raise it has no way in.

def test_the_hotspot_refuses_to_start_with_no_country_set(monkeypatch):
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.country = clock, "00"        # world domain: no-IR
    service = _service(monkeypatch, radio, clock)

    service._start_hotspot()

    assert radio.mode != "AP", "tried to transmit in the world domain"
    assert "Skylapse-Setup" not in radio.activations


def test_a_country_lets_the_hotspot_start(monkeypatch):
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.country = clock, "US"
    service = _service(monkeypatch, radio, clock)

    service._start_hotspot()
    assert radio.mode == "AP"


def test_the_configured_country_is_applied_to_the_radio(monkeypatch, tmp_path):
    """Set once in config, pushed to the radio on every hotspot start — a
    reboot resets the domain, and the camera must not need a human to reapply
    it before it can be reached."""
    from skylapse import config
    from skylapse.netwatch import service as svc

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    cfg.network.country = "GB"
    config.save(cfg)

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.country = clock, "00"
    service = _service(monkeypatch, radio, clock)

    applied = []
    real_run = svc.subprocess.run
    def spy(cmd, *a, **kw):
        if cmd and cmd[0].endswith("iw") and "reg" in cmd and "set" in cmd:
            applied.append(cmd[-1])
            radio.country = cmd[-1]
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(svc.subprocess, "run", spy)

    service._start_hotspot()
    assert applied == ["GB"], f"country was not pushed to the radio: {applied}"


def test_the_radio_is_unblocked_before_anything_else(monkeypatch):
    """rfkill soft-blocks Wi-Fi on a fresh image. Nothing else works until it
    is cleared, and nothing reports it."""
    from skylapse.netwatch import service as svc

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    service = _service(monkeypatch, radio, clock)

    seen = []
    real_run = svc.subprocess.run
    monkeypatch.setattr(svc.subprocess, "run",
                        lambda cmd, *a, **kw: (seen.append(cmd[0]),
                                               real_run(cmd, *a, **kw))[1])
    service._start_hotspot()
    assert "rfkill" in seen, "never unblocked the radio"


def test_a_missing_system_tool_does_not_kill_netwatch(monkeypatch):
    """It did, on the first SD image. iw and rfkill are not on Pi OS Lite, the
    radio-readiness check called both, and FileNotFoundError escaped the poll
    loop — so systemd restarted netwatch every five seconds forever.

    On a camera with no Wi-Fi that means no access point and no way in at all,
    which is the worst outcome this whole subsystem exists to prevent. Nothing
    netwatch shells out to may be load-bearing on its own existence.
    """
    from skylapse.netwatch import service as svc

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock, radio.country = clock, "00"
    service = _service(monkeypatch, radio, clock)

    def nothing_installed(cmd, *a, **kw):
        raise FileNotFoundError(f"No such file or directory: {cmd[0]!r}")

    monkeypatch.setattr(svc.subprocess, "run", nothing_installed)
    service._start_hotspot()          # must not raise
    service._poll_events()            # nor must the loop that calls it


def test_the_helper_reports_whether_the_tool_ran(monkeypatch):
    from skylapse.netwatch import service as svc

    monkeypatch.setattr(svc.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert svc._run("rfkill", "unblock", "wifi") is False


def test_a_failed_hotspot_is_reported_as_failed(monkeypatch, caplog):
    """It used to log "Hotspot up" unconditionally, two lines below a warning
    saying the activation had failed — contradicting itself in the same
    journal. That is how "no access point at all" read as "netwatch believes it
    has one" for a whole debugging session."""
    import logging

    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    service = _service(monkeypatch, radio, clock)

    # The radio refuses to become an access point, as it does when
    # NetworkManager's software switch is off.
    monkeypatch.setattr(service, "_in_ap_mode", lambda: False)
    with caplog.at_level(logging.INFO):
        service._start_hotspot()

    assert not any("up on" in r.message for r in caplog.records), \
        "claimed the hotspot came up"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "a hotspot that did not start must be an error, not silence"


def test_the_software_radio_switch_is_turned_on(monkeypatch):
    """Raspberry Pi OS Lite ships NetworkManager's own wireless switch off,
    persisted in its state file. rfkill is clear and the regulatory domain is
    fine, and wlan0 is still unavailable — so the hotspot profile binds to
    nothing and NM falls back to eth0."""
    clock, radio = FakeClock(), FakeRadio(FakeClock())
    radio.clock = clock
    service = _service(monkeypatch, radio, clock)

    seen = []
    original = radio.nmcli
    radio.nmcli = lambda *a, **kw: (seen.append(" ".join(a)), original(*a, **kw))[1]

    service._radio_ready()
    assert any("radio wifi on" in cmd for cmd in seen), \
        f"never enabled the radio: {seen}"
