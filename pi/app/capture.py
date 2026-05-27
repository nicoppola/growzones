"""Scheduled capture worker.

One APScheduler job runs every `interval_seconds`. Each fire:
  1. Bail if outside the configured window.
  2. Bail if free disk < 500 MB.
  3. Acquire the camera lock; apply the profile fresh (overriding any
     Live-tab tweaks); capture; measure mean V.
  4. If mean V < dark_skip_threshold, discard the frame (it's nighttime).
     Otherwise write the JPEG + append a record to that day's JSONL log.
  5. Restore the prior Live-tab session settings so framing tweaks survive
     across scheduled captures.

Refuses to start if no camera profile is saved.
"""
from __future__ import annotations

import io
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from PIL import Image

from .camera import (
    CameraContext,
    STATE_DIR,
    iso_now_local,
    load_profile,
    mean_value_channel,
)
from .storage import CAPTURES_DIR, append_log_entry, free_bytes

log = logging.getLogger(__name__)

CONFIG_PATH = STATE_DIR / "capture_config.json"
CONFIG_SCHEMA_VERSION = 1
MIN_FREE_SPACE_BYTES = 500 * 1024 * 1024  # 500 MB
MIN_INTERVAL_SECONDS = 5      # picamera2 + JPEG write under tens of ms; 5s is a safety floor
MAX_INTERVAL_SECONDS = 3600   # 1 hour; longer windows are fine but probably bugs


# ---------------------------------------------------------------------------
# Config (persisted to /var/lib/growzones/capture_config.json)
# ---------------------------------------------------------------------------

@dataclass
class CaptureConfig:
    schema_version: int = CONFIG_SCHEMA_VERSION
    active: bool = False
    interval_seconds: int = 900       # 15 min
    window_start: str = "05:00"       # HH:MM, Pi-local TZ
    window_stop: str = "22:00"
    dark_skip_threshold: int = 30     # 0-255 mean V

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CaptureConfig":
        if d.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Unknown capture_config schema version {d.get('schema_version')!r}"
            )
        defaults = cls()
        # Pick only known fields; tolerate extras (forward-compat).
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: d.get(k, getattr(defaults, k)) for k in valid})


def load_config() -> CaptureConfig:
    if not CONFIG_PATH.exists():
        return CaptureConfig()
    return CaptureConfig.from_dict(json.loads(CONFIG_PATH.read_text()))


