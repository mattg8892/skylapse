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
