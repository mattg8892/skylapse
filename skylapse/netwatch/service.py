"""Netwatch service: feeds real-world events into the state machine and
executes the actions it returns via NetworkManager (nmcli for the scaffold;
D-Bus is the follow-up, issue #2).

All decisions live in statemachine.py. This file only observes and executes.
Runs under systemd with WatchdogSec=30 (guard 6) — sd_notify keepalives below.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
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


def _nmcli_result(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Like _nmcli but hands back the whole result, for callers that must
    classify *why* something failed rather than just that it did."""
    result = subprocess.run(["nmcli", *args], capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode != 0:
        log.info("nmcli %s -> %d: %s", " ".join(args), result.returncode,
                 (result.stderr or result.stdout or "").strip().splitlines()[:1])
    return result


# Signatures measured against NetworkManager on Bookworm/trixie. A wrong
# passphrase surfaces as a secrets request, because the supplicant disconnects
# and NM concludes the stored key must be wrong — it never says "bad password".
_AUTH_MARKERS = ("secrets were required", "no-secrets", "passwords or encryption keys",
                 "invalid password", "authentication")
_NOT_FOUND_MARKERS = ("no network with ssid", "not found")


def classify_join_failure(output: str) -> str:
    """Why a join failed: 'auth', 'not_found', or 'other'.

    Guard 3 blacklists a network after repeated *credential* failures. Treating
    an out-of-range network as an auth failure would blacklist the network the
    camera belongs on, so the distinction is load-bearing.
    """
    text = (output or "").lower()
    if any(marker in text for marker in _AUTH_MARKERS):
        return "auth"
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        return "not_found"
    return "other"


def _iw(*args: str, timeout: int = 10) -> str:
    """Run iw, which lives in /usr/sbin and is not on every PATH.

    A missing binary here is silent and total: `_in_ap_mode` would answer False
    forever, so the service would believe it is never an access point and every
    hotspot decision would be made backwards. Worth one log line.
    """
    binary = shutil.which("iw") or "/usr/sbin/iw"
    try:
        return subprocess.run([binary, *args], capture_output=True,
                              text=True, timeout=timeout).stdout
    except FileNotFoundError:
        log.error("iw not found — cannot determine the radio's mode")
        return ""
    except Exception as exc:
        log.warning("iw %s failed: %s", " ".join(args), exc)
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
        self.hotspot_deadline = cfg.network.hotspot_until or 0.0

    def _now(self) -> float:
        """Stamp the state machine's clock, immediately before an event.

        Every guard in the machine is a duration, and several events are raised
        from inside a call that just blocked for most of WIFI_GRACE. A snapshot
        taken once per poll is therefore up to 90s stale at exactly the moments
        that set and read timestamps. Measured on the rig: the hotspot recorded
        its start time as the moment the *attempt* began, so the 300s dwell
        guard released it after 209s.
        """
        self.sm.ctx.now = time.time()
        return self.sm.ctx.now

    def run(self) -> None:
        _sd_notify("READY=1")
        self._now()
        self._execute(self.sm.on_boot())
        while True:
            self._now()
            self._poll_config()
            self._poll_events()
            self._poll_commands()
            self._write_status()
            _sd_notify("WATCHDOG=1")
            time.sleep(POLL_S)

    # -- observe -----------------------------------------------------------

    def _poll_config(self) -> None:
        """Apply a mode chosen from Settings, and expire a timed AP session.

        The mode lives in the config file rather than in a command, because it
        has to survive a restart: someone who put the camera into access-point
        mode to work on it in the field must not find it back on Wi-Fi after a
        power cut. Reading it every poll is what makes the Settings switch take
        effect without restarting the service — and restarting the service to
        change network mode is precisely the thing this control exists to avoid.
        """
        cfg = config.load()
        desired = Mode(cfg.network.mode)
        until = cfg.network.hotspot_until or 0.0

        if desired == Mode.STANDALONE and until and time.time() >= until:
            log.info("Timed access-point session expired; returning to auto")
            cfg.network.mode, cfg.network.hotspot_until = "auto", 0.0
            config.save(cfg)
            desired, until = Mode.AUTO, 0.0

        self.hotspot_deadline = until
        if desired == self.sm.ctx.mode:
            return

        log.info("Mode changed: %s -> %s", self.sm.ctx.mode.value, desired.value)
        self.sm.ctx.mode = desired
        self._now()
        if desired == Mode.STANDALONE:
            self._execute(self.sm.on_user_pick_standalone())
        else:
            # Leaving AP mode is user intent, so it clears the session-only
            # standalone flag and the auth strikes along with it.
            self.sm.ctx.session_standalone = False
            self._execute(self.sm.on_user_try_again())

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
            self._now()
            self._execute(self.sm.on_connection_lost())
            return

        state = self.sm.ctx.state
        wifi_up = self._wifi_connected()
        # Counted on every poll, including the ones where we are not an access
        # point. Updating it only while the hotspot was up left the last count
        # standing after the hotspot came down, so the camera went on reporting
        # a phone attached to a network that no longer existed — and the
        # client-freeze guard reads this field.
        self.sm.on_hotspot_client_change(self._hotspot_client_count())
        if state == State.CONNECTED and not wifi_up:
            delay = self.sm.ctx.backoff_delay()
            log.info("Connection lost; retrying in %ss", delay)
            self._retry_at = now + delay
        elif state in (State.HOTSPOT, State.STANDALONE):
            if state == State.HOTSPOT:
                for ssid in self._visible_known_networks():
                    action = self.sm.on_background_rescan_found_network(ssid)
                    if action == Action.NONE:
                        log.debug("Rescan saw %r but a guard holds the hotspot "
                                  "(clients=%d dwell_ok=%s)", ssid,
                                  self.sm.ctx.hotspot_clients,
                                  self.sm.ctx.hotspot_dwell_ok())
                    else:
                        log.info("Rescan found %r; leaving hotspot", ssid)
                    self._execute(action, preferred=ssid)
                    break

    def _poll_commands(self) -> None:
        cmd_file = config.RUN_DIR / "netwatch_cmd.json"
        if not cmd_file.exists():
            return
        try:
            cmd = json.loads(cmd_file.read_text())
        finally:
            cmd_file.unlink(missing_ok=True)
        self._now()
        if cmd.get("cmd") == "retry":
            self._execute(self.sm.on_user_try_again())
        elif cmd.get("cmd") == "standalone":
            self._execute(self.sm.on_user_pick_standalone(bool(cmd.get("always"))))
        elif cmd.get("cmd") == "join":
            self._join(cmd["ssid"], cmd.get("password", ""))

    # -- execute -----------------------------------------------------------

    def _execute(self, action: Action, preferred: str = "") -> None:
        if action != Action.NONE:
            log.info("state=%s action=%s", self.sm.ctx.state.value, action.value)
        if action == Action.START_WIFI_ATTEMPT:
            self._try_known_networks(preferred)
        elif action == Action.START_HOTSPOT:
            self._start_hotspot()
        elif action == Action.STOP_HOTSPOT:
            self._stop_hotspot()

    def _activate(self, name: str, deadline: float):
        """Run `nmcli con up` without ever going quiet longer than the watchdog
        allows.

        Activation blocks for as long as it takes — up to the whole grace
        window — and the unit sets WatchdogSec=30, so it cannot simply be
        awaited. Returns the finished process, or None if the deadline cut it
        short.
        """
        proc = subprocess.Popen(["nmcli", "con", "up", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        while True:
            try:
                out, err = proc.communicate(timeout=3)
                return subprocess.CompletedProcess(proc.args, proc.returncode,
                                                   out, err)
            except subprocess.TimeoutExpired:
                _sd_notify("WATCHDOG=1")
                if time.time() >= deadline:
                    proc.kill()
                    proc.communicate()
                    log.warning("Activation of %r ran out of grace window", name)
                    return None

    def _try_known_networks(self, preferred: str = "") -> None:
        """Bring up known networks explicitly, best candidate first.

        This used to lower the hotspot and then simply wait for NetworkManager
        to autoconnect a saved profile. It cannot work, and the reason is
        circular: the wait is preceded by `nmcli con down`, which leaves the
        device flagged "disconnected by user or client" (reason 39), and NM
        does not autoconnect a device in that state. Measured on the rig, the
        full 90s grace window passed with NM logging nothing whatsoever, so a
        camera that had fallen back to its hotspot could never come home by
        itself — precisely the failure this subsystem exists to prevent.

        Activating explicitly also gives us NM's own error text per attempt,
        which is what guard 3 needs to tell a wrong password apart from a
        network that is merely out of range.
        """
        self._stop_hotspot()
        if self._wifi_connected():
            self._now()
            self._execute(self.sm.on_wifi_connected())
            return

        known = self._known_networks()
        order = [preferred] if preferred in known else []
        order += [s for s in self._visible_known_networks() if s not in order]
        # Saved-but-unseen networks are still worth a try: a scan taken
        # seconds after the radio came up is not evidence of absence, and at
        # boot an empty scan would otherwise drop straight to the hotspot.
        order += [s for s in known
                  if s not in order and not self.sm.ctx.network_blacklisted(s)]
        log.info("Trying %s (up to %ss)", order or "no known networks", WIFI_GRACE)

        deadline = time.time() + WIFI_GRACE
        last_ssid, auth_failure = preferred or None, False
        for ssid in order:
            if time.time() >= deadline:
                log.info("Grace window expired before trying %r", ssid)
                break
            result = self._activate(known[ssid], deadline)
            if self._wifi_connected():
                log.info("Joined %r", ssid)
                self._now()
                self._execute(self.sm.on_wifi_connected())
                return
            last_ssid = ssid
            if result is not None:
                reason = classify_join_failure(result.stdout + result.stderr)
                log.warning("Join of %r failed (%s)", ssid, reason)
                auth_failure = reason == "auth"
        self._now()
        action = self.sm.on_wifi_attempt_failed(ssid=last_ssid,
                                                auth_failure=auth_failure)
        if action == Action.START_WIFI_ATTEMPT:
            # Only wifi_only mode answers a failure with another attempt, since
            # it has no hotspot to fall back to. Executing it here would recurse
            # once per failure, with no pause between attempts, for as long as
            # Wi-Fi stayed down. Handing it back to the poll loop with a delay
            # is also what makes guard 1's backoff schedule reachable at all:
            # on the automatic path the second step is never used, because a
            # second consecutive failure raises the hotspot instead.
            delay = self.sm.ctx.backoff_delay()
            log.info("No hotspot in this mode; retrying in %ss", delay)
            self._retry_at = time.time() + delay
            return
        self._execute(action)

    def _join(self, ssid: str, password: str) -> None:
        """Join a network with a user-supplied password.

        Handles the already-saved case explicitly: `nmcli dev wifi connect` on
        an SSID that already has a profile fails with
        "802-11-wireless-security.key-mgmt: property is missing" rather than
        using the new password, so correcting a wrong password from the UI
        could never have worked.
        """
        existing = self._known_networks().get(ssid)
        try:
            if existing:
                _nmcli("con", "modify", existing,
                       "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password)
                result = _nmcli_result("con", "up", existing, timeout=WIFI_GRACE)
            else:
                result = _nmcli_result("dev", "wifi", "connect", ssid,
                                       "password", password, timeout=WIFI_GRACE)
        except subprocess.TimeoutExpired:
            log.warning("Join of %r timed out", ssid)
            self._now()
            self._execute(self.sm.on_wifi_attempt_failed(ssid=ssid))
            return

        self._now()
        if self._wifi_connected():
            self._execute(self.sm.on_wifi_connected())
            return

        reason = classify_join_failure(result.stdout + result.stderr)
        log.warning("Join of %r failed (%s)", ssid, reason)
        # Only a genuine credential rejection counts toward guard 3's strike
        # limit. Blacklisting a network because it was briefly out of range
        # would lock the camera out of the network it belongs on.
        self._execute(self.sm.on_wifi_attempt_failed(
            ssid=ssid, auth_failure=(reason == "auth")))

    def _ensure_hotspot_profile(self, iface: str) -> str:
        """Build the hotspot profile to match config. Returns the password.

        Written out explicitly rather than using `nmcli dev wifi hotspot`,
        because that convenience command *always* applies WPA with a key it
        generates itself. With the documented default of no password, the
        camera broadcast a network whose passphrase existed only inside
        NetworkManager — visible to a phone, joinable by nobody. That is the
        worst possible failure for the one feature whose entire job is to let
        you reach a camera you otherwise cannot.
        """
        password = config.load().network.hotspot_password
        # Recreated each time so a changed SSID or password actually applies.
        if self.hotspot_ssid in _nmcli("-t", "-f", "NAME", "con", "show").splitlines():
            _nmcli("con", "delete", self.hotspot_ssid)
        _nmcli("con", "add", "type", "wifi", "ifname", iface,
               "con-name", self.hotspot_ssid, "autoconnect", "no",
               "ssid", self.hotspot_ssid,
               "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
               "ipv4.method", "shared", "ipv6.method", "ignore")
        if password:
            _nmcli("con", "modify", self.hotspot_ssid,
                   "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password)
        return password

    def _start_hotspot(self) -> None:
        if self._in_ap_mode():
            return                        # already broadcasting; idempotent
        iface = self._wifi_iface()
        if not iface:
            log.error("No Wi-Fi interface; cannot start the hotspot")
            return
        password = self._ensure_hotspot_profile(iface)
        _nmcli("con", "up", self.hotspot_ssid)
        log.info("Hotspot %s up on %s (%s)", self.hotspot_ssid, iface,
                 "password protected" if password else "open")

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

    def _known_networks(self) -> dict[str, str]:
        """Saved Wi-Fi networks, as {ssid: connection name}.

        The SSID must be read out of each profile, because a connection's name
        is not its SSID. netplan generates names like
        "netplan-wlan0-yourmomshouse" for the SSID "yourmomshouse", so the
        previous version — which intersected connection *names* with scan
        results — found nothing in common on any netplan-managed Pi. The
        background rescan is the only automatic route from hotspot back to
        Wi-Fi, so that silently stranded a fallen-back camera in hotspot mode
        forever: exactly the failure this whole subsystem exists to prevent.
        """
        networks: dict[str, str] = {}
        for line in _nmcli("-t", "-f", "NAME,TYPE", "con", "show").splitlines():
            name, _, kind = line.rpartition(":")
            if kind != "802-11-wireless" or not name:
                continue
            ssid = _nmcli("-g", "802-11-wireless.ssid", "con", "show", name)
            if ssid and ssid != self.hotspot_ssid:
                networks[ssid] = name
        return networks

    def _visible_known_networks(self) -> list[str]:
        visible = {line for line in
                   _nmcli("-t", "-f", "SSID", "dev", "wifi", "list").splitlines() if line}
        return [ssid for ssid in self._known_networks()
                if ssid in visible and not self.sm.ctx.network_blacklisted(ssid)]

    def _write_status(self) -> None:
        config.RUN_DIR.mkdir(parents=True, exist_ok=True)
        c = self.sm.ctx
        (config.RUN_DIR / "netwatch.json").write_text(json.dumps({
            "state": c.state.value, "mode": c.mode.value,
            "session_standalone": c.session_standalone,
            "hotspot_clients": c.hotspot_clients,
            "hotspot_until": self.hotspot_deadline,
            "hotspot_ssid": self.hotspot_ssid,
            "auth_failures": c.auth_failures, "updated": time.time(),
        }))


def main() -> None:
    # Timestamps matter here more than anywhere else in the project: every
    # guard in this subsystem is a duration (backoff, dwell, grace), so a log
    # without times cannot show whether any of them actually held. journald
    # stamps its own, but this also runs in the foreground during bring-up.
    # SKYLAPSE_LOG_LEVEL=DEBUG surfaces the per-poll guard decisions, which is
    # the only way to tell "the rescan is holding the hotspot on purpose" apart
    # from "the rescan is not running" — they look identical at INFO.
    logging.basicConfig(
        level=os.environ.get("SKYLAPSE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s")
    NetwatchService().run()


if __name__ == "__main__":
    main()
