"""A server error must never be able to blank the page.

FastAPI has two error shapes. `raise HTTPException(500, "text")` gives
`detail` as a string. A *validation* failure gives `detail` as a list of
objects -- {type, loc, msg, input}. Put one of those into JSX and React throws
"Objects are not valid as a React child" mid-render, unmounts the tree, and the
screen goes black with the reason only in the browser console.

The dew heater's test button did exactly that: it POSTed no body to an endpoint
whose body was required, got a 422, and took the whole settings page down. The
one control whose entire purpose is to make commissioning a heater safe was the
control that broke the page.

Nine call sites had the same latent bug. They now go through errorText(), which
is unit-tested in web/src/lib/errors.test.mjs to always return a string.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api

WEB = Path(__file__).resolve().parents[1] / "web" / "src"

# `showToast(x)` / `setError(x)` put x straight into the DOM, so anything
# derived from a response body has to be flattened before it gets there.
#
# Optional chaining has to count. The first draft of this pattern required a
# literal dot before `detail`, which meant it matched `body.detail` but not
# `detail?.detail` -- four of the five real call sites. It passed, and it was
# decorative. Hence the historical fixtures asserted below.
RAW_DETAIL = re.compile(
    r"(?:showToast|setError)\s*\??\.?\(\s*[^)]*?\w+\s*\??\.\s*detail\b", re.S)

# Verbatim from the commits that shipped them. A guard nobody has watched fail
# is a guess.
HISTORICAL_BUGS = [
    "return showToast?.(detail?.detail || 'That did not work')",
    "return showToast?.(detail?.detail || 'Could not restart')",
    "if (!r.ok) showToast(body.detail ?? 'Could not start the update')",
    "return setError(detail?.detail || 'Could not save your settings.')",
    "showToast(body.detail ?? 'Render failed')",
]

REPAIRED = [
    "return showToast?.(errorText(detail))",
    "if (!r.ok) showToast(errorText(body, 'Could not start the update'))",
    "return setError(errorText(detail, 'Could not save your settings.'))",
    "showToast(errorText(body, 'Render failed'))",
    # Binding the body to a local is fine; it is rendering it that is not.
    "const detail = await r?.json().catch(() => null)",
    "const d = body.detail ?? {}",
]


@pytest.mark.parametrize("line", HISTORICAL_BUGS)
def test_the_guard_catches_every_call_site_that_actually_shipped(line):
    assert RAW_DETAIL.search(line), f"guard would have missed: {line}"


@pytest.mark.parametrize("line", REPAIRED)
def test_the_guard_does_not_cry_wolf(line):
    assert not RAW_DETAIL.search(line), f"false positive on: {line}"


@pytest.mark.parametrize("path", sorted(WEB.rglob("*.jsx")), ids=lambda p: p.name)
def test_no_screen_renders_a_response_detail_directly(path):
    src = path.read_text(encoding="utf-8")
    hits = [m.group(0).replace("\n", " ")[:90] for m in RAW_DETAIL.finditer(src)]
    assert not hits, (
        f"{path.name} renders a response `detail` without flattening it. On a "
        f"validation error that is a list of objects, and React blanks the "
        f"page. Use errorText() from lib/errors.js:\n  " + "\n  ".join(hits))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())
    return TestClient(api.app)


def test_the_heater_test_accepts_the_request_the_button_sends(client, monkeypatch):
    """The button posts no body. That has to mean "the default pulse", not 422.

    This is the actual regression: the endpoint declared a required body, the
    UI sent none, and the resulting validation error is what blanked the page.
    """
    from skylapse.daemon import dewheater
    monkeypatch.setattr(dewheater, "test_pulse",
                        lambda pin, seconds: {"ok": True, "seconds": seconds,
                                              "gpio_pin": pin})
    r = client.post("/api/dewheater/test")          # no body, exactly as the UI does
    assert r.status_code == 200, \
        f"the commissioning button gets {r.status_code}: {r.text[:200]}"
    assert r.json()["seconds"] == 60.0


def test_an_explicit_duration_is_still_honoured(client, monkeypatch):
    from skylapse.daemon import dewheater
    monkeypatch.setattr(dewheater, "test_pulse",
                        lambda pin, seconds: {"ok": True, "seconds": seconds})
    assert client.post("/api/dewheater/test",
                       json={"seconds": 2}).json()["seconds"] == 2


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_error_flattener_holds_its_invariant():
    """Runs the JS unit tests, whose central assertion is that errorText()
    returns a string for every shape the API can produce."""
    suite = WEB / "lib" / "errors.test.mjs"
    result = subprocess.run([NODE, str(suite)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
