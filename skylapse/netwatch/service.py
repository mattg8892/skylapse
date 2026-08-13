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
from .statemachine import Action, Mode, NetContext, NetStateMachine, State

log = logging.getLogger("skylapse.netwatch")

POLL_S = 5


def _nmcli(*args: str, timeout: int = 30) -> str:
    return subprocess.run(["nmcli", *args], capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


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
        state = self.sm.ctx.state
        wifi_up = self._wifi_connected()
        if state == State.CONNECTED and not wifi_up:
            log.info("Connection lost; backoff %ss", self.sm.ctx.backoff_delay())
            time.sleep(self.sm.ctx.backoff_delay())
            self._execute(self.sm.on_connection_lost())
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
        deadline = time.time() + 90
        while time.time() < deadline:
            if self._wifi_connected():
                self._execute(self.sm.on_wifi_connected())
                return
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
        if self.hotspot_ssid in _nmcli("-t", "-f", "NAME", "con", "show", "--active"):
            return
        _nmcli("dev", "wifi", "hotspot", "ssid", self.hotspot_ssid)
        log.info("Hotspot %s up", self.hotspot_ssid)

    def _stop_hotspot(self) -> None:
        if self.hotspot_ssid in _nmcli("-t", "-f", "NAME", "con", "show", "--active"):
            _nmcli("con", "down", self.hotspot_ssid)

    # -- probes ------------------------------------------------------------

    def _wifi_connected(self) -> bool:
        out = _nmcli("-t", "-f", "DEVICE,TYPE,STATE", "dev")
        return any(":wifi:connected" in line and self.hotspot_ssid not in line
                   for line in out.splitlines())

    def _hotspot_client_count(self) -> int:
        try:
            out = subprocess.run(["iw", "dev", "wlan0", "station", "dump"],
                                 capture_output=True, text=True, timeout=10).stdout
            return out.count("Station ")
        except Exception:
            return 0

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
