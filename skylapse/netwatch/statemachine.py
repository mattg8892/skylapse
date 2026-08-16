"""Network state machine — pure logic, no I/O.

Every loop guard from DESIGN.md is encoded here and unit-tested in
tests/test_statemachine.py. The NetworkManager glue in service.py feeds
events in and executes the actions this machine returns; it makes no
decisions of its own.

States:  BOOT -> TRY_WIFI -> {CONNECTED | HOTSPOT} ; HOTSPOT -> STANDALONE
Guards:  reconnect backoff, hotspot dwell, client-connected freeze,
         auth-failure blacklist, session-only standalone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    BOOT = "boot"
    TRY_WIFI = "try_wifi"
    CONNECTED = "connected"
    HOTSPOT = "hotspot"
    STANDALONE = "standalone"


class Mode(str, Enum):
    AUTO = "auto"
    STANDALONE = "standalone"   # "always use standalone mode" checkbox
    WIFI_ONLY = "wifi_only"


class Action(str, Enum):
    START_WIFI_ATTEMPT = "start_wifi_attempt"
    START_HOTSPOT = "start_hotspot"
    STOP_HOTSPOT = "stop_hotspot"
    NONE = "none"


# Guard constants (seconds)
WIFI_GRACE = 90                  # per-attempt connection window
BACKOFF_STEPS = (10, 30, 120, 600)   # guard 1: reconnect backoff, capped
HOTSPOT_MIN_DWELL = 300          # guard 1: hotspot stays up at least this long
AUTH_FAIL_LIMIT = 3              # guard 3: strikes before session blacklist


@dataclass
class NetContext:
    mode: Mode = Mode.AUTO
    state: State = State.BOOT
    session_standalone: bool = False          # guard 5: cleared by reboot
    reconnect_attempts: int = 0
    hotspot_started_at: float | None = None
    hotspot_clients: int = 0                  # guard 2
    auth_failures: dict[str, int] = field(default_factory=dict)   # guard 3
    now: float = field(default_factory=time.time)

    # -- guard queries -----------------------------------------------------

    def backoff_delay(self) -> int:
        i = min(self.reconnect_attempts, len(BACKOFF_STEPS) - 1)
        return BACKOFF_STEPS[i]

    def hotspot_dwell_ok(self) -> bool:
        if self.hotspot_started_at is None:
            return True
        return (self.now - self.hotspot_started_at) >= HOTSPOT_MIN_DWELL

    def network_blacklisted(self, ssid: str) -> bool:
        return self.auth_failures.get(ssid, 0) >= AUTH_FAIL_LIMIT

    def may_leave_hotspot(self, user_initiated: bool = False) -> bool:
        """Guards 1 + 2: never tear down hotspot while a phone is attached
        (unless the user themself asked from that phone), and respect dwell."""
        if self.hotspot_clients > 0 and not user_initiated:
            return False
        if not user_initiated and not self.hotspot_dwell_ok():
            return False
        return True


class NetStateMachine:
    def __init__(self, ctx: NetContext | None = None) -> None:
        self.ctx = ctx or NetContext()

    # -- events ------------------------------------------------------------

    def on_boot(self) -> Action:
        c = self.ctx
        c.state = State.BOOT
        c.session_standalone = False          # guard 5: reboot self-heals
        c.reconnect_attempts = 0
        c.auth_failures.clear()
        if c.mode == Mode.STANDALONE:
            c.state = State.STANDALONE
            return Action.START_HOTSPOT
        c.state = State.TRY_WIFI
        return Action.START_WIFI_ATTEMPT

    def on_wifi_connected(self) -> Action:
        c = self.ctx
        c.state = State.CONNECTED
        c.reconnect_attempts = 0
        c.hotspot_started_at = None
        return Action.STOP_HOTSPOT

    def on_wifi_attempt_failed(self, ssid: str | None = None,
                               auth_failure: bool = False) -> Action:
        c = self.ctx
        if auth_failure and ssid:
            c.auth_failures[ssid] = c.auth_failures.get(ssid, 0) + 1  # guard 3
        c.reconnect_attempts += 1
        if c.mode == Mode.WIFI_ONLY:
            c.state = State.TRY_WIFI          # keep trying with backoff, no hotspot
            return Action.START_WIFI_ATTEMPT
        c.state = State.HOTSPOT
        if c.hotspot_started_at is None:
            c.hotspot_started_at = c.now
        return Action.START_HOTSPOT

    def on_connection_lost(self) -> Action:
        """Connected Wi-Fi dropped. Retry with backoff (guard 1)."""
        c = self.ctx
        c.state = State.TRY_WIFI
        return Action.START_WIFI_ATTEMPT      # service.py sleeps backoff_delay() first

    def on_background_rescan_found_network(self, ssid: str) -> Action:
        """Known network reappeared while in hotspot mode."""
        c = self.ctx
        if c.state != State.HOTSPOT:
            return Action.NONE
        if c.session_standalone:              # guard 5: user said not tonight
            return Action.NONE
        if c.network_blacklisted(ssid):       # guard 3
            return Action.NONE
        if not c.may_leave_hotspot():         # guards 1 + 2
            return Action.NONE
        c.state = State.TRY_WIFI
        return Action.START_WIFI_ATTEMPT

    def on_user_try_again(self) -> Action:
        """UI 'Try again' button — user-initiated, so dwell/client guards yield,
        but the UI must warn about the disconnect first (guard 2)."""
        c = self.ctx
        if not c.may_leave_hotspot(user_initiated=True):
            return Action.NONE
        c.state = State.TRY_WIFI
        c.reconnect_attempts = 0              # fresh user intent resets backoff
        c.auth_failures.clear()               # they may have fixed the password
        # Asking to try again revokes an earlier "use standalone for now".
        # Leaving guard 5 set would land the failed retry back in HOTSPOT with
        # the background rescan permanently disabled — a camera that looks like
        # it is waiting for its network to return, and never checks.
        c.session_standalone = False
        return Action.START_WIFI_ATTEMPT

    def on_user_pick_standalone(self, always: bool = False) -> Action:
        c = self.ctx
        c.session_standalone = True
        if always:
            c.mode = Mode.STANDALONE          # persisted by service.py to config
        c.state = State.STANDALONE
        return Action.START_HOTSPOT           # idempotent if already up

    def on_hotspot_client_change(self, count: int) -> Action:
        self.ctx.hotspot_clients = count
        return Action.NONE
