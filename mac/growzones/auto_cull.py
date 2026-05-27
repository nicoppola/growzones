"""Auto-cull: score every frame of a day against the day's median and flag
outliers for the Cull page to pre-check.

The trick is masking out "expected-to-change" pixels (top quartile of per-day
V variance — that's where the sun's shadow march lives) before scoring. Without
that mask, every frame would score high simply because the sun moved between
shots. With it, only unexpected changes (you walking through, a bird, a closed
parasol) score above threshold.

Memory: V-only stacking. 40 frames at 3280x2464 ~ 320 MB; fine on a Mac and
the reason this never runs on the Pi.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .locations import Location

SCHEMA_VERSION = 1
AUTO_CULL_DIFF_THRESHOLD = 30  # 0-255 V scale; unrelated to Pi dark_skip threshold


def _iso_now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_mac_meta(day_dir: Path) -> dict:
    """Read _mac_meta.json or return the empty default."""
    meta_path = day_dir / "_mac_meta.json"
    if not meta_path.exists():
        return {"tag": None, "excluded_images": []}
    data = json.loads(meta_path.read_text())
    if data.get("schema_version") not in (None, SCHEMA_VERSION):
        raise ValueError(
            f"Unknown _mac_meta.json schema version {data.get('schema_version')!r}"
        )
    return data


def _list_day_frames(day_dir: Path) -> list[Path]:
    """All .jpg files in the day dir, sorted by filename (== HH-MM-SS)."""
    return sorted(p for p in day_dir.glob("*.jpg") if not p.name.startswith("_"))


def _load_v(path: Path) -> np.ndarray:
    """Load a JPEG, return the V (max RGB) channel as uint8. BGR -> just take max."""
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise IOError(f"Could not read image {path}")
    return bgr.max(axis=2)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def auto_cull_day(location: Location, date: str, threshold: int = AUTO_CULL_DIFF_THRESHOLD) -> dict:
    """Score every frame of `date` and write _cull_suggestions.json.

    Defensively skips frames already in _mac_meta.json.excluded_images — they
    shouldn't influence the median (otherwise a user-confirmed outlier still
    pulls the baseline toward itself).
    """
    day_dir = location.captures_dir / date
    if not day_dir.is_dir():
        raise FileNotFoundError(f"No capture dir for {date} in {location.slug}")

    meta = _load_mac_meta(day_dir)
    excluded = set(meta.get("excluded_images", []))

    all_frames = _list_day_frames(day_dir)
    eligible = [p for p in all_frames if p.name not in excluded]
    if not eligible:
        raise ValueError(f"No eligible frames in {day_dir} (all excluded or empty)")

    # Stack V channels of eligible frames. uint8 -> ~ H*W*N bytes.
    eligible_v: dict[str, np.ndarray] = {p.name: _load_v(p) for p in eligible}
    stack = np.stack(list(eligible_v.values()), axis=0)  # (N, H, W)

    median_v = np.median(stack, axis=0).astype(np.float32)
    variance_v = stack.astype(np.float32).var(axis=0)

    # Pixels above 75th percentile of variance = expected-to-change (shadow march).
    var_cut = float(np.percentile(variance_v, 75))
    expected_to_change = variance_v > var_cut
    stable_mask = ~expected_to_change

    if not stable_mask.any():
        # Pathological: every pixel changed. Score against the whole frame.
        stable_mask = np.ones_like(stable_mask, dtype=bool)

    suggestions = []
    # Score every captured frame (including user-excluded ones) so the Cull
    # grid can still show their diff_score for context.
    for path in all_frames:
        if path.name in eligible_v:
            frame_v = eligible_v[path.name].astype(np.float32)
        else:
            frame_v = _load_v(path).astype(np.float32)
        diff = np.abs(frame_v - median_v)
        score = float(diff[stable_mask].mean())
        suggestions.append({
            "image": path.name,
            "diff_score": round(score, 2),
            "flagged": score > threshold,
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "computed_at": _iso_now_local(),
        "threshold": threshold,
        "suggestions": suggestions,
    }
    _atomic_write_json(day_dir / "_cull_suggestions.json", payload)
    return payload
