"""Timelapse render + cleanup ordering (timelapses die last)."""
import subprocess
from pathlib import Path
from unittest import mock

import pytest

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


def rendering(night, frames):
    """Patch the two subprocess calls a render now makes.

    ffmpeg is faked into creating its output file, and the probe reports the
    frame count it was given — because render_night now refuses to call a
    render finished until it has counted the frames in the result.
    """
    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return (mock.patch.object(subprocess, "run", side_effect=fake_run),
            mock.patch.object(nightjobs, "_probe",
                              return_value={"nb_read_frames": str(frames),
                                            "width": "100", "height": "100"}))


def ffmpeg_argv(run):
    """The ffmpeg invocation out of a run mock that also saw ffprobe."""
    for call in run.call_args_list:
        if call[0][0] and call[0][0][0] == "ffmpeg":
            return call[0][0]
    raise AssertionError("ffmpeg was never invoked")


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
    runner, prober = rendering(night, 100)
    with runner as run, prober, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night, force=True)
    ffmpeg_argv(run)                              # existing mp4 deleted, re-rendered


def test_render_fps_scales_with_frame_count(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", frames=900)
    runner, prober = rendering(night, 900)
    with runner as run, prober, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify") as note:
        nightjobs.render_night(night)
        args = ffmpeg_argv(run)
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
    runner, prober = rendering(night, 900)
    with runner as run, prober, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night, TimelapseConfig(clip_seconds=60))
        args = ffmpeg_argv(run)
        assert int(args[args.index("-r") + 1]) == 15   # 900/60, not 900/30


def test_quality_setting_maps_to_crf(tmp_path):
    from skylapse.config import TimelapseConfig
    night = fake_night(tmp_path, "2026-08-13", frames=100)
    runner, prober = rendering(night, 100)
    with runner as run, prober, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night, TimelapseConfig(quality="max"))
        args = ffmpeg_argv(run)
        assert args[args.index("-crf") + 1] == "17"


# -- what the corrupt-timelapse investigation found --------------------------
#
# A 328-frame night rendered as a 17-frame, 1.4-second clip that reported
# success and notified a phone. Three separate faults lined up:
# one zero-byte JPEG, a concat demuxer that stops at the first input it cannot
# open while still exiting 0, and a render that trusted that exit code.

def test_a_zero_byte_frame_does_not_cost_the_rest_of_the_night(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", frames=100)
    (night / "img_20260813_000042.jpg").write_bytes(b"")

    assert len(nightjobs.usable_frames(night)) == 100, \
        "the empty frame was handed to ffmpeg, which stops dead at it"
    assert all(f.stat().st_size > 0 for f in nightjobs.usable_frames(night))


def test_the_render_uses_only_the_usable_frames(tmp_path):
    night = fake_night(tmp_path, "2026-08-13", frames=60)
    for i in range(5):
        (night / f"img_20260813_9000{i:02d}.jpg").write_bytes(b"")

    runner, prober = rendering(night, 60)
    with runner as run, prober, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night)
    listed = (night / ".frames.txt")
    assert not listed.exists()                    # cleaned up
    args = ffmpeg_argv(run)
    assert int(args[args.index("-r") + 1]) == 12  # 60 usable, not 65


# -- post-render validation --------------------------------------------------

def test_a_short_render_is_not_success(tmp_path):
    out = tmp_path / "t.mp4"
    out.write_bytes(b"mp4")
    with mock.patch.object(nightjobs, "_probe", return_value={"nb_read_frames": "17"}):
        assert "17 of 328" in nightjobs.validate_render(out, 328)


def test_a_complete_render_passes(tmp_path):
    out = tmp_path / "t.mp4"
    out.write_bytes(b"mp4")
    with mock.patch.object(nightjobs, "_probe", return_value={"nb_read_frames": "328"}):
        assert nightjobs.validate_render(out, 328) == ""


