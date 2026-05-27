"""K-means zones over the multi-day-averaged sun_minutes heatmap.

Features per pixel: (sun_minutes_normalized, x_normalized, y_normalized) all
in [0, 1]. Without normalization, raw pixel coords (0..3280) would swamp the
sun_minutes axis (~0..600) and zones would degenerate to spatial blobs that
ignore sun. Equal weighting across the three axes gives the desired
"contiguous-and-by-sun-tier" zones.

Reference frame for the overlay is picked as the median-brightness frame
within +/- 1 hour of approximated solar noon on the most recent clear day,
read cheaply from _pi_capture_log.jsonl's mean_v rather than re-reading
JPEGs.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time
from pathlib import Path

import cv2
import numpy as np
from matplotlib import cm
from sklearn.cluster import KMeans

from .heatmap import (
    DEFAULT_INTERVAL_SECONDS,
    HEATMAP_FILENAME,
    process_range,
    _iter_dates,
    _load_mac_meta,
)
from .locations import Location

SCHEMA_VERSION = 1

# Solar-noon approximation hard-coded for v1: midpoint of the default capture
# window (05:00..22:00) -> 13:30. No astronomical computation in v1.
DEFAULT_WINDOW_START = time(5, 0)
DEFAULT_WINDOW_STOP = time(22, 0)
NOON_HALF_WINDOW_SECONDS = 3600  # +/- 1 hour around approximated solar noon

# Hand-picked 4-tier palette (sun -> shade). For k != 4, we use the matplotlib
# 'plasma' colormap sampled at k points so any k still gets distinct colors.
_TIER_PALETTE_4 = [
    "#f4d03f",  # tier 1 - sunniest, warm yellow
    "#f39c12",  # tier 2 - amber
    "#a0795c",  # tier 3 - taupe
    "#566573",  # tier 4 - slate
]

_TIME_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})\.jpg$")


# ---------------------------------------------------------------------------
# Reference frame selection
# ---------------------------------------------------------------------------

def _parse_capture_log(day_dir: Path) -> list[dict]:
    """Read _pi_capture_log.jsonl, skipping the header line. Returns a list
    of per-capture dicts (each with at least `image` and `mean_v`)."""
    log_path = day_dir / "_pi_capture_log.jsonl"
    if not log_path.exists():
        return []
    entries: list[dict] = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        # First line is the header {"schema_version": 1, "date": ...}; skip.
        if "image" not in rec:
            continue
        entries.append(rec)
    return entries


def _solar_noon_window() -> tuple[time, time]:
    start_sec = DEFAULT_WINDOW_START.hour * 3600 + DEFAULT_WINDOW_START.minute * 60
    stop_sec = DEFAULT_WINDOW_STOP.hour * 3600 + DEFAULT_WINDOW_STOP.minute * 60
    noon_sec = (start_sec + stop_sec) // 2
    lo = max(0, noon_sec - NOON_HALF_WINDOW_SECONDS)
    hi = min(24 * 3600 - 1, noon_sec + NOON_HALF_WINDOW_SECONDS)
    return (time(lo // 3600, (lo % 3600) // 60), time(hi // 3600, (hi % 3600) // 60))


def _filename_within_window(name: str, lo: time, hi: time) -> bool:
    m = _TIME_RE.match(name)
    if not m:
        return False
    t = time(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return lo <= t <= hi


def pick_reference_frame(location: Location, date_from: str, date_to: str) -> Path:
    """Median-brightness, non-excluded frame within +/- 1 hr of solar noon on
    the most recent clear day in range. Reads mean_v from _pi_capture_log.jsonl
    instead of re-loading JPEGs."""
    lo, hi = _solar_noon_window()

    # Walk dates newest-first; first clear day with usable candidates wins.
    for date, day_dir in sorted(
        list(_iter_dates(location.captures_dir, date_from, date_to)),
        key=lambda x: x[0],
        reverse=True,
    ):
        meta = _load_mac_meta(day_dir)
        if meta.get("tag") != "clear":
            continue
        excluded = set(meta.get("excluded_images", []))

        candidates: list[tuple[float, Path]] = []
        for rec in _parse_capture_log(day_dir):
            name = rec["image"]
            if name in excluded:
                continue
            if not _filename_within_window(name, lo, hi):
                continue
            mean_v = rec.get("mean_v")
            if mean_v is None:
                continue
            path = day_dir / name
            if not path.exists():
                continue
            candidates.append((float(mean_v), path))

        if not candidates:
            # Capture log missing or no in-window frames; fall back to any
            # non-excluded JPEG so the overlay at least renders.
            jpgs = sorted(
                p for p in day_dir.glob("*.jpg")
                if not p.name.startswith("_") and p.name not in excluded
            )
            if jpgs:
                return jpgs[len(jpgs) // 2]
            continue

        candidates.sort(key=lambda x: x[0])
        # Median (not max) — brightest = most blown-out = bad overlay background.
        return candidates[len(candidates) // 2][1]

    raise ValueError(
        f"No clear day with a usable reference frame in {date_from}..{date_to} "
        f"for {location.slug}"
    )


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def _hex_palette(k: int) -> list[str]:
    if k == 4:
        return list(_TIER_PALETTE_4)
    cmap = cm.get_cmap("plasma", k)
    out: list[str] = []
    for i in range(k):
        r, g, b, _ = cmap(i)
        out.append("#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255)))
    return out


def _hex_to_bgr(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return (b, g, r)  # cv2 is BGR


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_heatmap_png(sun_minutes: np.ndarray, out_path: Path) -> None:
    """Plain colormapped heatmap of sun_minutes_per_day. Self-normalized."""
    arr = sun_minutes.astype(np.float32)
    if arr.max() > 0:
        norm = arr / arr.max()
    else:
        norm = arr
    cmap = cm.get_cmap("inferno")
    rgba = (cmap(norm) * 255).astype(np.uint8)  # (H, W, 4)
    bgr = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR)
    _atomic_imwrite(out_path, bgr)


def _render_zones_overlay(
    labels: np.ndarray,
    palette_hex: list[str],
    reference_path: Path,
    out_path: Path,
    alpha: float = 0.5,
) -> None:
    """Color the labelmap with the palette, alpha-blend over the reference frame."""
    ref = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
    if ref is None:
        raise IOError(f"Could not read reference frame {reference_path}")

    if ref.shape[:2] != labels.shape:
        labels = cv2.resize(
            labels.astype(np.int32),
            (ref.shape[1], ref.shape[0]),
            interpolation=cv2.INTER_NEAREST,
        )

    overlay = np.zeros_like(ref)
    for zone_id, hex_color in enumerate(palette_hex):
        overlay[labels == zone_id] = _hex_to_bgr(hex_color)

    blended = cv2.addWeighted(ref, 1.0 - alpha, overlay, alpha, 0.0)
    _atomic_imwrite(out_path, blended)


def _atomic_imwrite(path: Path, bgr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # cv2.imwrite infers format from the extension, which we lose on the .tmp
    # rename trick. Use cv2.imencode + manual write to keep the format choice
    # explicit and the rename atomic.
    ext = path.suffix or ".png"
    ok, buf = cv2.imencode(ext, bgr)
    if not ok:
        raise IOError(f"cv2.imencode failed for {path} (ext={ext})")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def _iso_now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _count_clear_days(location: Location, date_from: str, date_to: str) -> int:
    n = 0
    for _, day_dir in _iter_dates(location.captures_dir, date_from, date_to):
        if _load_mac_meta(day_dir).get("tag") == "clear":
            n += 1
    return n


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_zones(
    location: Location,
    date_from: str,
    date_to: str,
    k: int = 4,
    random_state: int = 42,
) -> dict:
    """K-means cluster pixels into k zones; write heatmap PNG, zones PNG,
    stats JSON. Returns the stats payload.
    """
    if k < 2:
        raise ValueError("k must be >= 2")

    sun_minutes = process_range(location, date_from, date_to)  # float32, (H, W)
    h, w = sun_minutes.shape

    # max_possible = capture window length * 60 minutes. Hard-coded against
    # the default 05:00-22:00 window (17 hours); see DEFAULT_WINDOW_*.
    window_minutes = (
        (DEFAULT_WINDOW_STOP.hour * 60 + DEFAULT_WINDOW_STOP.minute)
        - (DEFAULT_WINDOW_START.hour * 60 + DEFAULT_WINDOW_START.minute)
    )
    sun_norm = (sun_minutes / float(window_minutes)).clip(0.0, 1.0)

    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    x_norm = xs / float(max(w - 1, 1))
    y_norm = ys / float(max(h - 1, 1))

    features = np.stack(
        [sun_norm.ravel(), x_norm.ravel(), y_norm.ravel()], axis=1
    )  # (H*W, 3)

    kmeans = KMeans(n_clusters=k, random_state=random_state, n_init=10)
    raw_labels = kmeans.fit_predict(features).reshape(h, w)

    # Compute mean_sun_minutes per raw cluster id; sort descending to assign
    # tier 1 (sunniest) -> tier k. Tier assignment is stable across runs given
    # random_state, but we sort explicitly so the label semantic is guaranteed.
    raw_means: list[tuple[int, float, int]] = []
    for rid in range(k):
        sel = raw_labels == rid
        cnt = int(sel.sum())
        mean = float(sun_minutes[sel].mean()) if cnt else 0.0
        raw_means.append((rid, mean, cnt))
    raw_means.sort(key=lambda t: t[1], reverse=True)

    # Remap raw_label -> tier_id (0..k-1, sunniest first).
    raw_to_tier = {raw_id: tier for tier, (raw_id, _m, _c) in enumerate(raw_means)}
    tier_labels = np.zeros_like(raw_labels)
    for raw_id, tier in raw_to_tier.items():
        tier_labels[raw_labels == raw_id] = tier

    palette = _hex_palette(k)

    # Pick reference frame (lazy: only needed for the overlay PNG).
    reference_path = pick_reference_frame(location, date_from, date_to)

    # Build payload.
    clear_days_used = _count_clear_days(location, date_from, date_to)
    zones_payload = []
    for tier, (raw_id, mean, cnt) in enumerate(raw_means):
        zones_payload.append({
            "id": tier,
            "label": f"tier {tier + 1}",
            "color_hex": palette[tier],
            "mean_sun_minutes_per_day": round(mean, 1),
            "pixel_count": cnt,
        })

    stats = {
        "schema_version": SCHEMA_VERSION,
        "computed_at": _iso_now_local(),
        "date_range": [date_from, date_to],
        "clear_days_used": clear_days_used,
        "k": k,
        "random_state": random_state,
        "zones": zones_payload,
        "reference_frame": str(reference_path),
    }

    # Write outputs (atomic).
    results = location.results_dir
    results.mkdir(parents=True, exist_ok=True)
    heatmap_path = results / f"heatmap_{date_from}_to_{date_to}.png"
    zones_path = results / f"zones_{date_from}_to_{date_to}.png"
    stats_path = results / f"stats_{date_from}_to_{date_to}.json"

    _render_heatmap_png(sun_minutes, heatmap_path)
    _render_zones_overlay(tier_labels, palette, reference_path, zones_path)
    _atomic_write_json(stats_path, stats)

    return stats
