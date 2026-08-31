"""If a screen says it will recover on its own, it has to actually try.

Three of these were found in one setup session, and the person doing it
described the pattern before anyone had named it: "that seems to be pretty
universal across this whole build."

  - The Wi-Fi step said "the page will catch up on its own" and then sat on a
    spinner for ever. It never polled. The camera had connected minutes ago.
  - The camera step announced "Camera found -- take a test shot" while the
    button to do that sat behind a stale list, so it appeared only after a
    manual reload.
  - Restart said "this page will go quiet, then come back on its own" and did
    nothing whatsoever to make that true.

Each was written in good faith and each was a lie by omission, because the
recovering half was never built. Telling someone to wait for something that is
not coming is worse than telling them to reload: they wait, then they reload
anyway, and now they do not trust the next message either.

So: a screen that promises to recover must contain the machinery to do it --
a poll, a reload, or a callback that refreshes whatever it is talking about.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "src"

# Phrases that promise the interface will move on without the user.
PROMISES = (
    "on its own",
    "by itself",
    "catch up",
    "carries on",
    "reloads itself",
)

# What keeping that promise looks like in this codebase.
MACHINERY = ("setInterval", "setTimeout", "location.reload", "onFound",
             "onChanged", "onJoined", "refresh")

# Promises about hardware or the server, which the page cannot and should not
# implement. "The camera rejoins this network by itself after a power cut" is a
# statement about the camera, not an undertaking by this screen.
ABOUT_THE_MACHINE = re.compile(
    r"(camera|session|it) (keeps|rejoins|stops|exits|comes back)|"
    r"stops by itself|exits by itself|"
    r"the Pi can.t find by itself|does not show up by itself|"
    r"cannot find by itself|not enough on its own|"
    r"scrolls on its own",
    re.I)


def components() -> list[Path]:
    return sorted(WEB.rglob("*.jsx"))


@pytest.mark.parametrize("path", components(), ids=lambda p: p.name)
def test_a_promise_to_self_recover_comes_with_the_means_to_do_it(path):
    src = path.read_text(encoding="utf-8")
    # Comments explain the code; they do not appear on screen.
    visible = "\n".join(line for line in src.splitlines()
                        if not line.strip().startswith(("*", "//", "/*")))

    promised = [p for p in PROMISES if p in visible and not ABOUT_THE_MACHINE.search(visible)]
    if not promised:
        return
    assert any(m in src for m in MACHINERY), (
        f"{path.name} tells the user it will recover on its own ({promised}) "
        f"but contains no poll, reload or refresh callback to make that true. "
        f"Either build the recovery or stop promising it.")


def test_the_restart_button_reloads_the_page_itself():
    """The specific one that was reported. It said the page comes back on its
    own and did nothing at all toward that."""
    src = (WEB / "screens" / "SettingsScreen.jsx").read_text(encoding="utf-8")
    body = src.split("function RestartButton", 1)[1].split("\nfunction ", 1)[0]
    assert "location.reload" in body, "restart must bring the page back itself"
    assert "/api/status" in body, "and it has to wait for the camera to answer"


def test_the_restart_gives_up_rather_than_spinning_for_ever():
    """A camera that is not coming back must not be represented by a spinner
    that never stops. That is the same failure in a nicer costume."""
    src = (WEB / "screens" / "SettingsScreen.jsx").read_text(encoding="utf-8")
    body = src.split("function RestartButton", 1)[1].split("\nfunction ", 1)[0]
    assert "stuck" in body, "no timeout state"
    assert re.search(r"\d[\d_]*\s*\)?\s*\{?\s*$|240_000|240000", body), \
        "no deadline on the poll"


def test_joining_wifi_reports_success():
    """It used to stay on 'Connecting...' for ever by design, reasoning that
    the page might be losing the network. True sometimes; the rest of the time
    it hid the fact that the job was done."""
    src = (WEB / "components" / "JoinNetwork.jsx").read_text(encoding="utf-8")
    assert "'joined'" in src, "no success state"
    assert "/api/network" in src, "nothing checks whether it worked"
    assert "Continue" in src, "does not tell the user what to do next"


def test_finding_a_camera_tells_the_screen_that_owns_the_button():
    """"Take a test shot" is useless advice if the button is not rendered."""
    src = (WEB / "components" / "camera.jsx").read_text(encoding="utf-8")
    assert "onFound?.()" in src, "WaitingForReboot never notifies its parent"
    assert "<DeclareSensor onChanged=" in src, "and the parent never passes one"


# -- alerts that always fire ------------------------------------------------

def test_a_missing_temperature_rise_is_not_styled_as_a_warning():
    """On a sealed dome the sensor is outside and the heater is inside, so it
    cannot see the heat by construction. Reporting that in amber as "only
    0.05C of change" fires a warning on every correct build -- reported from
    the rig, on a heater that was working.

    A rise proves the heater works. No rise proves nothing, and must not be
    dressed up as a fault.
    """
    src = (WEB / "screens" / "SettingsScreen.jsx").read_text(encoding="utf-8")
    block = src.split("pulse.rise_c >= 0.3", 1)[1][:900]
    assert "text-amber" not in block, \
        "a no-rise result is styled as a warning; it fires on working hardware"
    assert "expected" in block, \
        "should say a flat reading is expected on a sealed dome, not suspicious"