def test_a_missing_or_empty_output_is_not_success(tmp_path):
    assert nightjobs.validate_render(tmp_path / "nope.mp4", 10) == "no output file"
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert nightjobs.validate_render(empty, 10) == "no output file"


def test_an_unprobeable_output_is_not_success(tmp_path):
    out = tmp_path / "t.mp4"
    out.write_bytes(b"not actually video")
    with mock.patch.object(nightjobs, "_probe", return_value={}):
        assert nightjobs.validate_render(out, 10) != ""


def test_a_failed_render_never_notifies(tmp_path):
    """The notification is the whole reason nobody looked at the journal. A
    phone saying "timelapse ready" for a file that is not is worse than
    silence."""
    night = fake_night(tmp_path, "2026-08-13", frames=100)

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(subprocess, "run", side_effect=fake_run), \
         mock.patch.object(nightjobs, "_probe", return_value={"nb_read_frames": "17"}), \
         mock.patch("skylapse.daemon.nightjobs.notify.notify") as note:
        result = nightjobs.render_night(night)

    assert result is None
    note.assert_not_called()
    assert not list(night.glob("timelapse_*.mp4")), "left a bad file in place"


def test_a_failed_render_is_retried_once_smaller(tmp_path):
    """A render that dies at 12 MP is far likelier to survive at 4K than to
    survive being tried again identically."""
    night = fake_night(tmp_path, "2026-08-13", frames=100)
    sizes = []

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "ffmpeg":
            sizes.append(cmd[cmd.index("-vf") + 1])
            Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(subprocess, "run", side_effect=fake_run), \
         mock.patch.object(nightjobs, "_probe", return_value={"nb_read_frames": "1"}), \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night)

    assert len(sizes) == 2, f"expected one retry, got {len(sizes)} attempts"
    assert sizes[0] != sizes[1], "retried at the same size"


# -- output resolution -------------------------------------------------------

def test_the_default_budget_stays_inside_a_playable_level():
    """Measured on the rig: 3840x2878 encodes as h264 level 6.0, which no phone
    or browser hardware decoder will touch, while the same 4K *pixel budget* at
    3328x2494 comes out level 5.1 and plays. Level follows macroblock count, so
    a width alone decides nothing on a sensor that is not 16:9."""
    for w, h in ((4056, 3040), (3552, 3552), (4096, 2160)):
        ow, oh = nightjobs.output_size(w, h, "4k")
        assert ow * oh <= nightjobs.RESOLUTIONS["4k"], f"{w}x{h} -> {ow}x{oh}"


def test_the_aspect_ratio_survives():
    ow, oh = nightjobs.output_size(4056, 3040, "4k")
    assert abs((ow / oh) - (4056 / 3040)) < 0.01


@pytest.mark.parametrize("resolution", ["4k", "1080p", "full"])
def test_dimensions_are_always_even(resolution):
    """h264 with yuv420p cannot encode an odd edge; ffmpeg fails outright."""
    for w, h in ((4055, 3039), (3553, 3551), (101, 99)):
        ow, oh = nightjobs.output_size(w, h, resolution)
        assert ow % 2 == 0 and oh % 2 == 0, f"{w}x{h} -> {ow}x{oh}"


def test_small_sensors_are_never_upscaled():
    """A 640x480 camera is not improved by being stretched to fill 4K, and the
    file would be many times larger for no added detail."""
    assert nightjobs.output_size(640, 480, "4k") == (640, 480)


def test_full_means_native():
    assert nightjobs.output_size(4056, 3040, "full") == (4056, 3040)


def test_an_unknown_resolution_falls_back_to_the_safe_default():
    """A hand-edited config must not produce a file nothing can play."""
    assert nightjobs.output_size(4056, 3040, "nonsense") == \
        nightjobs.output_size(4056, 3040, "4k")


# -- stale renders -----------------------------------------------------------

