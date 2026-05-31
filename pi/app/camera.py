"""Picamera2 wrapper with a shared lock between preview, capture, and setup wizard.

The Pi camera can only do one thing at a time; the lock here is the single
coordination point for all camera consumers. Acquire it before any
configuration switch or capture; the live MJPEG stream blocks on it during
scheduled captures and the setup wizard.
"""
from __future__ import annotations

import io
import json
import logging
import subprocess
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from picamera2 import Picamera2
from libcamera import controls

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/growzones")
PROFILE_PATH = STATE_DIR / "camera_profile.json"
SETUP_TESTS_DIR = STATE_DIR / "setup_tests"
PROFILE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Profile data class + persistence
# ---------------------------------------------------------------------------

@dataclass
class SensorInfo:
    model: str
    native_width: int
    native_height: int
    supports_autofocus: bool


@dataclass
class CameraProfile:
    schema_version: int
    calibrated_at: str
    sensor: SensorInfo
    exposure_time_us: int
    analogue_gain: float
    colour_gains: list[float]  # [red, blue]
    lens_position: float | None
    scaler_crop: list[int]  # [x, y, width, height] in sensor-native pixels
    test_capture_path: str | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CameraProfile":
        if d.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise ValueError(
                f"Unknown camera_profile.json schema version "
                f"{d.get('schema_version')!r}; expected {PROFILE_SCHEMA_VERSION}"
            )
        return cls(
            schema_version=d["schema_version"],
            calibrated_at=d["calibrated_at"],
            sensor=SensorInfo(**d["sensor"]),
            exposure_time_us=d["exposure_time_us"],
            analogue_gain=d["analogue_gain"],
            colour_gains=list(d["colour_gains"]),
            lens_position=d.get("lens_position"),
            scaler_crop=list(d["scaler_crop"]),
            test_capture_path=d.get("test_capture_path"),
        )


def load_profile() -> CameraProfile | None:
    """Return the saved profile, or None if it doesn't exist."""
    if not PROFILE_PATH.exists():
        return None
    return CameraProfile.from_dict(json.loads(PROFILE_PATH.read_text()))


