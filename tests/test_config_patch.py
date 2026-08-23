"""PUT /api/config must not delete cameras it was not asked about.

Found the hard way: setting one white balance multiplier on one camera with a
one-line patch removed the other camera's registry entry entirely, taking its
profiles with it. model_copy(update=) replaces a key wholesale, and `cameras`
is a registry rather than a value.

The settings screen resends the whole registry with every edit, so the UI never
saw this. That is a workaround for a sharp edge, not a design, and nothing else
touching the endpoint inherits it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skylapse import config
from skylapse.api import main as api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "run")
    cfg = config.Config()
    cfg.camera("zwo-asi676mc").label = "ZWO ASI676MC"
    cfg.camera("picam-imx477").label = "Pi Camera"
    config.save(cfg)
    return TestClient(api.app)


def test_patching_one_camera_leaves_the_others_alone(client):
    client.put("/api/config", json={"cameras": {"picam-imx477": {"wb_r": 1.78}}})
    cameras = config.load().cameras
    assert set(cameras) == {"zwo-asi676mc", "picam-imx477"}, \
        "a one-camera patch deleted the other camera"
    assert cameras["zwo-asi676mc"].label == "ZWO ASI676MC"


def test_the_patched_camera_keeps_its_untouched_fields(client):
    client.put("/api/config", json={"cameras": {"picam-imx477": {"wb_r": 1.78}}})
    entry = config.load().cameras["picam-imx477"]
    assert entry.wb_r == 1.78
    assert entry.label == "Pi Camera", "a field nobody mentioned was reset"


def test_tuning_one_camera_never_moves_another_camera_colour(client):
    """Per-camera isolation, at the API. The multipliers describe a sensor and
    its lens; leaking one camera's onto another would be worse than having no
    white balance at all."""
    client.put("/api/config", json={"cameras": {"picam-imx477":
                                                {"wb_r": 1.78, "wb_b": 1.111}}})
    cameras = config.load().cameras
    assert (cameras["zwo-asi676mc"].wb_r, cameras["zwo-asi676mc"].wb_b) == (1.0, 1.0)
    assert (cameras["picam-imx477"].wb_r, cameras["picam-imx477"].wb_b) == (1.78, 1.111)


def test_a_new_camera_can_still_be_added_by_patch(client):
    client.put("/api/config", json={"cameras": {"picam-imx708": {"label": "New"}}})
    cameras = config.load().cameras
    assert set(cameras) == {"zwo-asi676mc", "picam-imx477", "picam-imx708"}


def test_other_top_level_keys_still_replace(client):
    """Only the registry merges. A list or scalar that merged instead of
    replacing could never be shortened or cleared."""
    client.put("/api/config", json={"jpeg_quality": 80})
    assert config.load().jpeg_quality == 80
