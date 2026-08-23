"""DNG writer.

These guard the pidng call sequence, which is version-sensitive. pidng 4.x
takes the tags through options() and its convert() accepts only
(image, filename); passing tags= to convert() raises TypeError at runtime.
That is exactly what shipped, and because the keeper logged success
unconditionally, every DNG write on the rig failed silently. A test that
merely mocked pidng would have stayed green through all of it — so these
call the real encoder and inspect the bytes on disk.
"""
from __future__ import annotations

import numpy as np
import pytest

from skylapse.daemon.drivers.base import BayerPattern, Frame
from skylapse.daemon.pipeline import process

# pidng is source-only (no wheels for any platform) and needs a C toolchain,
# so it is absent on the Windows dev box and present on the Pi. Skipping beats
# a mock that cannot catch the very bug this file is about.
pytest.importorskip("pidng", reason="pidng not installed (needs a C compiler)")

TIFF_MAGIC = (b"II*\x00", b"MM\x00*")     # a DNG is a TIFF container


def _frame(width: int = 64, height: int = 64,
           bayer: BayerPattern = BayerPattern.RGGB) -> Frame:
    rng = np.random.default_rng(0)
    data = rng.integers(0, 65535, size=(height, width), dtype=np.uint16)
    return Frame(data=data.tobytes(), width=width, height=height, bayer=bayer,
                 bit_depth=16, exposure_us=1_000_000, gain=100,
                 timestamp=1_700_000_000.0, sensor_temp_c=12.5)


def test_write_dng_creates_a_file(tmp_path):
    """The original failure mode: writer raises, caller reports success."""
    out = process.write_dng(_frame(), tmp_path / "img_test.dng")
    assert out.exists(), "write_dng returned a path it never wrote"


def test_write_dng_is_a_tiff_container(tmp_path):
    out = process.write_dng(_frame(), tmp_path / "img_test.dng")
    header = out.read_bytes()[:4]
    assert header in TIFF_MAGIC, f"not a TIFF/DNG container: {header!r}"


def test_write_dng_contains_the_full_payload(tmp_path):
    """16-bit samples are stored unpacked, so the file cannot be smaller than
    the raw pixel payload — catches a writer that emits headers only."""
    out = process.write_dng(_frame(128, 96), tmp_path / "img_big.dng")
    assert out.stat().st_size >= 128 * 96 * 2


def test_write_dng_mono_skips_cfa_tags(tmp_path):
    """MONO takes a different tag branch; it must not break the call sequence."""
    out = process.write_dng(_frame(bayer=BayerPattern.MONO), tmp_path / "m.dng")
    assert out.read_bytes()[:4] in TIFF_MAGIC


def test_write_dng_appends_suffix_once(tmp_path):
    """pidng appends .dng itself; we must not end up with img.dng.dng."""
    out = process.write_dng(_frame(), tmp_path / "img_test.dng")
    assert out.name == "img_test.dng"
    assert not (tmp_path / "img_test.dng.dng").exists()


# -- AsShotNeutral -----------------------------------------------------------
#
# The multipliers have to reach every DNG, not just the ones written by the
# scheduled-RAW path. Missed on the rig: the keeper button wrote 1.0/1.0/1.0
# into files from a camera whose white balance had been set for ten minutes,
# and nothing anywhere reported a problem. This is the same failure shape the
# module docstring above already warns about — a DNG write that looks like it
# worked.

def _as_shot_neutral(path):
    """Read the tag straight out of the file, rather than trusting the writer."""
    import struct

    raw = path.read_bytes()
    bo = "<" if raw[:2] == b"II" else ">"
    off = struct.unpack(bo + "I", raw[4:8])[0]
    for i in range(struct.unpack(bo + "H", raw[off:off + 2])[0]):
        e = off + 2 + i * 12
        tag, _, count = struct.unpack(bo + "HHI", raw[e:e + 8])
        if tag == 50728:            # AsShotNeutral
            vo = struct.unpack(bo + "I", raw[e + 8:e + 12])[0]
            v = struct.unpack(bo + "%dI" % (count * 2), raw[vo:vo + count * 8])
            return [v[j] / v[j + 1] for j in range(0, len(v), 2)]
    return None


def test_the_multipliers_reach_the_file(tmp_path):
    """DNG wants the neutral in camera space, which is the reciprocal of the
    multipliers with green normalised to 1."""
    out = process.write_dng(_frame(), tmp_path / "img_wb.dng", wb=(1.78, 1.111))
    neutral = _as_shot_neutral(out)
    assert neutral is not None, "AsShotNeutral missing"
    assert neutral[0] == pytest.approx(1 / 1.78, abs=0.001)
    assert neutral[1] == 1.0
    assert neutral[2] == pytest.approx(1 / 1.111, abs=0.001)


def test_a_neutral_camera_still_writes_the_tag(tmp_path):
    """Absent, a raw editor applies its own guess; present and neutral, it
    knows the data is untouched."""
    out = process.write_dng(_frame(), tmp_path / "img_neutral.dng")
    assert _as_shot_neutral(out) == [1.0, 1.0, 1.0]


def test_the_pixel_data_is_not_touched_by_the_multipliers(tmp_path):
    """The whole point of shipping DNG. Colour rides along as metadata; the
    sensor data must come back bit for bit whatever the multipliers say."""
    plain = process.write_dng(_frame(), tmp_path / "a.dng").read_bytes()
    tinted = process.write_dng(_frame(), tmp_path / "b.dng", wb=(2.5, 0.6)).read_bytes()
    assert len(plain) == len(tinted)
    # Everything after the header/IFD is the strip: identical either way.
    assert plain[-4096:] == tinted[-4096:]


def test_a_mono_frame_gets_no_neutral(tmp_path):
    """There is no red or blue to balance, and a colour tag on a mono file is
    a lie a raw editor may act on."""
    out = process.write_dng(_frame(bayer=BayerPattern.MONO), tmp_path / "m2.dng",
                            wb=(1.78, 1.111))
    assert _as_shot_neutral(out) is None
