"""Capture session lifecycle.

A session = one user-driven start→stop window of timed captures. Each session
gets a directory under sessions/<id>/ containing the JPEGs, a manifest, and a
per-capture log.

Threading: at most one active session. The session worker thread takes the
camera lock for each capture, so the MJPEG preview is automatically paused
during the brief still capture.
"""
from __future__ import annotations

import io
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from . import storage
from .camera import Camera

log = logging.getLogger(__name__)

DARK_SKIP_V_THRESHOLD = 30
MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 24 * 3600


class SessionAlreadyActive(Exception):
    pass


class NoProfileSaved(Exception):
    pass


class SessionManager:
    def __init__(self, camera: Camera) -> None:
        self._camera = camera
        self._lock = threading.Lock()
        self._active_id: str | None = None
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._image_count = 0

    def active_id(self) -> str | None:
        with self._lock:
            return self._active_id

    def start(self, interval_seconds: int) -> dict[str, Any]:
        profile = storage.load_profile()
        if profile is None:
            raise NoProfileSaved("No camera profile saved. Run calibration first.")
        interval_seconds = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, int(interval_seconds)))

        with self._lock:
            if self._active_id is not None:
                raise SessionAlreadyActive(f"Session {self._active_id} is already active")
            session_id = storage.new_session_id()
            d = storage.session_dir(session_id)
            d.mkdir(parents=True, exist_ok=True)
            started_at = storage.now_iso()
            manifest: dict[str, Any] = {
                "schema_version": storage.SCHEMA_VERSION,
                "session_id": session_id,
                "started_at": started_at,
                "stopped_at": None,
                "interval_seconds": interval_seconds,
                "profile_snapshot": profile,
                "image_count": 0,
                "total_bytes": 0,
            }
            storage.write_manifest(session_id, manifest)
            self._active_id = session_id
            self._image_count = 0
            self._stop_event = threading.Event()
            # Apply profile settings to camera for the session
            self._camera.apply_settings({
                "exposure_time_us": profile.get("exposure_time_us"),
                "analogue_gain": profile.get("analogue_gain"),
                "colour_gains": profile.get("colour_gains"),
                "lens_position": profile.get("lens_position"),
                "scaler_crop": profile.get("scaler_crop"),
            })
            self._thread = threading.Thread(
                target=self._worker,
                args=(session_id, interval_seconds, self._stop_event),
                daemon=True,
            )
            self._thread.start()
            return {"id": session_id, "started_at": started_at}

    def stop(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if self._active_id != session_id:
                raise ValueError(f"Session {session_id} is not active")
            stop_event = self._stop_event
            thread = self._thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=10)
        with self._lock:
            self._active_id = None
            self._stop_event = None
            self._thread = None
        return self._finalize_manifest(session_id)

    def _finalize_manifest(self, session_id: str) -> dict[str, Any]:
        manifest = storage.load_manifest(session_id)
        manifest["stopped_at"] = storage.now_iso()
        manifest["image_count"] = len(storage.session_image_paths(session_id))
        manifest["total_bytes"] = storage.session_size_bytes(session_id)
        storage.write_manifest(session_id, manifest)
        return manifest

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        active = self.active_id()
        for sid in storage.list_session_ids():
            mp = storage.session_manifest_path(sid)
            if not mp.exists():
                continue
            try:
                m = storage.load_manifest(sid)
            except Exception:
                continue
            is_active = sid == active
            if is_active:
                m["image_count"] = len(storage.session_image_paths(sid))
                m["total_bytes"] = storage.session_size_bytes(sid)
            out.append({
                "id": sid,
                "started_at": m.get("started_at"),
                "stopped_at": m.get("stopped_at"),
                "interval_seconds": m.get("interval_seconds"),
                "image_count": m.get("image_count", 0),
                "total_bytes": m.get("total_bytes", 0),
                "active": is_active,
            })
        out.sort(key=lambda s: s["started_at"] or "", reverse=True)
        return out

    def _worker(self, session_id: str, interval: int, stop_event: threading.Event) -> None:
        log.info("session %s started, interval=%ss", session_id, interval)
        # Capture the first frame immediately, then space subsequent captures
        next_at = time.monotonic()
        while not stop_event.is_set():
            now = time.monotonic()
            if now < next_at:
                if stop_event.wait(timeout=min(0.5, next_at - now)):
                    break
                continue
            try:
                self._capture_one(session_id)
            except Exception:
                log.exception("session %s: capture failed", session_id)
            next_at = time.monotonic() + interval
        log.info("session %s worker exiting", session_id)

    def _capture_one(self, session_id: str) -> None:
        jpeg, _meta = self._camera.capture_still()
        mean_v = _mean_v(jpeg)
        if mean_v < DARK_SKIP_V_THRESHOLD:
            log.debug("session %s: skipping dark frame mean_v=%s", session_id, mean_v)
            return
        ts = datetime.now()
        filename = ts.strftime("%H-%M-%S") + ".jpg"
        path = storage.session_dir(session_id) / filename
        # Avoid same-second collisions (interval < 1s edge case)
        if path.exists():
            filename = ts.strftime("%H-%M-%S-") + f"{ts.microsecond//1000:03d}" + ".jpg"
            path = storage.session_dir(session_id) / filename
        path.write_bytes(jpeg)
        storage.append_capture_log(session_id, {
            "image": filename,
            "captured_at": storage.now_iso(),
            "mean_v": int(mean_v),
        })


def _mean_v(jpeg: bytes) -> float:
    img = Image.open(io.BytesIO(jpeg)).convert("HSV")
    v = img.split()[2]
    # PIL stat is faster than numpy here and we don't depend on numpy
    histogram = v.histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0
    weighted = sum(i * c for i, c in enumerate(histogram))
    return weighted / total