def test_a_timelapse_older_than_the_night_is_re_rendered(tmp_path):
    """Folders roll at local noon but the render fires at dawn, so on the rig
    every clip was missing its morning frames — the 08-15 one was written at
    07:31 and 252 frames arrived afterwards."""
    import os

    night = fake_night(tmp_path, "2026-08-13", frames=100, with_mp4=True)
    mp4 = night / "timelapse_2026-08-13.mp4"
    os.utime(mp4, (1_000_000, 1_000_000))         # older than every frame

    runner, prober = rendering(night, 100)
    with runner as run, prober, \
         mock.patch("skylapse.daemon.nightjobs.notify.notify"):
        nightjobs.render_night(night)
    ffmpeg_argv(run)                              # it re-rendered


def test_a_current_timelapse_is_left_alone(tmp_path):
    """Idempotency still holds when nothing has arrived since."""
    import os

    night = fake_night(tmp_path, "2026-08-13", frames=100, with_mp4=True)
    mp4 = night / "timelapse_2026-08-13.mp4"
    os.utime(mp4, (2_000_000_000, 2_000_000_000))  # newer than every frame

    with mock.patch.object(subprocess, "run") as run:
        out = nightjobs.render_night(night)
    run.assert_not_called()
    assert out == mp4


# -- h264 level, which is set by macroblocks per second ----------------------

def test_a_long_night_at_4k_is_capped_to_a_playable_rate():
    """The 2026-08-17 night, exactly. 2205 frames over a 30-second target asks
    for 74 fps; clamped to 60 it produced a level 5.2 file, which a phone may
    refuse. The resolution budget had done its job — 3326x2492 is inside level
    5.1's per-frame ceiling — and the frame RATE took it over anyway.
    """
    assert nightjobs.max_playable_fps(3326, 2492) == 30


def test_small_output_is_not_slowed_down_for_nothing():
    """1080p has macroblocks to spare, so the cap must not touch it."""
    assert nightjobs.max_playable_fps(1920, 1440) == nightjobs.FPS_MAX


def test_native_resolution_is_capped_hardest():
    """Full sensor is the case the UI already warns about; it gets the lowest
    rate rather than an unplayable file."""
    assert nightjobs.max_playable_fps(4056, 3040) < 30


def test_an_unmeasurable_frame_does_not_stall_the_render():
    """Falls back to the ordinary ceiling rather than to zero fps, which would
    be a failed render for a night that is otherwise fine."""
    assert nightjobs.max_playable_fps(0, 0) == nightjobs.FPS_MAX


def test_the_cap_never_goes_below_the_floor():
    assert nightjobs.max_playable_fps(20000, 20000) == nightjobs.FPS_MIN


# -- clip length, which is what the setting actually promises -----------------

def test_a_long_night_is_sampled_to_the_target_length():
    """The setting said 30 seconds and the night came out 73. Duration is
    frames over fps, and with the rate pinned by h264 level the only variable
    left is how many frames go in."""
    fps = nightjobs.max_playable_fps(3326, 2492)          # 30, for a 4K-budget frame
    shown = nightjobs.select_frames(list(range(2204)), 30, fps)
    assert len(shown) / fps == 30.0


def test_sampling_spans_the_whole_night():
    """Evenly spaced, not the first N — a clip of the first twenty minutes is
    not a timelapse of the night."""
    shown = nightjobs.select_frames(list(range(2204)), 30, 30)
    assert shown[0] == 0
    assert shown[-1] > 2100
    gaps = {b - a for a, b in zip(shown, shown[1:])}
    assert max(gaps) - min(gaps) <= 1, "spacing should be even to within a frame"


class _Timed:
    """A frame that knows when it was taken, which is what sampling needs."""

    def __init__(self, when):
        self.when = when

    def stat(self):
        return type("S", (), {"st_mtime": self.when})()


