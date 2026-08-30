"""The dashboard's dew heater banner.

The heater is the only thing on this camera that draws real power and makes
heat unattended, and on a damp night it can legitimately run from dusk to dawn:
the sensor is outside the dome and cannot see the heater's own effect, so the
heater never satisfies its own condition. It stops when the weather changes.

Which means "on for nine hours" is correct behaviour that looks exactly like
something stuck on. The dashboard has to make that legible without opening
Settings.
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = (Path(__file__).resolve().parents[1]
             / "web" / "src" / "screens" / "Dashboard.jsx")


def source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_the_banner_is_rendered_on_the_dashboard():
    src = source()
    assert "<DewHeaterBanner" in src, "the banner is defined but never rendered"
    assert "function DewHeaterBanner" in src


def test_the_banner_hides_itself_when_there_is_no_reading():
    """The daemon reports dew: null when the feature is off or no sensor has
    answered. A card reading "undefined°C" is worse than no card."""
    src = source()
    body = src.split("function DewHeaterBanner", 1)[1]
    assert re.search(r"if\s*\(!dew\)\s*return null", body), \
        "must return null when the daemon reports no dew reading"


def test_the_banner_distinguishes_heating_from_idle():
    body = source().split("function DewHeaterBanner", 1)[1]
    assert "dew.heating" in body
    assert "Heating" in body and "Idle" in body


def test_the_banner_explains_that_a_long_run_is_normal():
    """Without this, the honest answer to "why has it been on all night?" lives
    only in a chat log."""
    body = source().split("function DewHeaterBanner", 1)[1]
    assert "normal" in body.lower()
    assert "outside" in body.lower(), \
        "should say the sensor cannot see the heater's own effect"


def test_the_margin_is_shown_not_just_the_two_temperatures():
    """Margin is the number the control law actually acts on. Making the reader
    subtract two figures to find out how close they are is a small cruelty."""
    body = source().split("function DewHeaterBanner", 1)[1]
    assert "temp_c - dew.dewpoint_c" in body.replace("dew.temp_c", "temp_c")
    assert "Margin" in body
