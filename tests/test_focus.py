"""Sharpness score must rank focused above defocused, and the session must
track best-seen and direction — that's the entire promise of the feature."""
import cv2
import numpy as np

from skylapse.daemon.focus import FocusSession, sharpness


def star_field(blur_sigma: float) -> np.ndarray:
    """Synthetic stars, optionally defocused with a gaussian blur."""
    rng = np.random.default_rng(3)
    img = rng.normal(2000, 80, (400, 600)).astype(np.float32)
    for _ in range(60):
        y, x = rng.integers(20, 380), rng.integers(20, 580)
        img[y, x] += 40000
    if blur_sigma > 0:
        img = cv2.GaussianBlur(img, (0, 0), blur_sigma)
    return np.clip(img, 0, 65535).astype(np.uint16)


def test_sharp_scores_higher_than_blurred():
    focused = sharpness(star_field(0))
    slightly_off = sharpness(star_field(1.5))
    way_off = sharpness(star_field(4.0))
    assert focused > slightly_off > way_off        # monotonic with focus error


def test_session_tracks_best_and_trend():
    s = FocusSession()
    for score in (10.0, 14.0, 19.0, 25.0, 31.0):   # turning the right way
        info = s.update(score)
    assert info["best"] == 31.0
    assert info["trend"] == "improving"
    for score in (28.0, 22.0, 15.0, 11.0, 8.0):    # overshot the peak
        info = s.update(score)
    assert info["best"] == 31.0                    # best remembers the peak
    assert info["trend"] == "worsening"
    assert info["frames"] == 10
