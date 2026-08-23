"""Pi camera driver.

The parts worth pinning are the ones measured on hardware and impossible to
infer: which format the stream actually is, the row padding, and where the
control limits come from. picamera2 is stubbed so these run anywhere.
"""
from __future__ import annotations

import numpy as np
import pytest

from skylapse.daemon.drivers.base import BayerPattern, CameraError
from skylapse.daemon.drivers.picam import PiCamDriver, bayer_from_stream_format

WIDTH, HEIGHT = 4056, 3040
STRIDE = 8128                      # 4064 uint16 per row: 8 pixels of padding


@pytest.mark.parametrize("fmt,expected", [
    ("SBGGR16", BayerPattern.BGGR),      # what an IMX477 actually delivers
    ("SRGGB16", BayerPattern.RGGB),
    ("SGRBG12", BayerPattern.GRBG),
    ("SGBRG10", BayerPattern.GBRG),
    ("BGGR_PISP_COMP1", BayerPattern.BGGR),
])
def test_bayer_read_from_the_stream_format(fmt, expected):
    assert bayer_from_stream_format(fmt) is expected


def test_unknown_stream_format_is_an_error():
    """Better to fail loudly than to debayer with a guessed order."""
    with pytest.raises(CameraError, match="Unrecognised raw stream"):
        bayer_from_stream_format("YUV420")


