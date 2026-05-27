"""Streaming tar export of captures + per-day capture logs + a manifest.

Layout (matches the bundle layout in PLAN.md):
    growzones-export-<YYYY-MM-DD>.tar
    ├── manifest.json
    └── captures/
        └── <date>/
            ├── _pi_capture_log.jsonl
            ├── <HH-MM-SS>.jpg
            └── ...

Implementation note: uncompressed (JPEGs are already compressed) tar in
streaming mode (`tarfile.open(mode="w|")`) means memory stays constant
regardless of bundle size — a multi-GB export won't OOM the Pi Zero.
"""
from __future__ import annotations

import io
import json
import logging
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from . import storage
from .storage import CAPTURES_DIR

log = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1
# 50 Mbps real-world Wi-Fi from a Pi Zero is generous; the estimate is
# only used to set expectations in the UI ("expect ~9 min"), not as a
# hard guarantee.
LAN_BPS_ESTIMATE = 50_000_000


# ---------------------------------------------------------------------------
# Range resolution + size pre-flight
# ---------------------------------------------------------------------------

def resolve_range(
    *, scope: str | None, date_from: str | None, date_to: str | None
) -> tuple[str, str]:
    """Return (from, to) for the export. `scope` shortcuts:
        all   → first..last day on disk
        today → today..today (Pi-local)
    Otherwise both `date_from` and `date_to` must be provided.
    """
    if scope == "all":
        days = storage.list_days()
        if not days:
            today = datetime.now().strftime("%Y-%m-%d")
            return today, today
        return days[0]["date"], days[-1]["date"]
    if scope == "today":
        today = datetime.now().strftime("%Y-%m-%d")
        return today, today
    if not (date_from and date_to):
        raise ValueError("from and to are required when no scope is given")
    return date_from, date_to


def compute_size(date_from: str, date_to: str) -> dict:
    """Walk on-disk days in [from, to] and tally bytes + counts.

    Returns the payload shape used by /api/export/size:
      {bytes, image_count, day_count, estimated_seconds_at_50mbps,
       from, to, days: [{date, image_count, total_bytes}]}
    """
    matching = [
        d for d in storage.list_days()
        if date_from <= d["date"] <= date_to
    ]
    # Per-day total_bytes from storage.list_days only covers JPEGs; add the
    # capture log + an estimate for the manifest. Manifest is small (KB),
    # so it doesn't move the needle for the user-facing estimate.
    total_bytes = 0
    total_images = 0
    for d in matching:
        log_path = CAPTURES_DIR / d["date"] / storage.LOG_FILENAME
        log_bytes = log_path.stat().st_size if log_path.exists() else 0
        d["log_bytes"] = log_bytes
        total_bytes += d["total_bytes"] + log_bytes
        total_images += d["image_count"]
    # Bytes-per-second estimate: include a 10% overhead for tar headers.
    estimated_seconds = int((total_bytes * 1.1 * 8) / LAN_BPS_ESTIMATE)
    return {
        "from": date_from,
        "to": date_to,
        "bytes": total_bytes,
        "image_count": total_images,
        "day_count": len(matching),
        "estimated_seconds_at_50mbps": estimated_seconds,
        "days": matching,
    }


# ---------------------------------------------------------------------------
# Tar generator
# ---------------------------------------------------------------------------

class _StreamingBuffer:
    """File-like sink that lets us pull bytes out as the tarfile writes them.

    tarfile in streaming mode calls .write() with small chunks; we accumulate,
    drain to the StreamingResponse generator after each .add() call.
    """
    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def tell(self) -> int:  # tarfile.TarFile needs this for streaming
        return len(self._buf)

    def drain(self) -> bytes:
        if not self._buf:
            return b""
        out = bytes(self._buf)
        self._buf.clear()
        return out


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname()
    except Exception:
        return "growzones"


def build_manifest(date_from: str, date_to: str, days: Iterable[dict]) -> bytes:
    days_summary = [
        {"date": d["date"], "image_count": d["image_count"], "total_bytes": d["total_bytes"]}
        for d in days
    ]
    return json.dumps({
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pi_hostname": _hostname(),
        "date_range": [date_from, date_to],
        "days": days_summary,
    }, indent=2).encode("utf-8")


def stream_tar(date_from: str, date_to: str) -> Iterator[bytes]:
    """Sync generator: yields tar bytes in chunks.

    FastAPI's StreamingResponse runs sync generators in a threadpool, so this
    doesn't block the event loop. Files are added one at a time; the buffer
    drains between each so the response keeps flowing.
    """
    days = [
        d for d in storage.list_days()
        if date_from <= d["date"] <= date_to
    ]
    buf = _StreamingBuffer()
    tar = tarfile.open(fileobj=buf, mode="w|")

    # Manifest first so the consumer can see the date range before any image
    # bytes flow.
    manifest_bytes = build_manifest(date_from, date_to, days)
    info = tarfile.TarInfo("manifest.json")
    info.size = len(manifest_bytes)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(manifest_bytes))
    chunk = buf.drain()
    if chunk:
        yield chunk

    for d in days:
        date = d["date"]
        day_dir = CAPTURES_DIR / date
        # Per-day capture log
        log_path = day_dir / storage.LOG_FILENAME
        if log_path.exists():
            tar.add(log_path, arcname=f"captures/{date}/{storage.LOG_FILENAME}")
            chunk = buf.drain()
            if chunk:
                yield chunk
        # JPEGs in chronological order
        for jpeg in sorted(day_dir.glob("*.jpg")):
            tar.add(jpeg, arcname=f"captures/{date}/{jpeg.name}")
            chunk = buf.drain()
            if chunk:
                yield chunk

    tar.close()
    chunk = buf.drain()
    if chunk:
        yield chunk


def export_filename(date_from: str, date_to: str | None = None) -> str:
    """Convention: growzones-export-<today>.tar (today = export date, not
    bundle range — matches the plan's `Content-Disposition` text)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return f"growzones-export-{today}.tar"
