"""Per-day sun_minutes heatmap + multi-day averaging.

Each saved capture stands for `interval_seconds` worth of elapsed time (the
Pi scheduler fires on a fixed interval), so accumulating sun-mask hits ->
sun_minutes is just `interval_seconds / 60 * mask_count`. Per-day arrays are
cached as `_heatmap.npz` so re-running with one more clear day is cheap:
process_range just re-averages the cached arrays.

Only "clear"-tagged days contribute to averaging. <3 clear days -> caller
should surface a warning; <1 is an error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import cv2
import numpy as np

from .locations import Location
from .sun_mask import sun_mask

SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 900  # 15 min, matches Pi default
HEATMAP_FILENAME = "_heatmap.npz"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_mac_meta(day_dir: Path) -> dict:
    meta_path = day_dir / "_mac_meta.json"
    if not meta_path.exists():
        return {"tag": None, "excluded_images": []}
    data = json.loads(meta_path.read_text())
    if data.get("schema_version") not in (None, SCHEMA_VERSION):
        raise ValueError(
            f"Unknown _mac_meta.json schema version {data.get('schema_version')!r}"
        )
    return data


def _list_day_frames(day_dir: Path, excluded: set[str]) -> list[Path]:
    return sorted(
        p for p in day_dir.glob("*.jpg")
        if not p.name.startswith("_") and p.name not in excluded
    )


def _iter_dates(captures_dir: Path, date_from: str, date_to: str):
    """Yield (date_str, day_dir) for every YYYY-MM-DD dir in [from, to]."""
    if not captures_dir.is_dir():
        return
    for entry in sorted(captures_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if not DATE_RE.match(name):
            continue
        if date_from <= name <= date_to:
            yield name, entry


# ---------------------------------------------------------------------------
# Per-day heatmap
# ---------------------------------------------------------------------------

def process_day(location: Location, date: str, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> Path:
    """Build _heatmap.npz for one day. Returns the .npz path.

    sun_minutes[y, x] = (interval_seconds / 60) * count(frames where mask[y, x] is set).
    uint16 is plenty: max possible at 15-min sampling over a 17-hr window is
    17*60 = 1020 minutes; uint16 caps at 65535.
    """
    day_dir = location.captures_dir / date
    if not day_dir.is_dir():
        raise FileNotFoundError(f"No capture dir for {date} in {location.slug}")

    meta = _load_mac_meta(day_dir)
    excluded = set(meta.get("excluded_images", []))
    frames = _list_day_frames(day_dir, excluded)
    if not frames:
        raise ValueError(f"No eligible frames in {day_dir}")

    minutes_per_hit = interval_seconds / 60.0
    accumulator: np.ndarray | None = None

    for path in frames:
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise IOError(f"Could not read image {path}")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        mask = sun_mask(rgb)  # uint8 {0, 255}
        hit = (mask > 0).astype(np.uint16)
        if accumulator is None:
            accumulator = np.zeros_like(hit, dtype=np.uint32)
        accumulator += hit

    assert accumulator is not None
    sun_minutes = np.clip(
        np.round(accumulator.astype(np.float32) * minutes_per_hit),
        0, np.iinfo(np.uint16).max,
    ).astype(np.uint16)

    out_path = day_dir / HEATMAP_FILENAME
    # Write to a sibling .tmp then rename for atomicity. np.savez_compressed
    # auto-appends '.npz' if the filename doesn't already end in it — so we
    # pass an open file handle to keep the .tmp suffix.
    tmp = out_path.with_suffix(".npz.tmp")
    with open(tmp, "wb") as f:
        np.savez_compressed(f, sun_minutes=sun_minutes)
    tmp.replace(out_path)
    return out_path


# ---------------------------------------------------------------------------
# Multi-day average
# ---------------------------------------------------------------------------

def process_range(location: Location, date_from: str, date_to: str) -> np.ndarray:
    """Average per-day _heatmap.npz arrays across all clear days in [from, to].

    Returns sun_minutes_per_day (float32). Builds any missing _heatmap.npz
    on the fly. Skips any day whose _mac_meta.json.tag != "clear".

    Raises ValueError if no clear days found in range. Callers with <3 clear
    days should surface a "average isn't trustworthy yet" warning.
    """
    sum_arr: np.ndarray | None = None
    clear_days = 0

    for date, day_dir in _iter_dates(location.captures_dir, date_from, date_to):
        meta = _load_mac_meta(day_dir)
        if meta.get("tag") != "clear":
            continue

        npz_path = day_dir / HEATMAP_FILENAME
        if not npz_path.exists():
            process_day(location, date)

        with np.load(npz_path) as f:
            arr = f["sun_minutes"].astype(np.float32)

        if sum_arr is None:
            sum_arr = arr.copy()
        elif arr.shape != sum_arr.shape:
            # Camera resolution changed mid-range (different sensor swap, or
            # different ScalerCrop). The plan handles this by demanding a
            # separate location/import; here we just fail loud.
            raise ValueError(
                f"Heatmap shape mismatch in {date}: {arr.shape} vs {sum_arr.shape}"
            )
        else:
            sum_arr += arr
        clear_days += 1

    if clear_days == 0:
        raise ValueError(
            f"No clear-tagged days in range {date_from}..{date_to} for {location.slug}"
        )
    return sum_arr / float(clear_days)


def count_clear_days(location: Location, date_from: str, date_to: str) -> int:
    """Cheap helper for the Process page to show "N clear days" before running."""
    n = 0
    for _, day_dir in _iter_dates(location.captures_dir, date_from, date_to):
        meta = _load_mac_meta(day_dir)
        if meta.get("tag") == "clear":
            n += 1
    return n
