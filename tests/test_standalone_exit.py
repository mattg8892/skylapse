"""Getting back out of "use standalone for now".

Guard 5 lets someone say "not tonight" to Wi-Fi: the camera stays an access
point for the session, and a reboot clears it. The way back out before a reboot
is the Try Again button, and that path had a hole — the flag survived it, so a
retry that failed left the camera in HOTSPOT with the background rescan
silently switched off. It looks exactly like a camera waiting for its network
to come back, and it never checks.
"""
from __future__ import annotations

from skylapse.netwatch.statemachine import (Action, NetStateMachine, State)


def _standalone_by_choice() -> NetStateMachine:
    m = NetStateMachine()
    m.on_boot()
    m.on_wifi_attempt_failed()                  # no network -> hotspot
    m.on_user_pick_standalone()                 # "use standalone for now"
    assert m.ctx.state is State.STANDALONE and m.ctx.session_standalone
    return m


def test_try_again_revokes_the_standalone_choice():
    m = _standalone_by_choice()
    assert m.on_user_try_again() is Action.START_WIFI_ATTEMPT
    assert not m.ctx.session_standalone


def test_a_failed_retry_leaves_the_rescan_working():
    """The failure case is the one that matters: success would have cleared the
    state anyway, but a failed retry is where the camera has to be left able to
    find its own way home."""
    m = _standalone_by_choice()
    m.on_user_try_again()
    m.on_wifi_attempt_failed()
    assert m.ctx.state is State.HOTSPOT

    # Dwell runs from when the access point went up, which was before the
    # retry; push well past it either way.
    m.ctx.now = m.ctx.hotspot_started_at + 10_000
    assert m.on_background_rescan_found_network("yourmomshouse") is \
        Action.START_WIFI_ATTEMPT, \
        "the rescan is still disabled by a choice the user has since revoked"


def test_choosing_standalone_again_still_sticks():
    """Revoking on Try Again must not make the choice itself unreliable."""
    m = _standalone_by_choice()
    m.on_user_try_again()
    m.on_wifi_attempt_failed()
    m.on_user_pick_standalone()
    m.ctx.now = 10_000

    assert m.ctx.state is State.STANDALONE
    assert m.on_background_rescan_found_network("yourmomshouse") is Action.NONE


def test_always_standalone_is_not_revoked_by_try_again():
    """"Always" is a persisted mode, not a session choice. Try Again may run an
    attempt, but it must not quietly turn the setting off."""
    from skylapse.netwatch.statemachine import Mode

    m = _standalone_by_choice()
    m.on_user_pick_standalone(always=True)
    m.on_user_try_again()
    assert m.ctx.mode is Mode.STANDALONE