def save_profile(profile: CameraProfile) -> None:
    """Write atomically: write to a tempfile in the same dir, then rename."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile.to_dict(), indent=2))
    tmp.replace(PROFILE_PATH)


# ---------------------------------------------------------------------------
# Sensor detection (works without instantiating Picamera2)
# ---------------------------------------------------------------------------

def detect_sensor() -> SensorInfo:
    """Return info about the first attached camera.

    Uses Picamera2's static `global_camera_info()` rather than shelling out to
    `libcamera-hello --list-cameras` — same data, no subprocess.
    """
    infos = Picamera2.global_camera_info()
    if not infos:
        raise RuntimeError(
            "No camera detected. Check the CSI ribbon connection and that "
            "the camera interface is enabled (raspi-config nonint do_camera 0)."
        )
    info = infos[0]
    # `Model` is the libcamera identifier (e.g. "imx219").
    # Sensor-native resolution: query a fresh Picamera2 instance briefly.
    cam = Picamera2(info["Num"])
    try:
        sensor_modes = cam.sensor_modes
        # Pick the largest mode as "native."
        native = max(sensor_modes, key=lambda m: m["size"][0] * m["size"][1])
        width, height = native["size"]
        # AF capability: presence of LensPosition in camera_controls.
        has_af = "LensPosition" in cam.camera_controls
    finally:
        cam.close()
    return SensorInfo(
        model=info.get("Model", "unknown"),
        native_width=int(width),
        native_height=int(height),
        supports_autofocus=bool(has_af),
    )


# ---------------------------------------------------------------------------
# Camera context: the single instance everything coordinates through
# ---------------------------------------------------------------------------

class CameraBusy(RuntimeError):
    """Raised when a non-blocking acquire fails."""


class CameraContext:
    """Thread-safe wrapper around a single Picamera2 instance.

    Holds the camera open for the process lifetime. All operations that
    touch the camera (config switches, captures, control changes) must
    happen under `with ctx.lock(...)` to serialize against the live stream
    and the capture worker.

    `current_state` is a string the live stream polls (via the `state`
    property below) to decide whether to overlay "Capturing image…" on a
    frozen frame.

    Session settings (`current_settings`) are the camera's *currently
    applied* values. They start from the saved profile and can be tweaked
    via the Live tab without writing to disk. The capture worker (future
    milestone) will apply profile values temporarily under the lock, then
    restore these on release so the user's framing tweaks survive a
    scheduled capture.
    """

    def __init__(self, camera_index: int = 0) -> None:
        # detect_sensor opens + closes a temporary Picamera2; run it BEFORE
        # the persistent one so the camera isn't double-opened (which the
        # ov5647 / Camera v1 rejects with "Device or resource busy").
        self._sensor = detect_sensor()
        self._cam = Picamera2(camera_index)
        self._lock = threading.RLock()
        self._state: str = "idle"
        self._configured_for: str | None = None  # "preview" | "still" | None
        self._current: dict[str, Any] = {}  # in-memory "what's applied right now"

    # ------------------------------ accessors ------------------------------
    @property
    def sensor(self) -> SensorInfo:
        return self._sensor

    @property
    def state(self) -> str:
        return self._state

    # ------------------------------- locking -------------------------------
    def lock(self, *, state: str = "capturing", blocking: bool = True):
        """Context manager: acquire the camera, set state, release.

        Use `blocking=False` to fail fast (raises CameraBusy) — only the
        live stream should do that; everything else should wait its turn.
        """
        return _CameraLockCtx(self, state=state, blocking=blocking)

    # --------------------------- configuration ----------------------------
    def _ensure_preview(self, width: int = 640, height: int = 480) -> None:
        """Switch to a low-res streaming config. Call under the lock."""
        config = self._cam.create_video_configuration(
            main={"size": (width, height), "format": "RGB888"}
        )
        if self._configured_for != "preview":
            try:
                self._cam.stop()
            except Exception:
                pass
            self._cam.configure(config)
            self._cam.start()
            self._configured_for = "preview"

    def _ensure_still(self) -> None:
        """Switch to a full-resolution still config. Call under the lock."""
        config = self._cam.create_still_configuration(
            main={"size": (self._sensor.native_width, self._sensor.native_height)}
        )
        if self._configured_for != "still":
            try:
                self._cam.stop()
            except Exception:
                pass
            self._cam.configure(config)
            self._cam.start()
            self._configured_for = "still"

    # ------------------------------ controls -------------------------------
    def apply_manual(
        self,
        *,
        exposure_time_us: int | None = None,
        analogue_gain: float | None = None,
        colour_gains: tuple[float, float] | None = None,
        lens_position: float | None = None,
        scaler_crop: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Set explicit manual values + update `current_settings`. Call under the lock."""
        ctrls: dict[str, Any] = {}
        if exposure_time_us is not None:
            ctrls["ExposureTime"] = int(exposure_time_us)
            ctrls["AeEnable"] = False
            self._current["exposure_time_us"] = int(exposure_time_us)
        if analogue_gain is not None:
            ctrls["AnalogueGain"] = float(analogue_gain)
            ctrls["AeEnable"] = False
            self._current["analogue_gain"] = float(analogue_gain)
        if colour_gains is not None:
            ctrls["ColourGains"] = (float(colour_gains[0]), float(colour_gains[1]))
            ctrls["AwbEnable"] = False
            self._current["colour_gains"] = [float(colour_gains[0]), float(colour_gains[1])]
        if lens_position is not None and self._sensor.supports_autofocus:
            ctrls["AfMode"] = controls.AfModeEnum.Manual
            ctrls["LensPosition"] = float(lens_position)
            self._current["lens_position"] = float(lens_position)
        if scaler_crop is not None:
            ctrls["ScalerCrop"] = tuple(int(v) for v in scaler_crop)
            self._current["scaler_crop"] = [int(v) for v in scaler_crop]
        if ctrls:
            self._cam.set_controls(ctrls)

    def apply_profile(self, profile: "CameraProfile") -> None:
        """Apply every setting from a saved profile. Call under the lock.
        Used at startup and (eventually) before each scheduled capture."""
        self.apply_manual(
            exposure_time_us=profile.exposure_time_us,
            analogue_gain=profile.analogue_gain,
            colour_gains=tuple(profile.colour_gains),
            lens_position=profile.lens_position,
            scaler_crop=tuple(profile.scaler_crop),
        )

    @property
    def current_settings(self) -> dict[str, Any]:
        """Snapshot of what's currently applied to the camera."""
        return dict(self._current)

    def apply_auto_exposure(self) -> dict[str, Any]:
        """Turn AE on, settle, then read the values it chose. Returns the
        metadata dict so the setup wizard can record what auto-exposure
        decided as a starting point. Call under the lock."""
        # Camera must be configured + started before set_controls / capture,
        # else capture_array blocks forever. Preview mode is fine for AE.
        self._ensure_preview()
        self._cam.set_controls({"AeEnable": True})
        # Capture once to let AE settle.
        _ = self._cam.capture_array()
        md = self._cam.capture_metadata()
        return md

    def apply_auto_white_balance(self) -> tuple[float, float]:
        """Turn AWB on, settle, return the (red, blue) gains it chose.
        Then lock AWB off with those gains. Call under the lock."""
        self._ensure_preview()
        self._cam.set_controls({"AwbEnable": True})
        _ = self._cam.capture_array()
        md = self._cam.capture_metadata()
        gains = md.get("ColourGains")
        if gains is None:
            raise RuntimeError(
                "Camera did not report ColourGains after AWB pass — "
                "sensor may not support AWB read-back"
            )
        red, blue = float(gains[0]), float(gains[1])
        self._cam.set_controls({
            "AwbEnable": False,
            "ColourGains": (red, blue),
        })
        return red, blue

    def autofocus_settle(self) -> float:
        """Run continuous AF, let it settle, return the chosen LensPosition.
        Caller is responsible for switching the camera to AfMode=Manual at
        that position afterward (via apply_manual). Call under the lock."""
        if not self._sensor.supports_autofocus:
            raise RuntimeError("Sensor does not support autofocus")
        self._cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        # Let AF settle: capture a handful of frames.
        for _ in range(8):
            _ = self._cam.capture_array()
        md = self._cam.capture_metadata()
        lp = md.get("LensPosition")
        if lp is None:
            raise RuntimeError("Camera did not report LensPosition after AF")
        return float(lp)

    # ------------------------------ captures -------------------------------
    def capture_preview_jpeg(self, *, quality: int = 75) -> bytes:
        """Single JPEG-encoded frame for the live stream. Call under the lock."""
        self._ensure_preview()
        arr = self._cam.capture_array()  # RGB888 numpy array
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def capture_still_array(self) -> np.ndarray:
        """Full-resolution RGB array, current control settings. Call under the lock."""
        self._ensure_still()
        return self._cam.capture_array()

    def capture_still_jpeg(self, path: Path, *, quality: int = 92) -> dict[str, Any]:
        """Full-resolution JPEG to disk + return capture metadata for the
        sidecar log. Call under the lock."""
        self._ensure_still()
        arr = self._cam.capture_array()
        md = self._cam.capture_metadata()
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(arr).save(path, format="JPEG", quality=quality)
        return md

    # --------------------------- internal: lock ----------------------------
    def _set_state(self, state: str) -> None:
        self._state = state

    # ---------------------------- lifecycle -------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._cam.stop()
            except Exception:
                pass
            self._cam.close()

    def reset(self) -> None:
        """Close and reopen the underlying Picamera2 instance.

        Rapid switching between preview and still configurations (as in the
        setup wizard) can corrupt picamera2's internal DMA buffer queues
        ("failed to find buffer in DmaSync"), leaving subsequent capture_array
        calls hanging indefinitely. A fresh Picamera2 instance resets all
        that state. Caller must hold the lock; callers outside the wizard
        normally shouldn't need this.
        """
        with self._lock:
            try:
                self._cam.stop()
            except Exception:
                pass
            try:
                self._cam.close()
            except Exception:
                pass
            self._cam = Picamera2(0)
            self._configured_for = None


