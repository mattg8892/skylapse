"""Saving config must not change who can read it.

This is not a hypothetical. netwatch writes the config as root (it has to —
it drives NetworkManager), and mkstemp creates 0600 owned by the writer. On the
rig, one expiring hotspot session turned /etc/skylapse/config.yaml into a
root-only file; the api and daemon run as an ordinary user, so every page
returned 500 and capture stopped.
"""
from __future__ import annotations

import os
import stat

import pytest

from skylapse import config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_an_existing_files_mode_survives_a_save(cfg_path):
    config.save(config.Config())
    os.chmod(cfg_path, 0o644)

    cfg = config.load()
    cfg.network.mode = "standalone"
    config.save(cfg)

    assert _mode(cfg_path) == 0o644, \
        "the save narrowed the file's permissions out from under its readers"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_new_file_is_readable_by_the_services_that_need_it(cfg_path):
    config.save(config.Config())
    assert _mode(cfg_path) & stat.S_IROTH, "created unreadable to the services"


def test_the_contents_still_round_trip(cfg_path):
    cfg = config.Config()
    cfg.network.mode = "standalone"
    cfg.network.hotspot_until = 12345.0
    config.save(cfg)

    loaded = config.load()
    assert loaded.network.mode == "standalone"
    assert loaded.network.hotspot_until == 12345.0


def test_a_failed_write_leaves_no_temp_files_behind(cfg_path, monkeypatch):
    config.save(config.Config())
    monkeypatch.setattr(config.yaml, "safe_dump",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        config.save(config.Config())
    assert not list(cfg_path.parent.glob("*.tmp"))
