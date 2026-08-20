"""Every component the UI renders must actually exist.

Vite compiles JSX without resolving component identifiers, so `<Foo />` where
Foo does not exist builds perfectly and then throws at render time — a blank
black screen with the reason only in the browser console.

That is not hypothetical. Reorganising the settings screen spliced out three
components along with the block they sat in; the build passed, the tests passed,
the release went out, and the settings page was blank on the camera.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "src"

# Lowercase names are HTML elements; these are the DOM tags React knows.
COMPONENT = re.compile(r"<([A-Z][A-Za-z0-9_]*)[\s/>]")


def defined_names(src: str) -> set[str]:
    names = set(re.findall(r"function ([A-Z][A-Za-z0-9_]*)", src))
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Z][A-Za-z0-9_]*)\s*=", src))
    for imported in re.findall(r"import\s+(.+?)\s+from", src, re.S):
        names |= set(re.findall(r"[A-Z][A-Za-z0-9_]*", imported))
    return names


@pytest.mark.parametrize("path", sorted(WEB.rglob("*.jsx")), ids=lambda p: p.name)
def test_every_component_used_is_defined(path):
    src = path.read_text(encoding="utf-8")
    missing = sorted(set(COMPONENT.findall(src)) - defined_names(src))
    assert not missing, (
        f"{path.name} renders {missing} but never defines or imports them — "
        f"this builds cleanly and shows a blank screen")


# -- serving the frontend ----------------------------------------------------

def test_index_html_is_never_cached(tmp_path, monkeypatch):
    """The one file that must always be fresh.

    Vite fingerprints every asset, so those can be cached forever. index.html
    is what says which fingerprints are current — and a stale one names a
    bundle that has been deleted, which the camera answers with a 404 and the
    browser renders as a black screen. Seen exactly that way after an update,
    with the working build already installed and serving.
    """
    from fastapi.testclient import TestClient
    from skylapse import config
    from skylapse.api import main as api

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    config.save(config.Config())

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><script src=/assets/x.js>")
    (dist / "assets" / "x.js").write_text("console.log(1)")

    app = api.FastAPI()
    app.mount("/", api._Frontend(directory=dist, html=True), name="web")
    client = TestClient(app)

    index = client.get("/")
    assert "no-cache" in index.headers.get("cache-control", "")
    # Fingerprinted assets are free to cache; only the index must revalidate.
    asset = client.get("/assets/x.js")
    assert "no-cache" not in asset.headers.get("cache-control", "")