class _CameraLockCtx:
    def __init__(self, ctx: CameraContext, *, state: str, blocking: bool) -> None:
        self._ctx = ctx
        self._state = state
        self._blocking = blocking
        self._prior_state: str | None = None

    def __enter__(self) -> CameraContext:
        acquired = self._ctx._lock.acquire(blocking=self._blocking)
        if not acquired:
            raise CameraBusy("Camera is busy")
        self._prior_state = self._ctx.state
        self._ctx._set_state(self._state)
        return self._ctx

    def __exit__(self, exc_type, exc, tb) -> None:
        self._ctx._set_state(self._prior_state or "idle")
        self._ctx._lock.release()


# ---------------------------------------------------------------------------
# Helpers used by the setup wizard's exposure step
# ---------------------------------------------------------------------------

def value_channel_percentile(rgb: np.ndarray, percentile: float) -> float:
    """V (HSV) of an RGB frame, at the given percentile.

    A single hot pixel would mislead a max-based check, so the wizard uses
    99th-percentile here to decide if the calibrated exposure clips highlights.
    """
    # RGB -> HSV V channel = max(R, G, B) / 255 then scaled. We want 0-255.
    v = rgb.max(axis=2)
    return float(np.percentile(v, percentile))


def mean_value_channel(rgb: np.ndarray) -> float:
    """Mean V for the dark-frame skip check (full frame)."""
    return float(rgb.max(axis=2).mean())


def value_channel_variance(rgb: np.ndarray) -> float:
    """Variance of V — proxy for whether hard shadow edges exist in the frame.
    Low variance ⇒ flat scene, likely no direct sun, exposure calibration
    won't generalize. Used for the wizard's pre-save warning."""
    return float(rgb.max(axis=2).var())


def iso_now_local() -> str:
    """ISO 8601 with local TZ offset (Pi-local time, set via /etc/timezone)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