def test_sampling_is_even_in_time_not_in_frame_number():
    """What "jumpy" meant. Frames do not arrive at a constant rate — exposure
    changes through the night and gaps appear where the sensor was settling — so
    every Nth frame makes the sky crawl through the dense stretches and leap
    across the sparse ones. Measured on a real night, that was 91 visible
    lurches; sampling on a clock instead left one.
    """
    dense = [_Timed(t) for t in range(0, 3000, 30)]          # a frame every 30s
    sparse = [_Timed(t) for t in range(9000, 12000, 300)]    # one every 5 min
    shown = nightjobs.select_frames(dense + sparse, 4, 10)

    steps = [b.when - a.when for a, b in zip(shown, shown[1:])]
    moving = [s for s in steps if s > 0]
    lurches = [s for s in moving if s > sorted(moving)[len(moving) // 2] * 3]
    assert len(lurches) <= 1, f"{len(lurches)} lurches in the timeline"


def test_a_gap_becomes_a_hold_rather_than_a_leap():
    """There is no frame to show for time nobody captured. Holding the last one
    is honest and brief; leaping is what reads as broken."""
    before = [_Timed(t) for t in range(0, 600, 30)]
    after = [_Timed(t) for t in range(3000, 3600, 30)]
    shown = nightjobs.select_frames(before + after, 3, 10)
    assert any(a is b for a, b in zip(shown, shown[1:])), "no hold across the gap"


def test_a_short_night_keeps_every_frame():
    """Nothing to sample. Padding it out would mean duplicating frames to reach
    a length nobody would notice."""
    frames = list(range(400))
    assert nightjobs.select_frames(frames, 30, 30) == frames


def test_selection_never_returns_more_than_it_was_given():
    assert len(nightjobs.select_frames(list(range(10)), 600, 60)) == 10


def test_no_frame_index_runs_off_the_end():
    for n in (13, 101, 999, 2204, 5000):
        shown = nightjobs.select_frames(list(range(n)), 30, 30)
        assert max(shown) < n


# -- explaining the render before it happens ---------------------------------

def test_jpeg_size_reads_the_header_without_decoding(tmp_path):
    import cv2
    import numpy as np
    path = tmp_path / "img_0001.jpg"
    cv2.imwrite(str(path), np.zeros((3040, 4056, 3), dtype=np.uint8))
    assert nightjobs.jpeg_size(path) == (4056, 3040)


def test_jpeg_size_survives_a_file_that_is_not_one(tmp_path):
    junk = tmp_path / "img_0001.jpg"
    junk.write_bytes(b"not a jpeg at all")
    assert nightjobs.jpeg_size(junk) == (0, 0)


def _night(tmp_path, count, w=4056, h=3040):
    import cv2
    import numpy as np
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    folder = tmp_path / "2026-08-17"
    folder.mkdir()
    for i in range(count):
        cv2.imwrite(str(folder / f"img_{i:05d}.jpg"), frame)
    return folder


def test_the_plan_matches_what_the_renderer_would_do(tmp_path):
    """It has to be the same arithmetic, or the label becomes a second source of
    truth that drifts from the thing it describes."""
    from skylapse.config import TimelapseConfig
    folder = _night(tmp_path, 300)
    settings = TimelapseConfig(clip_seconds=5)
    plan = nightjobs.plan_render(folder, settings)

    frames = nightjobs.usable_frames(folder)
    out_w, out_h = nightjobs.output_size(4056, 3040, settings.resolution)
    fps = min(max(nightjobs.FPS_MIN,
                  min(nightjobs.FPS_MAX, round(len(frames) / 5))),
              nightjobs.max_playable_fps(out_w, out_h))
    assert plan["fps"] == fps
    assert plan["used"] == len(nightjobs.select_frames(frames, 5, fps))
    assert plan["seconds"] == round(plan["used"] / fps, 1)


def test_the_plan_says_when_frames_are_being_left_out(tmp_path):
    from skylapse.config import TimelapseConfig
    plan = nightjobs.plan_render(_night(tmp_path, 600),
                                 TimelapseConfig(clip_seconds=5))
    assert plan["used"] < plan["frames"]
    assert plan["every_nth"] > 1


def test_an_empty_night_plans_nothing_rather_than_dividing_by_zero(tmp_path):
    folder = tmp_path / "2026-08-17"
    folder.mkdir()
    plan = nightjobs.plan_render(folder)
    assert plan["frames"] == 0 and plan["used"] == 0 and plan["seconds"] == 0.0


# -- a render in progress is not a timelapse ---------------------------------

def test_the_finished_name_only_exists_when_it_is_finished(tmp_path, monkeypatch):
    """An mp4's index is written last, so a render in progress is a large file
    no player can open. It was written straight to the final name, so
    everything downstream believed it was ready: the night's page showed a
    blank player reading 0:00 while ffmpeg was still working."""
    import cv2
    import numpy as np
    folder = tmp_path / "2026-08-20"
    folder.mkdir()
    for i in range(30):
        cv2.imwrite(str(folder / f"img_{i:05d}.jpg"),
                    np.zeros((80, 120, 3), dtype=np.uint8))

    final = folder / "timelapse_2026-08-20.mp4"
    seen = {}

    def fake_ffmpeg(folder_, frames, out, fps, crf, scale):
        # Mid-render: whatever exists must not be the published name.
        out.write_bytes(b"partial data with no index")
        seen["wrote_to"] = out
        seen["final_existed_during_render"] = final.exists()
        return ""

    monkeypatch.setattr(nightjobs, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(nightjobs, "validate_render", lambda p, n: "")
    monkeypatch.setattr(nightjobs.notify, "notify", lambda *a, **k: True)

    nightjobs.render_night(folder)
    assert seen["wrote_to"] == nightjobs.partial_path(final)
    assert seen["final_existed_during_render"] is False
    assert final.exists(), "never published"
    assert not nightjobs.partial_path(final).exists(), "left its scratch file"


def test_a_failed_render_leaves_nothing_behind(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    folder = tmp_path / "2026-08-20"
    folder.mkdir()
    for i in range(30):
        cv2.imwrite(str(folder / f"img_{i:05d}.jpg"),
                    np.zeros((80, 120, 3), dtype=np.uint8))

    monkeypatch.setattr(nightjobs, "_run_ffmpeg",
                        lambda *a, **k: "ffmpeg exited 1")
    assert nightjobs.render_night(folder) is None
    assert not list(folder.glob("timelapse_*"))


# -- day and night are separate films ----------------------------------------

def test_day_and_night_frames_are_kept_apart(tmp_path, monkeypatch):
    """A night folder runs noon to noon, so it holds the evening before dusk and
    the morning after dawn — shot at a fiftieth of a second at unity gain
    against twenty-five seconds at gain fifteen. Cut together, the picture
    changes completely at each end, which was the first thing anyone said about
    watching it."""
    import os
    from datetime import datetime, timezone
    from skylapse import config

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    cfg = config.Config()
    cfg.location.latitude, cfg.location.longitude = 42.56, -87.88   # Racine
    config.save(cfg)

    folder = tmp_path / "2026-08-20"
    folder.mkdir()
    # Local noon and local midnight, expressed as instants.
    noon = datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc).timestamp()
    midnight = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc).timestamp()
    made = {}
    for label, when in (("day", noon), ("night", midnight)):
        for i in range(3):
            f = folder / f"img_{label}_{i}.jpg"
            f.write_bytes(b"x")
            os.utime(f, (when + i, when + i))
            made.setdefault(label, []).append(f)

    groups = nightjobs.classify_frames(
        sorted(folder.glob("img_*.jpg")), cfg)
    assert {f.name for f in groups["day"]} == {f.name for f in made["day"]}
    assert {f.name for f in groups["night"]} == {f.name for f in made["night"]}


def test_the_night_film_keeps_the_plain_name():
    """It is the one anybody came for, and every existing night on every card
    is already called this."""
    folder = Path("/images/cam/2026-08-20")
    assert nightjobs.output_name(folder, "night").name == "timelapse_2026-08-20.mp4"
    assert nightjobs.output_name(folder, "day").name == "timelapse_2026-08-20_day.mp4"
