"""Calibration pipeline.

End-to-end: detect sensor → (optionally) lock focus → calibrate exposure →
calibrate white balance → test capture. Yields SSE events at each step so
the UI can narrate progress live.

The "save" step is separate (`save_profile`) and explicitly invoked by the
user — `run_calibration` returns a candidate that isn't persisted until they
click [Save profile].
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from .camera import (
    CameraContext,
    CameraProfile,
    PROFILE_SCHEMA_VERSION,
    SETUP_TESTS_DIR,
    SensorInfo,
    iso_now_local,
    mean_value_channel,
    save_profile as _persist_profile,
    value_channel_percentile,
    value_channel_variance,
)

log = logging.getLogger(__name__)

# Exposure calibration tuning
TARGET_V_PERCENTILE = 99
TARGET_V_CEILING = 250        # 99th-percentile V must land here or below
MIN_ANALOGUE_GAIN = 1.0       # don't go below the sensor's native unity
MAX_CALIBRATION_ITERATIONS = 6

# "Is direct sun actually in this frame?" check before saving
LOW_VARIANCE_WARN_THRESHOLD = 600.0  # V-channel variance; tune on real data


@dataclass
class CandidateProfile:
    """In-memory candidate produced by run_calibration; not persisted until
    save_profile() is called."""
    sensor: SensorInfo
    exposure_time_us: int
    analogue_gain: float
    colour_gains: list[float]
    lens_position: float | None
    scaler_crop: list[int]
    test_capture_path: str | None = None
    test_capture_variance: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_profile(self) -> CameraProfile:
        return CameraProfile(
            schema_version=PROFILE_SCHEMA_VERSION,
            calibrated_at=iso_now_local(),
            sensor=self.sensor,
            exposure_time_us=self.exposure_time_us,
            analogue_gain=self.analogue_gain,
            colour_gains=self.colour_gains,
            lens_position=self.lens_position,
            scaler_crop=self.scaler_crop,
            test_capture_path=self.test_capture_path,
        )


# ---------------------------------------------------------------------------
# SSE event helpers
# ---------------------------------------------------------------------------

def _evt(event: str, **data) -> dict:
    """Shape of the dict sse-starlette wraps into a SSE record."""
    return {"event": event, "data": json.dumps(data)}


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

async def run_calibration(camera: CameraContext) -> AsyncIterator[dict]:
    """Run the full calibration pipeline, yielding SSE events.

    The caller (FastAPI route) wraps this in an EventSourceResponse. The
    final event is `complete` with the full candidate payload; the UI then
    posts to /api/setup/save-profile if the user wants to keep it.
    """
    yield _evt("step", index=1, total=5, name="detect", message="Detecting sensor…")
    sensor = camera.sensor
    yield _evt(
        "detect.result",
        model=sensor.model,
        native_width=sensor.native_width,
        native_height=sensor.native_height,
        supports_autofocus=sensor.supports_autofocus,
    )

    # --------------- 2. lock focus (AF only) ----------------
    lens_position: float | None = None
    if sensor.supports_autofocus:
        yield _evt("step", index=2, total=5, name="focus", message="Locking focus…")
        lens_position = await asyncio.to_thread(_lock_focus, camera)
        yield _evt("focus.result", lens_position=lens_position)
    else:
        yield _evt(
            "step",
            index=2,
            total=5,
            name="focus",
            message="Fixed-focus sensor — skipping focus step.",
            skipped=True,
        )

    # --------------- 3. calibrate exposure ------------------
    yield _evt("step", index=3, total=5, name="exposure", message="Calibrating exposure…")
    async for ev in _calibrate_exposure(camera):
        yield ev
    # The exposure routine ends with an `exposure.result` event; we need its
    # values for the candidate, so re-run a non-yielding peek (the values
    # are now applied to the camera).
    exposure_time_us, analogue_gain = await asyncio.to_thread(_current_exposure, camera)

    # --------------- 4. calibrate white balance -------------
    yield _evt("step", index=4, total=5, name="white_balance", message="Calibrating white balance…")
    red, blue = await asyncio.to_thread(_calibrate_white_balance, camera)
    yield _evt("white_balance.result", red=red, blue=blue)
    if abs(red - 1.8) > 0.6 or abs(blue - 1.6) > 0.6:
        yield _evt(
            "warning",
            message=(
                f"White balance gains (R={red:.2f}, B={blue:.2f}) are far from "
                "daylight standard (~1.8 / ~1.6) — calibrate in pure daylight "
                "for best results."
            ),
        )

    # --------------- 5. test capture ------------------------
    yield _evt("step", index=5, total=5, name="test_capture", message="Taking a test capture…")
    test_path, variance = await asyncio.to_thread(_test_capture, camera)
    yield _evt(
        "test_capture.result",
        path=str(test_path),
        url=f"/api/setup/test-image/{test_path.name}",
        variance=round(variance, 1),
    )

    candidate = CandidateProfile(
        sensor=sensor,
        exposure_time_us=int(exposure_time_us),
        analogue_gain=float(analogue_gain),
        colour_gains=[red, blue],
        lens_position=lens_position,
        scaler_crop=[0, 0, sensor.native_width, sensor.native_height],
        test_capture_path=str(test_path),
        test_capture_variance=float(variance),
    )
    if variance < LOW_VARIANCE_WARN_THRESHOLD:
        candidate.warnings.append(
            "Frame variance is low — direct sun may not be in the frame. "
            "Exposure calibration is only accurate at midday clear-sky."
        )

    # picamera2's DMA buffer queue can get corrupted by the preview ↔ still
    # mode switching this pipeline does; without a reset, MJPEG capture_array
    # silently hangs afterward. Must run BEFORE the final yield, because the
    # browser typically closes the SSE stream the moment it sees `complete`,
    # which cancels the generator before any post-yield work would run.
    await asyncio.to_thread(_reset_camera, camera)

    yield _evt("complete", candidate=_candidate_payload(candidate))


def _reset_camera(camera: CameraContext) -> None:
    with camera.lock(state="setup"):
        camera.reset()


# ---------------------------------------------------------------------------
# Steps (blocking; call via asyncio.to_thread from the pipeline)
# ---------------------------------------------------------------------------

def _lock_focus(camera: CameraContext) -> float:
    with camera.lock(state="setup"):
        lens_position = camera.autofocus_settle()
        # Switch to manual at that position so it stays put for the rest.
        camera.apply_manual(lens_position=lens_position)
    return lens_position


async def _calibrate_exposure(camera: CameraContext) -> AsyncIterator[dict]:
    """Iteratively converge on a manual exposure where 99th-pct V ≤ ceiling.

    Strategy: start from auto-exposure's choice. Capture. Measure 99th-pct V.
    If too bright, halve gain first; once gain is at the floor, halve exposure
    time. Cap at MAX_CALIBRATION_ITERATIONS — if still clipping, save anyway
    with a warning.
    """
    # Seed from auto-exposure.
    md = await asyncio.to_thread(_auto_exposure_seed, camera)
    exposure_time_us = int(md.get("ExposureTime") or 10_000)
    analogue_gain = float(md.get("AnalogueGain") or 1.0)

    for iteration in range(1, MAX_CALIBRATION_ITERATIONS + 1):
        # Lock these values and measure.
        v99 = await asyncio.to_thread(
            _apply_and_measure, camera, exposure_time_us, analogue_gain
        )
        decision: str
        if v99 <= TARGET_V_CEILING:
            decision = "converged"
        elif analogue_gain > MIN_ANALOGUE_GAIN:
            # Halve gain (clamped to floor)
            analogue_gain = max(MIN_ANALOGUE_GAIN, analogue_gain / 2)
            decision = "halve_gain"
        else:
            exposure_time_us = max(1, exposure_time_us // 2)
            decision = "halve_exposure"

        yield _evt(
            "exposure.iteration",
            iteration=iteration,
            v99=round(v99, 1),
            exposure_time_us=exposure_time_us,
            analogue_gain=round(analogue_gain, 3),
            decision=decision,
        )

        if decision == "converged":
            yield _evt(
                "exposure.result",
                exposure_time_us=exposure_time_us,
                analogue_gain=round(analogue_gain, 3),
                iterations=iteration,
                v99=round(v99, 1),
            )
            return

    # Exhausted iterations without converging.
    yield _evt(
        "warning",
        message=(
            f"Exposure did not converge after {MAX_CALIBRATION_ITERATIONS} "
            f"iterations (99th-pct V still {v99:.0f}). Highlights may clip. "
            "Consider re-running in brighter conditions."
        ),
    )
    yield _evt(
        "exposure.result",
        exposure_time_us=exposure_time_us,
        analogue_gain=round(analogue_gain, 3),
        iterations=MAX_CALIBRATION_ITERATIONS,
        v99=round(v99, 1),
        converged=False,
    )


def _auto_exposure_seed(camera: CameraContext) -> dict:
    with camera.lock(state="setup"):
        return camera.apply_auto_exposure()


def _apply_and_measure(
    camera: CameraContext, exposure_time_us: int, analogue_gain: float
) -> float:
    with camera.lock(state="setup"):
        camera.apply_manual(
            exposure_time_us=exposure_time_us, analogue_gain=analogue_gain
        )
        arr = camera.capture_still_array()
        return value_channel_percentile(arr, TARGET_V_PERCENTILE)


def _current_exposure(camera: CameraContext) -> tuple[int, float]:
    """Read back what's currently applied — used after _calibrate_exposure to
    capture the final values into the candidate."""
    with camera.lock(state="setup"):
        md = camera._cam.capture_metadata()  # noqa: SLF001
    return int(md.get("ExposureTime") or 0), float(md.get("AnalogueGain") or 1.0)


def _calibrate_white_balance(camera: CameraContext) -> tuple[float, float]:
    with camera.lock(state="setup"):
        return camera.apply_auto_white_balance()


def _test_capture(camera: CameraContext) -> tuple[Path, float]:
    SETUP_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{iso_now_local().replace(':', '-')}.jpg"
    path = SETUP_TESTS_DIR / filename
    with camera.lock(state="setup"):
        camera.capture_still_jpeg(path)
        # Re-grab the array for variance check (cheap; we already have it
        # cached in metadata but the picamera2 API doesn't expose it back).
        arr = camera.capture_still_array()
    return path, value_channel_variance(arr)


def _candidate_payload(c: CandidateProfile) -> dict:
    """Serializable view of CandidateProfile for the `complete` SSE event."""
    return {
        "sensor": {
            "model": c.sensor.model,
            "native_width": c.sensor.native_width,
            "native_height": c.sensor.native_height,
            "supports_autofocus": c.sensor.supports_autofocus,
        },
        "exposure_time_us": c.exposure_time_us,
        "analogue_gain": c.analogue_gain,
        "colour_gains": c.colour_gains,
        "lens_position": c.lens_position,
        "scaler_crop": c.scaler_crop,
        "test_capture_url": (
            f"/api/setup/test-image/{Path(c.test_capture_path).name}"
            if c.test_capture_path
            else None
        ),
        "test_capture_variance": c.test_capture_variance,
        "warnings": c.warnings,
    }


# ---------------------------------------------------------------------------
# Save (separate from run_calibration; called from /api/setup/save-profile)
# ---------------------------------------------------------------------------

def save_candidate(candidate_payload: dict) -> CameraProfile:
    """Validate, build a CameraProfile, persist atomically."""
    required = {
        "sensor",
        "exposure_time_us",
        "analogue_gain",
        "colour_gains",
        "scaler_crop",
    }
    missing = required - candidate_payload.keys()
    if missing:
        raise ValueError(f"Candidate missing required fields: {sorted(missing)}")
    sensor = SensorInfo(**candidate_payload["sensor"])
    profile = CameraProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        calibrated_at=iso_now_local(),
        sensor=sensor,
        exposure_time_us=int(candidate_payload["exposure_time_us"]),
        analogue_gain=float(candidate_payload["analogue_gain"]),
        colour_gains=list(candidate_payload["colour_gains"]),
        lens_position=candidate_payload.get("lens_position"),
        scaler_crop=list(candidate_payload["scaler_crop"]),
        test_capture_path=candidate_payload.get("test_capture_path"),
    )
    _persist_profile(profile)
    return profile
