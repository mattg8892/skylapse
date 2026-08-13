"""Every loop-trap from DESIGN.md gets a test proving the guard holds."""
import time

from skylapse.netwatch.statemachine import (
    AUTH_FAIL_LIMIT, BACKOFF_STEPS, HOTSPOT_MIN_DWELL,
    Action, Mode, NetContext, NetStateMachine, State,
)


def machine(**ctx_kwargs) -> NetStateMachine:
    return NetStateMachine(NetContext(**ctx_kwargs))


# Guard 1a: reconnect backoff grows and caps -------------------------------

def test_backoff_escalates_and_caps():
    m = machine()
    m.on_boot()
    delays = []
    for _ in range(8):
        m.on_wifi_attempt_failed()
        delays.append(m.ctx.backoff_delay())
    assert delays[:3] == [30, 120, 600]          # attempt 1 already consumed step 0
    assert all(d == BACKOFF_STEPS[-1] for d in delays[3:])   # capped, never resets


# Guard 1b: hotspot minimum dwell blocks early teardown --------------------

def test_hotspot_dwell_blocks_rescan_switch():
    m = machine()
    m.on_boot()
    m.on_wifi_attempt_failed()                    # -> HOTSPOT, dwell clock starts
    assert m.ctx.state == State.HOTSPOT
    # 10 seconds later a known network reappears — too soon, stay put.
    m.ctx.now = m.ctx.hotspot_started_at + 10
    assert m.on_background_rescan_found_network("GleasonHome") == Action.NONE
    assert m.ctx.state == State.HOTSPOT
    # After the dwell window it may switch.
    m.ctx.now = m.ctx.hotspot_started_at + HOTSPOT_MIN_DWELL + 1
    assert m.on_background_rescan_found_network("GleasonHome") == Action.START_WIFI_ATTEMPT


# Guard 2: never strand a phone that's mid-setup ---------------------------

def test_client_connected_freezes_rescan():
    m = machine()
    m.on_boot()
    m.on_wifi_attempt_failed()
    m.ctx.now = m.ctx.hotspot_started_at + HOTSPOT_MIN_DWELL + 1
    m.on_hotspot_client_change(1)                 # phone joins
    assert m.on_background_rescan_found_network("GleasonHome") == Action.NONE
    m.on_hotspot_client_change(0)                 # phone leaves
    assert m.on_background_rescan_found_network("GleasonHome") == Action.START_WIFI_ATTEMPT


def test_user_try_again_overrides_guards_but_resets_cleanly():
    m = machine()
    m.on_boot()
    m.on_wifi_attempt_failed(ssid="GleasonHome", auth_failure=True)
    m.on_hotspot_client_change(1)                 # user is ON the hotspot
    action = m.on_user_try_again()                # explicit user intent wins
    assert action == Action.START_WIFI_ATTEMPT
    assert m.ctx.reconnect_attempts == 0          # fresh intent, fresh backoff
    assert m.ctx.auth_failures == {}              # maybe they fixed the password


# Guard 3: wrong password can't spin forever -------------------------------

def test_auth_failures_blacklist_network_for_session():
    m = machine()
    m.on_boot()
    for _ in range(AUTH_FAIL_LIMIT):
        m.on_wifi_attempt_failed(ssid="GleasonHome", auth_failure=True)
    assert m.ctx.network_blacklisted("GleasonHome")
    m.ctx.now = m.ctx.hotspot_started_at + HOTSPOT_MIN_DWELL + 1
    # Even after dwell, a blacklisted SSID never triggers a switch attempt.
    assert m.on_background_rescan_found_network("GleasonHome") == Action.NONE
    # But an unrelated network still can.
    assert m.on_background_rescan_found_network("ShopWiFi") == Action.START_WIFI_ATTEMPT


def test_blacklist_clears_on_reboot():
    m = machine()
    m.on_boot()
    for _ in range(AUTH_FAIL_LIMIT):
        m.on_wifi_attempt_failed(ssid="GleasonHome", auth_failure=True)
    m.on_boot()
    assert not m.ctx.network_blacklisted("GleasonHome")


# Guard 5: standalone is session-only unless the checkbox was ticked -------

def test_session_standalone_self_heals_on_reboot():
    m = machine()
    m.on_boot()
    m.on_wifi_attempt_failed()
    m.on_user_pick_standalone(always=False)
    assert m.ctx.state == State.STANDALONE
    # Rescans are ignored for the rest of the session.
    m.ctx.now = time.time() + HOTSPOT_MIN_DWELL + 1
    assert m.on_background_rescan_found_network("GleasonHome") == Action.NONE
    # Reboot goes back to trying Wi-Fi.
    assert m.on_boot() == Action.START_WIFI_ATTEMPT
    assert m.ctx.state == State.TRY_WIFI


def test_always_standalone_persists_across_boot():
    m = machine()
    m.on_boot()
    m.on_wifi_attempt_failed()
    m.on_user_pick_standalone(always=True)
    assert m.on_boot() == Action.START_HOTSPOT    # straight to hotspot, no Wi-Fi tries
    assert m.ctx.state == State.STANDALONE


# Mode: wifi_only never opens a hotspot ------------------------------------

def test_wifi_only_mode_retries_without_hotspot():
    m = machine(mode=Mode.WIFI_ONLY)
    m.on_boot()
    assert m.on_wifi_attempt_failed() == Action.START_WIFI_ATTEMPT
    assert m.ctx.state == State.TRY_WIFI


# Sanity: connected state resets everything --------------------------------

def test_connection_success_resets_backoff_and_dwell():
    m = machine()
    m.on_boot()
    for _ in range(4):
        m.on_wifi_attempt_failed()
    m.on_wifi_connected()
    assert m.ctx.reconnect_attempts == 0
    assert m.ctx.hotspot_started_at is None
    assert m.ctx.state == State.CONNECTED
