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