def save_config(cfg: CaptureConfig) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2))
    tmp.replace(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NoProfileError(RuntimeError):
    """Raised if start() is called without a saved camera profile."""


# ---------------------------------------------------------------------------
# Per-day stats (lives in memory; reset at midnight)
# ---------------------------------------------------------------------------

@dataclass
class DayStats:
    date: str
    saved: int = 0
    skipped_dark: int = 0
    skipped_low_disk: int = 0
    skipped_out_of_window: int = 0
    errors: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class CaptureScheduler:
    def __init__(self, camera: CameraContext) -> None:
        self._camera = camera
        self._scheduler = BackgroundScheduler()
        self._config = load_config()
        self._lock = threading.Lock()
        self._stats: DayStats = DayStats(date=_today_str())
        self._last_error: str | None = None
        self._last_capture_at: str | None = None

    # ----------------------------- public API ----------------------------

    @property
    def config(self) -> CaptureConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def state(self) -> dict:
        """Snapshot for the Capture tab + /api/capture/state."""
        # Roll stats to today if the day has changed (covers idle overnight).
        if self._stats.date != _today_str():
            self._stats = DayStats(date=_today_str())
        return {
            "active": self._config.active,
            "running": self.is_running,
            "in_window": self._in_window(),
            "config": self._config.to_dict(),
            "today_stats": self._stats.to_dict(),
            "last_capture_at": self._last_capture_at,
            "last_error": self._last_error,
        }

    def start(self) -> None:
        with self._lock:
            if self._scheduler.running:
                return
            if load_profile() is None:
                raise NoProfileError(
                    "No camera profile saved — run the Setup wizard first"
                )
            self._scheduler.add_job(
                self._capture_job,
                IntervalTrigger(seconds=self._config.interval_seconds),
                id="capture",
                replace_existing=True,
                # next_run_time omitted → APScheduler computes from interval.
                max_instances=1,
                coalesce=True,
            )
            self._scheduler.start()
            self._config.active = True
            save_config(self._config)
            log.info(
                "Capture scheduler started (interval=%ss, window=%s-%s)",
                self._config.interval_seconds,
                self._config.window_start,
                self._config.window_stop,
            )

    def stop(self) -> None:
        with self._lock:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            # APScheduler doesn't restart cleanly after shutdown — make a new one.
            self._scheduler = BackgroundScheduler()
            self._config.active = False
            save_config(self._config)
            log.info("Capture scheduler stopped")

    def update_config(self, **changes: Any) -> CaptureConfig:
        """Validate, persist, re-schedule the running job if needed.

        Accepts any subset of the CaptureConfig fields. Raises ValueError
        on out-of-range or malformed values.
        """
        with self._lock:
            new = CaptureConfig.from_dict(self._config.to_dict())
            for k, v in changes.items():
                if not hasattr(new, k) or k == "schema_version":
                    raise ValueError(f"Unknown config field: {k!r}")
                setattr(new, k, v)
            _validate_config(new)
            interval_changed = new.interval_seconds != self._config.interval_seconds
            window_changed = (
                new.window_start != self._config.window_start
                or new.window_stop != self._config.window_stop
            )
            self._config = new
            save_config(self._config)
            if self._scheduler.running and (interval_changed or window_changed):
                # Window changes don't reschedule the job (still runs at the
                # interval; the job itself checks _in_window) but interval does.
                if interval_changed:
                    self._scheduler.reschedule_job(
                        "capture",
                        trigger=IntervalTrigger(seconds=self._config.interval_seconds),
                    )
                    log.info("Rescheduled to interval=%ss", self._config.interval_seconds)
        return self._config

    # ------------------------------ job ----------------------------------

    def _capture_job(self) -> None:
        """One scheduled fire. Runs in an APScheduler worker thread; must be
        synchronous + tolerant of every kind of failure (we don't want a
        crash here to take down the rest of the service)."""
        try:
            self._capture_once()
        except Exception as e:  # noqa: BLE001  — last line of defense
            self._stats.errors += 1
            self._last_error = f"{type(e).__name__}: {e}"
            log.exception("capture_job failed")

    def _capture_once(self) -> None:
        # Roll the per-day stats container if midnight passed.
        today = _today_str()
        if self._stats.date != today:
            self._stats = DayStats(date=today)

        if not self._in_window():
            self._stats.skipped_out_of_window += 1
            return

        if free_bytes() < MIN_FREE_SPACE_BYTES:
            self._stats.skipped_low_disk += 1
            log.warning("Skipping capture: free space below %d MB", MIN_FREE_SPACE_BYTES // 1024 // 1024)
            return

        profile = load_profile()
        if profile is None:
            # Defensive: profile was deleted while scheduler was running.
            log.error("Profile disappeared mid-run; stopping scheduler")
            self._last_error = "Camera profile missing"
            self.stop()
            return

        # Snapshot Live-tab tweaks so we can restore them after capture.
        prior_session_settings = self._camera.current_settings

        try:
            with self._camera.lock(state="capturing"):
                # Apply profile fresh — this is the authoritative state for
                # scheduled captures, regardless of what the Live tab last did.
                self._camera.apply_profile(profile)

                arr = self._camera.capture_still_array()
                mean_v = mean_value_channel(arr)

                if mean_v < self._config.dark_skip_threshold:
                    self._stats.skipped_dark += 1
                    log.debug(
                        "Dark frame skip (mean_v=%.1f < %d)",
                        mean_v,
                        self._config.dark_skip_threshold,
                    )
                else:
                    captured_at = datetime.now().astimezone()
                    date = captured_at.strftime("%Y-%m-%d")
                    filename = captured_at.strftime("%H-%M-%S") + ".jpg"
                    day_dir = CAPTURES_DIR / date
                    day_dir.mkdir(parents=True, exist_ok=True)
                    path = day_dir / filename

                    # Write JPEG (write-tmp + rename so a half-written file
                    # never appears in the directory listing).
                    tmp = path.with_suffix(".jpg.tmp")
                    Image.fromarray(arr).save(tmp, format="JPEG", quality=92)
                    tmp.replace(path)

                    append_log_entry(date, {
                        "image": filename,
                        "captured_at": captured_at.isoformat(timespec="seconds"),
                        "exposure_time_us": profile.exposure_time_us,
                        "analogue_gain": profile.analogue_gain,
                        "colour_gains": list(profile.colour_gains),
                        "lens_position": profile.lens_position,
                        "scaler_crop": list(profile.scaler_crop),
                        "profile_calibrated_at": profile.calibrated_at,
                        "mean_v": round(float(mean_v), 1),
                    })

                    self._stats.saved += 1
                    self._last_capture_at = captured_at.isoformat(timespec="seconds")
                    log.info("Captured %s/%s (mean_v=%.1f)", date, filename, mean_v)

                # Restore Live-tab tweaks so they survive the scheduled fire.
                if prior_session_settings:
                    self._restore_session(prior_session_settings)
        except Exception:
            # Outermost _capture_job will record + log; just don't swallow here.
            raise

    # ------------------------------ helpers ------------------------------

    def _restore_session(self, settings: dict[str, Any]) -> None:
        """Re-apply the Live-tab user's tweaks after a scheduled capture."""
        kwargs: dict[str, Any] = {}
        if "exposure_time_us" in settings:
            kwargs["exposure_time_us"] = settings["exposure_time_us"]
        if "analogue_gain" in settings:
            kwargs["analogue_gain"] = settings["analogue_gain"]
        if "colour_gains" in settings:
            cg = settings["colour_gains"]
            kwargs["colour_gains"] = (float(cg[0]), float(cg[1]))
        if "lens_position" in settings:
            kwargs["lens_position"] = settings["lens_position"]
        if "scaler_crop" in settings:
            kwargs["scaler_crop"] = tuple(settings["scaler_crop"])
        if kwargs:
            self._camera.apply_manual(**kwargs)

    def _in_window(self) -> bool:
        """Is the current Pi-local time within [window_start, window_stop]?"""
        now = datetime.now().time()
        start = time.fromisoformat(self._config.window_start)
        stop = time.fromisoformat(self._config.window_stop)
        if start <= stop:
            return start <= now <= stop
        # Wraps midnight (e.g. 22:00 - 06:00 for night-only capture).
        return now >= start or now <= stop


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_config(cfg: CaptureConfig) -> None:
    if not (MIN_INTERVAL_SECONDS <= cfg.interval_seconds <= MAX_INTERVAL_SECONDS):
        raise ValueError(
            f"interval_seconds out of range [{MIN_INTERVAL_SECONDS}, {MAX_INTERVAL_SECONDS}]"
        )
    # window_start / window_stop must parse as HH:MM
    try:
        time.fromisoformat(cfg.window_start)
        time.fromisoformat(cfg.window_stop)
    except ValueError:
        raise ValueError(
            f"window_start/window_stop must be HH:MM (got {cfg.window_start!r}, {cfg.window_stop!r})"
        )
    if not (0 <= cfg.dark_skip_threshold <= 255):
        raise ValueError("dark_skip_threshold must be in [0, 255]")


def _today_str() -> str:
    """Today in Pi-local timezone, YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")
