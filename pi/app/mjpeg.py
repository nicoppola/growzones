"""MJPEG multipart/x-mixed-replace stream for the Mac UI's live feed.

Iterates `camera.grab_preview_jpeg()` indefinitely. The camera's internal lock
means the MJPEG loop yields automatically when a calibration step or scheduled
capture is in progress — the next iteration just blocks briefly.
"""
from __future__ import annotations

import time
from typing import Iterator

from .camera import Camera

BOUNDARY = "growzonesframe"
TARGET_FPS = 5


def stream_generator(camera: Camera) -> Iterator[bytes]:
    interval = 1.0 / TARGET_FPS
    while True:
        start = time.monotonic()
        try:
            jpeg = camera.grab_preview_jpeg()
        except Exception:
            time.sleep(0.5)
            continue
        yield (
            f"--{BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode("ascii") + jpeg + b"\r\n"
        elapsed = time.monotonic() - start
        if elapsed < interval:
            time.sleep(interval - elapsed)


def content_type() -> str:
    return f"multipart/x-mixed-replace; boundary={BOUNDARY}"
