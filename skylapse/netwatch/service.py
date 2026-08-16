"""Netwatch service: feeds real-world events into the state machine and
executes the actions it returns via NetworkManager (nmcli for the scaffold;
D-Bus is the follow-up, issue #2).

All decisions live in statemachine.py. This file only observes and executes.
Runs under systemd with WatchdogSec=30 (guard 6) — sd_notify keepalives below.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time

from .. import config
from .statemachine import (WIFI_GRACE, Action, Mode, NetContext,
                           NetStateMachine, State)

log = logging.getLogger("skylapse.netwatch")

POLL_S = 5


def _nmcli(*args: str, timeout: int = 30) -> str:
    """Run nmcli, logging failures.

    This used to discard the return code and stderr, which is why every fault
    in this file was invisible: a refused hotspot and a successful one looked
    identical to the caller.
    """
    result = subprocess.run(["nmcli", *args], capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode != 0:
        log.warning("nmcli %s failed (%d): %s", " ".join(args),
                    result.returncode, (result.stderr or "").strip())
    return result.stdout.strip()


def _iw(*args: str, timeout: int = 10) -> str:
    try:
        return subprocess.run(["iw", *args], capture_output=True,
                              text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _sd_notify(msg: str) -> None:
    try:
        import socket, os
        addr = os.environ.get("NOTIFY_SOCKET")
        if addr:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.sendto(msg.encode(), addr.replace("@", "\0", 1))
            s.close()
    except Exception:
        pass


class NetwatchService:
    def __init__(self) -> None:
        cfg = config.load()
        self.sm = NetStateMachine(NetContext(mode=Mode(cfg.network.mode)))
        self.hotspot_ssid = cfg.network.hotspot_ssid
        self._retry_at = 0.0          # non-blocking backoff deadline

    def run(self) -> None:
        _sd_notify("READY=1")
        self._execute(self.sm.on_boot())
        while True:
            self.sm.ctx.now = time.time()
            self._poll_events()
            self._poll_commands()
            self._write_status()
            _sd_notify("WATCHDOG=1")
            time.sleep(POLL_S)

    # -- observe -----------------------------------------------------------

    def _poll_events(self) -> None:
        # A pending backoff is served by returning early, never by sleeping.
        # Backoff reaches 600s and the unit sets WatchdogSec=30, so sleeping it
        # out here would starve the keepalive: systemd would kill netwatch, the
        # restart would reset the state machine to BOOT, and guard 1's
        # escalating backoff would silently never escalate.
        now = time.time()
        if self._retry_at:
            if now < self._retry_at:
                return
            self._retry_at = 0.0
            self._execute(self.sm.on_connection_lost())
            return

        state = self.sm.ctx.state
        wifi_up = self._wifi_connected()
        if state == State.CONNECTED and not wifi_up:
            delay = self.sm.ctx.backoff_delay()
            log.info("Connection lost; retrying in %ss", delay)
            self._retry_at = now + delay
        elif state in (State.HOTSPOT, State.STANDALONE):
            self.sm.on_hotspot_client_change(self._hotspot_client_count())
            if state == State.HOTSPOT:
                for ssid in self._visible_known_networks():
                    self._execute(self.sm.on_background_rescan_found_network(ssid))
                    break

    def _poll_commands(self) -> None:
        cmd_file = config.RUN_DIR / "netwatch_cmd.json"
        if not cmd_file.exists():
            return
        try:
            cmd = json.loads(cmd_file.read_text())
        finally:
            cmd_file.unlink(missing_ok=True)
        if cmd.get("cmd") == "retry":
            self._execute(self.sm.on_user_try_again())
        elif cmd.get("cmd") == "standalone":
            self._execute(self.sm.on_user_pick_standalone(bool(cmd.get("always"))))
        elif cmd.get("cmd") == "join":
            self._join(cmd["ssid"], cmd.get("password", ""))

    # -- execute -----------------------------------------------------------

    def _execute(self, action: Action) -> None:
        if action == Action.START_WIFI_ATTEMPT:
            self._try_known_networks()
        elif action == Action.START_HOTSPOT:
            self._start_hotspot()
        elif action == Action.STOP_HOTSPOT:
            self._stop_hotspot()

    def _try_known_networks(self) -> None:
        # nmcli autoconnects saved profiles when radio is in client mode.
        self._stop_hotspot()
        deadline = time.time() + WIFI_GRACE
        while time.time() < deadline:
            if self._wifi_connected():
                self._execute(self.sm.on_wifi_connected())
                return
            # Keep the watchdog fed: this wait is three times WatchdogSec, so
            # a silent sleep here gets the service killed mid-attempt.
            _sd_notify("WATCHDOG=1")
            time.sleep(3)
        # TODO(issue #2): read NM's per-connection failure reason to pass
        # auth_failure/ssid accurately instead of a generic failure.
        self._execute(self.sm.on_wifi_attempt_failed())

    def _join(self, ssid: str, password: str) -> None:
        try:
            _nmcli("dev", "wifi", "connect", ssid, "password", password, timeout=90)
            if self._wifi_connected():
                self._execute(self.sm.on_wifi_connected())
                return
            self._execute(self.sm.on_wifi_attempt_failed(ssid=ssid, auth_failure=True))
        except subprocess.TimeoutExpired:
            self._execute(self.sm.on_wifi_attempt_failed(ssid=ssid))

    def _start_hotspot(self) -> None:
        if self._in_ap_mode():
            return                        # already broadcasting; idempotent
        iface = self._wifi_iface()
        if not iface:
            log.error("No Wi-Fi interface; cannot start the hotspot")
            return
        # con-name matters: without it NetworkManager names the profile
        # "Hotspot" regardless of SSID, and every later lookup by SSID misses —
        # which meant the hotspot could be raised but never torn down.
        _nmcli("dev", "wifi", "hotspot", "ifname", iface,
               "con-name", self.hotspot_ssid, "ssid", self.hotspot_ssid)
        log.info("Hotspot %s up on %s", self.hotspot_ssid, iface)

    def _stop_hotspot(self) -> None:
        """Bring down whatever connection is holding the radio in AP mode.

        Matching on the connection name alone is fragile — a hotspot raised by
        an older build, or by hand, is named "Hotspot". Acting on the radio's
        actual mode works regardless of what the profile is called.
        """
        if not self._in_ap_mode():
            return
        iface = self._wifi_iface()
        name = self._active_connection(iface)
        if name:
            _nmcli("con", "down", name)
            log.info("Hotspot %s down", name)

    # -- probes ------------------------------------------------------------

    def _wifi_iface(self) -> str:
        """The Wi-Fi interface, from NetworkManager rather than assumed.

        wlan0 is the usual answer on a Pi but it is not a contract, and every
        probe below reads the wrong radio if it guesses wrong.
        """
        for line in _nmcli("-t", "-f", "DEVICE,TYPE", "dev").splitlines():
            device, _, kind = line.partition(":")
            if kind == "wifi":
                return device
        return ""

    def _active_connection(self, iface: str) -> str:
        if not iface:
            return ""
        for line in _nmcli("-t", "-f", "DEVICE,CONNECTION", "dev").splitlines():
            device, _, name = line.partition(":")
            if device == iface:
                return name
        return ""

    def _in_ap_mode(self) -> bool:
        """Whether the radio is currently an access point.

        `iw info` reports the operating mode directly, which nmcli's device
        state does not: it says "connected" whether the radio has joined a
        network or is serving one.
        """
        iface = self._wifi_iface()
        if not iface:
            return False
        for line in _iw("dev", iface, "info").splitlines():
            if line.strip().startswith("type "):
                return line.split()[1] == "AP"
        return False

    def _wifi_connected(self) -> bool:
        """Joined to a real network as a client.

        Being our own access point is not being connected. The radio reports
        "connected" either way, and conflating them told the state machine the
        house network was fine while the Pi was actually the AP.
        """
        if self._in_ap_mode():
            return False
        iface = self._wifi_iface()
        for line in _nmcli("-t", "-f", "DEVICE,TYPE,STATE", "dev").splitlines():
            device, _, rest = line.partition(":")
            if device == iface and rest == "wifi:connected":
                return True
        return False

    def _hotspot_client_count(self) -> int:
        """Stations attached to our access point.

        Only meaningful in AP mode: `iw station dump` on a client interface
        lists the access point we are joined to, so in client mode this read 1
        permanently — which would have frozen guard 2 and made the hotspot
        untearable-down for a reason nobody would ever guess.
        """
        iface = self._wifi_iface()
        if not iface or not self._in_ap_mode():
            return 0
        return _iw("dev", iface, "station", "dump").count("Station ")

    def _visible_known_networks(self) -> list[str]:
        known = set(_nmcli("-t", "-f", "NAME", "con", "show").splitlines())
        known.discard(self.hotspot_ssid)
        visible = set(_nmcli("-t", "-f", "SSID", "dev", "wifi", "list").splitlines())
        return [s for s in known & visible
                if not self.sm.ctx.network_blacklisted(s)]

    def _write_status(self) -> None:
        config.RUN_DIR.mkdir(parents=True, exist_ok=True)
        c = self.sm.ctx
        (config.RUN_DIR / "netwatch.json").write_text(json.dumps({
            "state": c.state.value, "mode": c.mode.value,
            "session_standalone": c.session_standalone,
            "hotspot_clients": c.hotspot_clients,
            "auth_failures": c.auth_failures, "updated": time.time(),
        }))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    NetwatchService().run()


if __name__ == "__main__":
    main()
