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


# -- the shared runtime directory -------------------------------------------
#
# /run/skylapse is written by three services and two of them do not run as
# root. systemd re-applies RuntimeDirectory ownership *recursively* every time
# a unit using it starts, so netwatch starting rechowned files the daemon had
# written seconds earlier. Measured at the first cold boot after netwatch was
# enabled: the daemon died with EACCES on its own status file 15s in.

@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_a_status_file_can_be_replaced_by_someone_who_does_not_own_it(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    config.write_run_file("daemon.json", '{"a": 1}')

    # Stand in for the rechown: a file this process may not write to, in a
    # directory it may.
    os.chmod(tmp_path / "daemon.json", 0o444)

    config.write_run_file("daemon.json", '{"a": 2}')
    assert (tmp_path / "daemon.json").read_text() == '{"a": 2}'


def test_status_files_are_readable_by_the_other_services(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    config.write_run_file("netwatch.json", "{}")
    if os.name != "nt":
        assert _mode(tmp_path / "netwatch.json") & stat.S_IROTH, \
            "mkstemp's 0600 would leave this unreadable by the api"


def test_a_reader_never_sees_a_half_written_file(tmp_path, monkeypatch):
    """The replace is what guarantees it: readers see the old file or the new
    one, never a truncated one. json.JSONDecodeError in a status poll is not a
    theoretical concern at a 5s cadence."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path)
    config.write_run_file("daemon.json", '{"frames": 1}')

    import json
    seen = []
    for payload in ('{"frames": 2}', '{"frames": 3}'):
        config.write_run_file("daemon.json", payload)
        seen.append(json.loads((tmp_path / "daemon.json").read_text()))
    assert [s["frames"] for s in seen] == [2, 3]
    assert not list(tmp_path.glob("*.tmp")), "left temp files in the runtime dir"
