"""Picamera2 wrapper.

Architecture ported from v1: two distinct configs (preview / still). Mode
switches are explicit stop → configure → start cycles, not picamera2's
switch_mode_and_capture_array shortcut. The explicit stop/start matters for
auto-WB and auto-exposure: libcamera's algorithms need a fresh pipeline start
to actually engage; keeping the camera running continuously across mode
"switches" leaves the previous state in place and the AWB algorithm never
runs.
"""
from __future__ import annotations

import io
import logging
import threading
from typing import Any

from PIL import Image

log = logging.getLogger(__name__)

try:
    from picamera2 import Picamera2  # type: ignore[import-not-found]
    from libcamera import controls  # type: ignore[import-not-found]
    PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None  # type: ignore[assignment,misc]
    controls = None  # type: ignore[assignment]
    PICAMERA2_AVAILABLE = False
    log.warning("picamera2 not available — running in stub mode (no hardware)")

PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 480


class Camera:
    """Owns the Picamera2 instance + the single shared lock.

    Methods that don't say 'locked' acquire the lock internally.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._cam: Any = None
        self._native_w = 0
        self._native_h = 0
        self._model = "unknown"
        self._supports_af = False
        self._configured_for: str | None = None  # "preview" | "still" | None
        self._live_settings: dict[str, Any] = {}

    def start(self) -> None:
        if not PICAMERA2_AVAILABLE:
            log.warning("camera.start() — stub mode")
            self._native_w, self._native_h = 3280, 2464
            self._model = "stub"
            self._supports_af = False
            return

        cam = Picamera2()
        info = Picamera2.global_camera_info()
        if info:
            props = info[0]
            self._model = props.get("Model", "unknown")
        sensor_modes = cam.sensor_modes
        if sensor_modes:
            self._native_w = sensor_modes[-1]["size"][0]
            self._native_h = sensor_modes[-1]["size"][1]

        ctrl_info = cam.camera_controls
        self._supports_af = "LensPosition" in ctrl_info

        self._cam = cam
        self._ensure_preview()

    def stop(self) -> None:
        with self.lock:
            if self._cam is not None:
                try:
                    self._cam.stop()
                except Exception:
                    pass
                self._cam.close()
                self._cam = None

    def sensor_info(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "native_width": self._native_w,
            "native_height": self._native_h,
            "supports_autofocus": self._supports_af,
        }

    # ----- Config switching: stop → configure → start cycles -----

    def _ensure_preview(self) -> None:
        """Switch to the low-res preview config. Call under the lock."""
        if not PICAMERA2_AVAILABLE or self._cam is None:
            return
        if self._configured_for == "preview":
            return
        try:
            self._cam.stop()
        except Exception:
            pass
        cfg = self._cam.create_video_configuration(
            main={"size": (PREVIEW_WIDTH, PREVIEW_HEIGHT), "format": "BGR888"}
        )
        self._cam.configure(cfg)
        self._cam.start()
        self._configured_for = "preview"
        # Reapply locked settings so they survive the start cycle.
        self._reapply_locked_controls()

    def _ensure_still(self) -> None:
        """Switch to the full-resolution still config. Call under the lock."""
        if not PICAMERA2_AVAILABLE or self._cam is None:
            return
        if self._configured_for == "still":
            return
        try:
            self._cam.stop()
        except Exception:
            pass
        cfg = self._cam.create_still_configuration(
            main={"size": (self._native_w, self._native_h)}
        )
        self._cam.configure(cfg)
        self._cam.start()
        self._configured_for = "still"
        self._reapply_locked_controls()

    def ensure_preview(self) -> None:
        """Public: callers (calibration) may explicitly request preview mode
        so AWB / AE algorithms start from a fresh pipeline."""
        with self.lock:
            self._ensure_preview()

    def _reapply_locked_controls(self) -> None:
        """After a config switch the camera starts auto-everything; re-push
        whatever the caller had locked in via apply_settings."""
        controls_dict = self._build_controls(self._live_settings)
        if controls_dict:
            self._cam.set_controls(controls_dict)

    # ----- Settings -----

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """Apply settings to the live camera. Stores them so they survive
        mode switches. settings keys: exposure_time_us, analogue_gain,
        colour_gains, scaler_crop, lens_position.
        """
        self._live_settings.update({k: v for k, v in settings.items() if v is not None})
        if not PICAMERA2_AVAILABLE or self._cam is None:
            return
        controls_dict = self._build_controls(self._live_settings)
        with self.lock:
            if controls_dict:
                self._cam.set_controls(controls_dict)

    def _build_controls(self, settings: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "exposure_time_us" in settings:
            out["ExposureTime"] = int(settings["exposure_time_us"])
            out["AeEnable"] = False
        if "analogue_gain" in settings:
            out["AnalogueGain"] = float(settings["analogue_gain"])
            out["AeEnable"] = False
        if "colour_gains" in settings and settings["colour_gains"]:
            out["ColourGains"] = tuple(settings["colour_gains"])
            out["AwbEnable"] = False
        if "lens_position" in settings and settings["lens_position"] is not None:
            out["AfMode"] = controls.AfModeEnum.Manual
            out["LensPosition"] = float(settings["lens_position"])
        if "scaler_crop" in settings and settings["scaler_crop"]:
            x, y, w, h = settings["scaler_crop"]
            out["ScalerCrop"] = (int(x), int(y), int(w), int(h))
        return out

    def live_settings(self) -> dict[str, Any]:
        return dict(self._live_settings)

    # ----- Captures -----

    def grab_preview_jpeg(self, quality: int = 80) -> bytes:
        """Grab one preview frame as JPEG. Called from the MJPEG loop."""
        with self.lock:
            if not PICAMERA2_AVAILABLE or self._cam is None:
                return _stub_jpeg(PREVIEW_WIDTH, PREVIEW_HEIGHT)
            self._ensure_preview()
            array = self._cam.capture_array("main")
        img = Image.fromarray(array)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def capture_still(self, quality: int = 92) -> tuple[bytes, dict[str, Any]]:
        """Grab one full-res still as JPEG bytes. Returns (jpeg_bytes, metadata)."""
        with self.lock:
            if not PICAMERA2_AVAILABLE or self._cam is None:
                return _stub_jpeg(self._native_w or 640, self._native_h or 480), {}
            self._ensure_still()
            array = self._cam.capture_array("main")
            metadata = {}
            try:
                metadata = dict(self._cam.capture_metadata())
            except Exception:
                pass
        img = Image.fromarray(array)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), metadata

    def reset(self) -> None:
        """Close and reopen the underlying Picamera2 instance — clears any
        DMA-buffer corruption from rapid mode switching during calibration.
        Call under the lock."""
        if not PICAMERA2_AVAILABLE:
            return
        try:
            self._cam.stop()
        except Exception:
            pass
        try:
            self._cam.close()
        except Exception:
            pass
        self._cam = Picamera2()
        self._configured_for = None
        self._ensure_preview()

    def get_raw(self) -> Any:
        """Escape hatch for calibration which needs direct camera access."""
        return self._cam


def _stub_jpeg(w: int, h: int) -> bytes:
    """Return a tiny solid-color JPEG for stub/test environments."""
    img = Image.new("RGB", (w, h), (50, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()
