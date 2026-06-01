"""Auto-calibration pipeline.

Runs on a background thread. Mac UI polls /api/setup/status at ~1 Hz to render
progress.

Pipeline:
  1. detect — read sensor info
  2. focus (AF cameras only) — Continuous AF, settle, lock at LensPosition
  3. exposure — auto-expose to seed, then iteratively shrink gain/time until
     99th-percentile V <= 250 (max 6 iterations)
  4. white balance — Auto WB, read ColourGains, lock
  5. test capture — full-res JPEG written to setup_tests/

Candidate profile is held in memory until /api/setup/save-profile persists it.
"""
from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from . import storage
from .camera import PICAMERA2_AVAILABLE, Camera

log = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]


TARGET_V_99 = 250
MAX_EXPOSURE_ITERATIONS = 6
AF_FRAMES = 8  # frames to settle continuous-AF before reading LensPosition
DAYLIGHT_GAIN_R = 2.2
DAYLIGHT_GAIN_B = 2.0


@dataclass
class CalibrationStatus:
    state: str = "idle"  # idle | running | done | error
    step: str = ""
    progress: float = 0.0
    message: str = ""
    candidate: dict[str, Any] | None = None
    test_image_url: str | None = None
    error: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


class CalibrationRunner:
    """One runner instance for the app's lifetime. Holds the latest status
    + candidate profile."""

    def __init__(self, camera: Camera) -> None:
        self._camera = camera
        self._status = CalibrationStatus()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._lock:
            if self._status.state == "running":
                return False
            self._status = CalibrationStatus(state="running", step="starting", message="Starting calibration")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            s = self._status
            return {
                "state": s.state,
                "step": s.step,
                "progress": s.progress,
                "message": s.message,
                "candidate": s.candidate,
                "test_image_url": s.test_image_url,
                "error": s.error,
                "history": list(s.history),
            }

    def candidate(self) -> dict[str, Any] | None:
        with self._lock:
            return self._status.candidate

    def _update(self, *, step: str | None = None, progress: float | None = None, message: str | None = None) -> None:
        with self._lock:
            if step is not None:
                self._status.step = step
            if progress is not None:
                self._status.progress = progress
            if message is not None:
                self._status.message = message
                self._status.history.append({"step": self._status.step, "message": message})
                log.info("calibrate: %s", message)

    def _run(self) -> None:
        try:
            self._do_calibration()
        except Exception as e:
            log.exception("calibration failed")
            with self._lock:
                self._status.state = "error"
                self._status.error = str(e)
                self._status.message = f"Calibration failed: {e}"

    def _do_calibration(self) -> None:
        cam = self._camera
        sensor = cam.sensor_info()

        # Step 1: detect
        self._update(step="detect", progress=0.1, message=f"Detected {sensor['model']} ({sensor['native_width']}x{sensor['native_height']}, AF={sensor['supports_autofocus']})")

        # Step 2: focus
        lens_position: float | None = None
        if sensor["supports_autofocus"]:
            self._update(step="focus", progress=0.2, message="Running continuous autofocus")
            lens_position = self._do_focus()
            self._update(step="focus", progress=0.3, message=f"Focus locked at LensPosition={lens_position:.2f}")
        else:
            self._update(step="focus", progress=0.3, message="Sensor has no autofocus — skipping focus step")

        # Step 3: exposure — seed with AE, then iteratively tighten until V99 <= ceiling
        self._update(step="exposure", progress=0.35, message="Seeding auto-exposure")
        exposure_time_us, analogue_gain = self._do_exposure_seed()
        self._update(progress=0.40, message=f"AE seed: ExposureTime={exposure_time_us}us, AnalogueGain={analogue_gain:.2f}")
        exposure_time_us, analogue_gain = self._do_exposure_tighten(exposure_time_us, analogue_gain)

        # Step 4: white balance — runs AT THE LOCKED EXPOSURE so the gains
        # reflect the actual scene illumination the captures will see.
        self._update(step="white_balance", progress=0.78, message="Calibrating white balance")
        colour_gains = self._do_white_balance()
        wb_warn = ""
        if abs(colour_gains[0] - DAYLIGHT_GAIN_R) > 0.6 or abs(colour_gains[1] - DAYLIGHT_GAIN_B) > 0.6:
            wb_warn = " (gains far from daylight standard — calibrating under non-daylight?)"
        self._update(progress=0.85, message=f"ColourGains=({colour_gains[0]:.2f}, {colour_gains[1]:.2f}){wb_warn}")

        # Apply final locked settings before test capture
        scaler_crop = [0, 0, sensor["native_width"], sensor["native_height"]]
        cam.apply_settings({
            "exposure_time_us": exposure_time_us,
            "analogue_gain": analogue_gain,
            "colour_gains": colour_gains,
            "lens_position": lens_position,
            "scaler_crop": scaler_crop,
        })

        # Step 5: test capture
        self._update(step="test_capture", progress=0.9, message="Capturing test frame")
        jpeg, _meta = cam.capture_still()
        test_name = f"{storage.now_iso().replace(':', '-')}.jpg"
        storage.SETUP_TESTS_DIR.mkdir(parents=True, exist_ok=True)
        test_path = storage.SETUP_TESTS_DIR / test_name
        test_path.write_bytes(jpeg)

        # Sanity check: if the test frame has low variance, warn (likely no direct sun in scene)
        variance_warn = ""
        v99 = self._measure_v_p99(jpeg)
        if v99 < 180:
            variance_warn = " — frame appears flat (no direct sun?). Calibration may clip on a sunny day."

        candidate = {
            "schema_version": storage.SCHEMA_VERSION,
            "calibrated_at": storage.now_iso(),
            "sensor": sensor,
            "exposure_time_us": exposure_time_us,
            "analogue_gain": analogue_gain,
            "colour_gains": list(colour_gains),
            "lens_position": lens_position,
            "scaler_crop": scaler_crop,
            "test_capture_path": str(test_path),
        }
        with self._lock:
            self._status.candidate = candidate
            self._status.test_image_url = f"/api/setup/test-image/{test_name}"
            self._status.state = "done"
            self._status.step = "done"
            self._status.progress = 1.0
            self._status.message = f"Calibration complete{variance_warn}"

    def _do_focus(self) -> float:
        if not PICAMERA2_AVAILABLE:
            return 4.0
        from libcamera import controls  # type: ignore[import-not-found]
        raw = self._camera.get_raw()
        with self._camera.lock:
            raw.set_controls({"AfMode": controls.AfModeEnum.Continuous})
            # Drive the pipeline so AF actually has frames to focus on.
            for _ in range(AF_FRAMES):
                raw.capture_array("main")
            md = raw.capture_metadata()
            lens_position = float(md.get("LensPosition", 0.0))
            raw.set_controls({"AfMode": controls.AfModeEnum.Manual, "LensPosition": lens_position})
        return lens_position

    def _do_exposure_seed(self) -> tuple[int, float]:
        """Run AE for one frame in preview mode, read what it chose.

        Mirrors v1: ensure preview (stop/configure/start cycle so AE engages
        from a fresh state), set AeEnable=True, capture one frame, read
        metadata.
        """
        if not PICAMERA2_AVAILABLE:
            return 10000, 1.0
        raw = self._camera.get_raw()
        with self._camera.lock:
            self._camera._ensure_preview()
            raw.set_controls({"AeEnable": True})
            raw.capture_array("main")
            md = raw.capture_metadata()
            exposure_time_us = int(md.get("ExposureTime", 10000))
            analogue_gain = float(md.get("AnalogueGain", 1.0))
        return exposure_time_us, analogue_gain

    def _do_exposure_tighten(self, exposure_time_us: int, analogue_gain: float) -> tuple[int, float]:
        """Iteratively halve gain/exposure until 99th-percentile V <= 250.
        WB is already locked; this only touches exposure controls.
        """
        if not PICAMERA2_AVAILABLE:
            return exposure_time_us, analogue_gain
        raw = self._camera.get_raw()
        v99 = 0
        for i in range(1, MAX_EXPOSURE_ITERATIONS + 1):
            with self._camera.lock:
                raw.set_controls({
                    "AeEnable": False,
                    "ExposureTime": exposure_time_us,
                    "AnalogueGain": analogue_gain,
                })
            jpeg, _ = self._camera.capture_still()
            v99 = self._measure_v_p99(jpeg)
            self._update(
                progress=0.45 + 0.3 * (i / MAX_EXPOSURE_ITERATIONS),
                message=f"Iter {i}: ExposureTime={exposure_time_us}us, AnalogueGain={analogue_gain:.2f}, V99={v99}",
            )
            if v99 <= TARGET_V_99:
                return exposure_time_us, analogue_gain
            if analogue_gain > 1.0:
                analogue_gain = max(1.0, analogue_gain / 2.0)
            else:
                exposure_time_us = max(50, exposure_time_us // 2)
        self._update(message=f"Warning: still clipping after {MAX_EXPOSURE_ITERATIONS} iterations (V99={v99}); saving anyway")
        return exposure_time_us, analogue_gain

    def _do_white_balance(self) -> tuple[float, float]:
        """Run AWB after switching back to preview mode (the stop/start cycle
        gives libcamera's AWB algorithm a fresh pipeline state, which is what
        actually makes it engage). Mirrors v1.
        """
        if not PICAMERA2_AVAILABLE:
            return (1.72, 1.61)
        raw = self._camera.get_raw()
        with self._camera.lock:
            self._camera._ensure_preview()  # stop/configure/start — critical for AWB
            raw.set_controls({"AwbEnable": True})
            raw.capture_array("main")
            md = raw.capture_metadata()
            gains = md.get("ColourGains")
            if gains is None:
                raise RuntimeError("Camera did not report ColourGains after AWB pass")
            red, blue = float(gains[0]), float(gains[1])
            raw.set_controls({"AwbEnable": False, "ColourGains": (red, blue)})
        log.info("AWB locked at red=%.3f blue=%.3f", red, blue)
        return (red, blue)

    def _measure_v_p99(self, jpeg: bytes) -> int:
        if np is None:
            return 200
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        arr = np.asarray(img)
        # V channel = max across RGB at each pixel
        v = arr.max(axis=2)
        return int(np.percentile(v, 99))
