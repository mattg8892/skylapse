"""Timelapse render + cleanup ordering (timelapses die last)."""
import subprocess
from pathlib import Path
from unittest import mock

from skylapse.daemon import nightjobs


def fake_night(tmp_path, name, frames=20, with_mp4=False):
    night = tmp_path / name
    night.mkdir(parents=True)
    for i in range(frames):
        (night / f"img_2026{name[-4:].replace('-','')}_{i:06d}.jpg").write_bytes(b"j" * 100)
        (night / f"img_2026{name[-4:].replace('-','')}_{i:06d}.json").write_text("{}")
    if with_mp4:
        (night / f"timelapse_{name}.mp4").write_bytes(b"m" * 100)
    return night


def test_render_skips_tiny_folders(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", frames=5)
    assert nightjobs.render_night(night) is None


def test_render_is_idempotent(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", with_mp4=True)
    with mock.patch.object(subprocess, "run") as run:
        out = nightjobs.render_night(night)
    run.assert_not_called()                       # existing mp4 -> no re-render
    assert out.name == "timelapse_2026-08-13.mp4"


def test_force_rerenders_existing_mp4(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", frames=100, with_mp4=True)
    with mock.patch.object(subprocess, "run") as run, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night, force=True)
    run.assert_called_once()                      # existing mp4 deleted, re-rendered


def test_render_fps_scales_with_frame_count(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", frames=900)
    with mock.patch.object(subprocess, "run") as run, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify") as note:
        nightjobs.render_night(night)
        args = run.call_args[0][0]
        fps = int(args[args.index("-r") + 1])
        assert fps == 30                          # 900 frames / 30s target
        note.assert_called_once()                 # fires timelapse_ready


def test_cleanup_deletes_frames_before_timelapses(tmp_path):
    old = fake_night(tmp_path, "2026-08-10", with_mp4=True)
    fake_night(tmp_path, "2026-08-13")            # newest night: untouchable
    # Free space "recovers" after frame deletion but before mp4 deletion.
    free_values = iter([0.5, 5.0, 5.0, 5.0])
    with mock.patch.object(nightjobs, "_free_gb", side_effect=lambda p: next(free_values)):
        nightjobs.cleanup(tmp_path, free_gb_floor=2.0)
    assert not list(old.glob("img_*"))            # frames gone
    assert list(old.glob("timelapse_*.mp4"))      # keepsake survives


def test_cleanup_takes_mp4_only_when_still_tight(tmp_path):
    old = fake_night(tmp_path, "2026-08-10", with_mp4=True)
    fake_night(tmp_path, "2026-08-13")
    with mock.patch.object(nightjobs, "_free_gb", return_value=0.5):
        nightjobs.cleanup(tmp_path, free_gb_floor=2.0)
    assert not old.exists()                       # fully removed, dir pruned


def test_cleanup_never_touches_newest_night(tmp_path):
    newest = fake_night(tmp_path, "2026-08-13")
    with mock.patch.object(nightjobs, "_free_gb", return_value=0.1):
        nightjobs.cleanup(tmp_path, free_gb_floor=2.0)
    assert len(list(newest.glob("img_*.jpg"))) == 20


def test_clip_seconds_setting_changes_fps(tmp_path):
    from skylapse.config import TimelapseConfig
    night = fake_night(tmp_path, "2026-08-13", frames=900)
    with mock.patch.object(subprocess, "run") as run, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night, TimelapseConfig(clip_seconds=60))
        args = run.call_args[0][0]
        assert int(args[args.index("-r") + 1]) == 15   # 900/60, not 900/30


def test_quality_setting_maps_to_crf(tmp_path):
    from skylapse.config import TimelapseConfig
    night = fake_night(tmp_path, "2026-08-13", frames=100)
    with mock.patch.object(subprocess, "run") as run, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night, TimelapseConfig(quality="max"))
        args = run.call_args[0][0]
        assert args[args.index("-crf") + 1] == "17"
