"""MJPEG stream endpoint: pushes JPEG frames as multipart/x-mixed-replace.

The stream yields ~5-10 fps; it intentionally blocks on the camera lock so
that scheduled captures and the setup wizard take precedence. While the lock
is held by someone else, the stream pauses (the last frame stays on screen);
the UI overlays "Capturing image…" by polling /api/capture/state.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi.responses import StreamingResponse

from .camera import CameraBusy, CameraContext

log = logging.getLogger(__name__)

_BOUNDARY = "frame"
TARGET_FPS = 8


async def mjpeg_stream(camera: CameraContext) -> StreamingResponse:
    """Return a StreamingResponse whose body is an unbounded MJPEG stream."""
    return StreamingResponse(
        _frame_generator(camera),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


async def _frame_generator(camera: CameraContext):
    interval = 1.0 / TARGET_FPS
    last_frame: bytes | None = None
    while True:
        loop_start = time.monotonic()
        jpeg: bytes | None = None
        try:
            # Non-blocking: yield to other camera consumers.
            with camera.lock(state="streaming", blocking=False):
                jpeg = await asyncio.to_thread(camera.capture_preview_jpeg)
        except CameraBusy:
            # Someone else (capture worker or setup wizard) has the lock.
            # Re-serve the last frame so the UI doesn't go blank.
            jpeg = last_frame
        except Exception as e:
            log.warning("MJPEG capture error: %s", e)
            jpeg = last_frame

        if jpeg is not None:
            last_frame = jpeg
            yield (
                f"--{_BOUNDARY}\r\n"
                "Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n"
            ).encode("ascii") + jpeg + b"\r\n"

        # Pace to roughly TARGET_FPS, accounting for capture/yield time.
        elapsed = time.monotonic() - loop_start
        sleep = interval - elapsed
        if sleep > 0:
            await asyncio.sleep(sleep)
