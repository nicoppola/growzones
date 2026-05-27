"""Filesystem layout helpers for /var/lib/growzones/captures/.

Layout (per the plan):
    /var/lib/growzones/captures/
        2026-05-26/
            06-15-00.jpg
            06-30-00.jpg
            ...
            _pi_capture_log.jsonl    # header line + one record per capture

A "day" is any directory matching YYYY-MM-DD; an "image" is any *.jpg inside.
We never delete files outside this tree.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from .camera import STATE_DIR

log = logging.getLogger(__name__)

CAPTURES_DIR = STATE_DIR / "captures"
LOG_FILENAME = "_pi_capture_log.jsonl"
LOG_HEADER_SCHEMA_VERSION = 1

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_day_dir(path: Path) -> bool:
    return path.is_dir() and bool(_DATE_RE.match(path.name))


def _safe_day_path(date: str) -> Path:
    """Build CAPTURES_DIR/<date>; reject anything that isn't YYYY-MM-DD."""
    if not _DATE_RE.match(date):
        raise ValueError(f"Invalid date {date!r}; expected YYYY-MM-DD")
    return CAPTURES_DIR / date


def _safe_image_path(date: str, name: str) -> Path:
    """Build CAPTURES_DIR/<date>/<name>; reject path traversal and non-jpg."""
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid image name {name!r}")
    if not name.lower().endswith(".jpg"):
        raise ValueError(f"Image name must end with .jpg: {name!r}")
    return _safe_day_path(date) / name


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_days() -> list[dict]:
    """Sorted list of capture days with summary stats."""
    if not CAPTURES_DIR.exists():
        return []
    out: list[dict] = []
    for entry in sorted(CAPTURES_DIR.iterdir(), key=lambda p: p.name):
        if not _is_day_dir(entry):
            continue
        images = list(entry.glob("*.jpg"))
        total_bytes = sum(p.stat().st_size for p in images)
        out.append({
            "date": entry.name,
            "image_count": len(images),
            "total_bytes": total_bytes,
        })
    return out


def list_images(date: str) -> list[dict]:
    """Per-image metadata for one day. Joins JPEGs on disk with the JSONL log
    so each entry includes mean_v, captured_at, exposure settings.
    Missing log entries fall back to filesystem-only info."""
    day_path = _safe_day_path(date)
    if not day_path.exists():
        raise FileNotFoundError(f"Day {date} not found")

    log_by_image: dict[str, dict] = {}
    log_path = day_path / LOG_FILENAME
    if log_path.exists():
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "image" in rec:
                    log_by_image[rec["image"]] = rec

    out: list[dict] = []
    for jpeg in sorted(day_path.glob("*.jpg")):
        st = jpeg.stat()
        info = log_by_image.get(jpeg.name, {})
        out.append({
            "image": jpeg.name,
            "bytes": st.st_size,
            "captured_at": info.get("captured_at"),
            "mean_v": info.get("mean_v"),
            "exposure_time_us": info.get("exposure_time_us"),
            "analogue_gain": info.get("analogue_gain"),
        })
    return out


def image_path(date: str, name: str) -> Path:
    """Return the absolute path to an image, or raise FileNotFoundError."""
    path = _safe_image_path(date, name)
    if not path.exists():
        raise FileNotFoundError(f"{date}/{name}")
    return path


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------

def disk_usage() -> dict:
    """Snapshot of disk + per-day capture sizes."""
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(CAPTURES_DIR)
    days = list_days()
    captures_total = sum(d["total_bytes"] for d in days)
    return {
        "disk_total_bytes": du.total,
        "disk_used_bytes": du.used,
        "disk_free_bytes": du.free,
        "captures_total_bytes": captures_total,
        "by_day": days,
    }


def free_bytes() -> int:
    """Free space on the captures filesystem."""
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(CAPTURES_DIR).free


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_day(date: str) -> dict:
    """Remove a single day's directory. Returns count + bytes freed."""
    day_path = _safe_day_path(date)
    if not day_path.exists():
        return {"date": date, "image_count": 0, "bytes_freed": 0}
    images = list(day_path.glob("*.jpg"))
    bytes_freed = sum(p.stat().st_size for p in images)
    image_count = len(images)
    shutil.rmtree(day_path)
    log.info("Deleted day %s (%d images, %d bytes)", date, image_count, bytes_freed)
    return {"date": date, "image_count": image_count, "bytes_freed": bytes_freed}


def delete_range(date_from: str, date_to: str) -> dict:
    """Remove every day between date_from and date_to (inclusive)."""
    if not _DATE_RE.match(date_from) or not _DATE_RE.match(date_to):
        raise ValueError("from/to must be YYYY-MM-DD")
    if date_from > date_to:
        raise ValueError("from must be <= to")

    per_day: list[dict] = []
    total_bytes = 0
    total_images = 0
    for d in list_days():
        if date_from <= d["date"] <= date_to:
            result = delete_day(d["date"])
            per_day.append(result)
            total_bytes += result["bytes_freed"]
            total_images += result["image_count"]
    return {
        "from": date_from,
        "to": date_to,
        "day_count": len(per_day),
        "image_count": total_images,
        "bytes_freed": total_bytes,
        "per_day": per_day,
    }


# ---------------------------------------------------------------------------
# Capture-log writer (used by capture.py)
# ---------------------------------------------------------------------------

def append_log_entry(date: str, record: dict) -> None:
    """Atomically append a record to that day's _pi_capture_log.jsonl.

    Writes the header line on first append. Uses open("a") + flush + fsync;
    JSONL appends are line-atomic on local filesystems.
    """
    day_path = _safe_day_path(date)
    day_path.mkdir(parents=True, exist_ok=True)
    log_path = day_path / LOG_FILENAME
    new_file = not log_path.exists()
    with log_path.open("a") as f:
        if new_file:
            f.write(json.dumps({
                "schema_version": LOG_HEADER_SCHEMA_VERSION,
                "date": date,
            }) + "\n")
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
        f.flush()
        import os
        os.fsync(f.fileno())