class FakeRequest:
    """Stands in for picamera2's CompletedRequest."""

    def __init__(self, meta, released):
        self._meta = meta
        self._released = released

    def get_metadata(self):
        return self._meta

    def make_array(self, name):
        frame = np.zeros((HEIGHT, STRIDE // 2), dtype=np.uint16)
        frame[:, :WIDTH] = 4095 << 4          # 12-bit sample, left-shifted
        frame[:, WIDTH:] = 0xDEAD             # padding must never be kept
        return frame.view(np.uint8)

    def release(self):
        self._released.append(self)


class FakePicamera2:
    """Mimics the picamera2 surface the driver touches, with the geometry and
    limits measured from the real IMX477."""

    sensor_resolution = (WIDTH, HEIGHT)
    camera_properties = {"Model": "imx477"}

    def __init__(self):
        self.controls_set = {}
        self.started = False
        self._configured = None
        self.requested = {"ExposureTime": 100_000, "AnalogueGain": 1.0}
        self.applied = dict(self.requested)
        self.settle_lag = 0        # frames a control change takes to appear
        self.pending_lag = 0
        self.requests_made = 0
        self.released = []

    def create_still_configuration(self, raw=None, buffer_count=2):
        return {"raw": dict(raw or {})}

    def configure(self, config):
        # libcamera answers with what it can deliver, not what was requested:
        # an unpacked SRGGB12 request comes back as SBGGR16 with a padded row.
        self._configured = {"raw": {"format": "SBGGR16",
                                    "size": (WIDTH, HEIGHT), "stride": STRIDE}}

    def camera_configuration(self):
        return self._configured

    def start(self):
        self.started = True

    @property
    def camera_controls(self):
        # Post-configure values: the pre-configure ceiling is 66ms.
        return {"ExposureTime": (110, 694_422_939, 20_000),
                "AnalogueGain": (1.0, 22.3, 1.0)}

    def set_controls(self, controls):
        self.controls_set.update(controls)
        for key in ("ExposureTime", "AnalogueGain"):
            if key in controls:
                self.requested[key] = controls[key]
        self.pending_lag = self.settle_lag

    def capture_request(self):
        """A request whose metadata lags the controls, as the real pipeline does.

        Measured on the IMX477: a control change shows up seven frames later,
        and the queue keeps serving frames exposed at the old settings until
        then. `settle_lag` reproduces that.
        """
        self.requests_made += 1
        if self.pending_lag > 0:
            self.pending_lag -= 1
            meta = dict(self.applied)          # still the OLD exposure
        else:
            self.applied = dict(self.requested)
            meta = dict(self.applied)
        meta["SensorTemperature"] = 41.5
        return FakeRequest(meta, self.released)

    def capture_metadata(self):
        return {"SensorTemperature": 41.5}

    def stop(self):
        self.started = False

    def close(self):
        pass


@pytest.fixture()
def driver(monkeypatch):
    import skylapse.daemon.drivers.picam as picam_mod
    fake_module = type("M", (), {"Picamera2": FakePicamera2})
    monkeypatch.setitem(__import__("sys").modules, "picamera2", fake_module)
    d = PiCamDriver()
    d.open()
    return d


def test_limits_come_from_the_sensor_not_a_hardcoded_guess(driver):
    """The stub claimed 200s and gain 22 regardless of what was attached."""
    info = driver._info
    assert info.max_exposure_us == 694_422_939
    assert info.min_exposure_us == 110
    assert info.max_gain == 22


def test_bayer_follows_the_delivered_stream(driver):
    """Sensor says SRGGB12; the stream is SBGGR16. Trust the stream."""
    assert driver._info.bayer is BayerPattern.BGGR


def test_capture_crops_the_row_padding(driver):
    frame = driver.capture()
    assert frame.width == WIDTH
    assert len(frame.data) == WIDTH * HEIGHT * 2, "padding kept, image would shear"
    arr = np.frombuffer(frame.data, dtype=np.uint16).reshape(HEIGHT, WIDTH)
    assert not (arr == 0xDEAD).any(), "padding bytes leaked into the frame"
    assert arr.max() == 4095 << 4


def test_exposure_is_clamped_to_the_sensor_limits(driver):
    driver.set_controls(999_999_999_999, 5)
    assert driver._picam.controls_set["ExposureTime"] == 694_422_939
    driver.set_controls(1, 5)
    assert driver._picam.controls_set["ExposureTime"] == 110


def test_frame_duration_covers_the_exposure(driver):
    """Without this libcamera clamps a 30s request down to the frame rate."""
    driver.set_controls(30_000_000, 2)
    lo, hi = driver._picam.controls_set["FrameDurationLimits"]
    assert lo >= 30_000_000 and hi >= 30_000_000


def test_gain_is_clamped_to_the_analogue_range(driver):
    """AE works in the ZWO's integer scale and will ask for far more than this
    sensor's 22x ceiling."""
    driver.set_controls(100_000, 300)
    assert driver._picam.controls_set["AnalogueGain"] == 22.0


def test_autoexposure_is_disabled(driver):
    """Skylapse owns exposure; libcamera's AE would fight the capture loop."""
    driver.set_controls(100_000, 4)
    assert driver._picam.controls_set["AeEnable"] is False


def test_stale_frames_are_discarded_after_a_control_change(driver):
    """The bug this exists for: libcamera applies a control change several
    frames late while the queue keeps serving the old exposure, so 100ms, 500ms
    and 2s captures all came back byte-for-byte identical."""
    driver._picam.settle_lag = 6
    driver.set_controls(2_000_000, 4)
    frame = driver.capture()
    assert frame.exposure_us == 2_000_000, "recorded a stale frame under the new exposure"
    # 6 stale frames pulled and discarded, then the good one (also released).
    assert driver._picam.requests_made == 7, "did not discard the stale frames"
    assert len(driver._picam.released) == 7, "a request was leaked"


def test_steady_state_costs_nothing(driver):
    """No control change means no discards — a manual-exposure night must not
    pay six frames per capture."""
    driver._picam.settle_lag = 6
    driver.set_controls(1_000_000, 2)
    driver.capture()
    before = driver._picam.requests_made
    driver.set_controls(1_000_000, 2)          # same settings
    driver.capture()
    assert driver._picam.requests_made - before == 1


def test_settling_gives_up_rather_than_blocking_forever(driver, caplog):
    """A sensor that never agrees must not hang the night."""
    driver._picam.settle_lag = 999
    driver.set_controls(3_000_000, 2)
    with caplog.at_level("WARNING"):
        frame = driver.capture()
    assert frame is not None
    assert any("did not settle" in r.getMessage() for r in caplog.records)


def test_frame_records_what_the_sensor_did_not_what_was_asked(driver):
    """The sensor quantises to its line time; the sidecar should say so."""
    driver._picam.settle_lag = 0
    driver.set_controls(100_000, 2)
    driver._picam.applied["ExposureTime"] = 99_954     # as the real sensor reports
    driver._picam.requested["ExposureTime"] = 99_954
    assert driver.capture().exposure_us == 99_954


def test_camera_id_and_metadata(driver):
    frame = driver.capture()
    assert driver._info.camera_id == "picam-imx477"
    assert driver._info.driver == "picam"
    assert frame.sensor_temp_c == 41.5
    assert frame.bit_depth == 16
