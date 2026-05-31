"""MJPEG stream endpoint: pushes JPEG frames as multipart/x-mixed-replace.

A single background producer reads the camera and publishes each JPEG to all
connected streams (Setup tab + Live tab + N curl clients all share frames).
This avoids per-stream lock contention that previously caused any second
connection to render black while the first held the camera.
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


class _FrameHub:
    """Single producer, many subscribers. The producer task starts on the
    first subscriber and stops when the last subscriber leaves.

    Subscribers emit at TARGET_FPS regardless of producer activity — so when
    the camera lock is held by the setup wizard or capture worker, the stream
    re-emits the last good frame instead of going silent. Going silent (no
    bytes on the wire for several seconds) causes browsers' `<img>` elements
    to give up and render black on the multipart/x-mixed-replace stream."""

    def __init__(self) -> None:
        self._latest: bytes | None = None
        self._subscribers = 0
        self._producer_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def subscribe(self, camera: CameraContext):
        async with self._lock:
            self._subscribers += 1
            if self._producer_task is None or self._producer_task.done():
                self._producer_task = asyncio.create_task(self._produce(camera))
        interval = 1.0 / TARGET_FPS
        try:
            # Wait briefly for the first frame so a new subscriber doesn't
            # have to wait the full 1/TARGET_FPS interval.
            for _ in range(20):
                if self._latest is not None:
                    break
                await asyncio.sleep(0.05)
            while True:
                if self._latest is not None:
                    yield self._latest
                await asyncio.sleep(interval)
        finally:
            async with self._lock:
                self._subscribers -= 1
                if self._subscribers == 0 and self._producer_task is not None:
                    self._producer_task.cancel()
                    self._producer_task = None

    async def _produce(self, camera: CameraContext) -> None:
        interval = 1.0 / TARGET_FPS
        try:
            while True:
                loop_start = time.monotonic()
                try:
                    with camera.lock(state="streaming", blocking=False):
                        jpeg = await asyncio.to_thread(camera.capture_preview_jpeg)
                    self._latest = jpeg
                except CameraBusy:
                    # Setup wizard / capture worker has the lock; leave the
                    # last good frame in place so subscribers keep emitting.
                    pass
                except Exception as e:
                    log.warning("MJPEG capture error: %s", e)

                elapsed = time.monotonic() - loop_start
                sleep = interval - elapsed
                if sleep > 0:
                    await asyncio.sleep(sleep)
        except asyncio.CancelledError:
            pass


_HUB = _FrameHub()


async def mjpeg_stream(camera: CameraContext) -> StreamingResponse:
    """Return a StreamingResponse whose body is an unbounded MJPEG stream."""
    return StreamingResponse(
        _frame_generator(camera),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


async def _frame_generator(camera: CameraContext):
    async for jpeg in _HUB.subscribe(camera):
        yield (
            f"--{_BOUNDARY}\r\n"
            "Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode("ascii") + jpeg + b"\r\n"
