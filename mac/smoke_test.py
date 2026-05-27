"""End-to-end smoke test for the GrowZones Mac pipeline.

Builds a synthetic Pi-style tar bundle, imports it into a fresh Location
inside a temporary data dir, then runs auto-cull, multi-day heatmap
averaging, K-means zone clustering, and (if ffmpeg is present) timelapse
generation. Verifies every expected artifact exists.

The point is to surface shape/interface bugs before the user has real
hardware producing real captures. Synthetic data is not photorealistic —
it's a moving white "sun spot" against a dark background — but it exercises
every code path and produces sensible-looking outputs.

Run: source .venv/bin/activate && python smoke_test.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, time, timedelta
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

# --- Redirect DATA_ROOT before any growzones submodule reads it -------------

_TMP_HOME = Path(tempfile.mkdtemp(prefix="growzones_smoke_"))
print(f"Using temp data root: {_TMP_HOME}")

import growzones.locations as L  # noqa: E402
L.DATA_ROOT = _TMP_HOME / "data"
L.LOCATIONS_INDEX = L.DATA_ROOT / "locations.json"
L.LAST_LOCATION_FILE = L.DATA_ROOT / ".last_location"

from growzones import auto_cull, bundle, heatmap, locations, timelapse, zones  # noqa: E402


# --- Synthetic bundle generation -------------------------------------------

IMG_W, IMG_H = 320, 240
SUN_RADIUS = 40
N_DAYS = 4
FRAMES_PER_DAY = 10
FIRST_FRAME_TIME = time(12, 0)   # 12:00 - puts plenty of frames in solar-noon window
FRAME_INTERVAL_MIN = 15
BUNDLE_SCHEMA = 1


def _synth_frame(frame_index: int, day_index: int) -> Image.Image:
    """One synthetic frame: dark scene with a bright sun spot that moves
    across the frame. Day-to-day variation is small (so SSIM stays high)."""
    rng = np.random.default_rng(seed=42 + day_index * 1000 + frame_index)
    arr = np.full((IMG_H, IMG_W, 3), 30, dtype=np.uint8)  # dim grey "scene"
    # Add a vertical "wall" feature for visual texture.
    arr[:, :60, :] = 45
    arr[:, 60:62, :] = 90  # edge of the wall
    # Add some scene noise — small spatial variance so the sun-mask
    # variance/edge test has something to grip on.
    noise = rng.integers(-5, 5, size=(IMG_H, IMG_W, 1), dtype=np.int16)
    arr = np.clip(arr.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    # Sun spot moves left-to-right across the frame as the day progresses.
    cx = int(IMG_W * 0.1 + (frame_index / max(FRAMES_PER_DAY - 1, 1)) * IMG_W * 0.7)
    cy = int(IMG_H * 0.4)
    yy, xx = np.ogrid[:IMG_H, :IMG_W]
    sun = (xx - cx) ** 2 + (yy - cy) ** 2 <= SUN_RADIUS ** 2
    arr[sun] = (245, 245, 240)
    # Soft edge: pixels just outside the disk are brighter than scene baseline
    edge = ((xx - cx) ** 2 + (yy - cy) ** 2 <= (SUN_RADIUS + 6) ** 2) & ~sun
    arr[edge] = (180, 180, 175)

    return Image.fromarray(arr)


def build_synthetic_bundle(out_path: Path, start_date: str = "2026-05-20") -> None:
    """Write a tar that looks like a Pi export at out_path."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    days_meta = []

    with tarfile.open(out_path, mode="w") as tar:
        for d in range(N_DAYS):
            date = start + timedelta(days=d)
            date_str = date.isoformat()
            log_records: list[dict] = [{"schema_version": 1, "date": date_str}]
            total_bytes = 0

            for f in range(FRAMES_PER_DAY):
                capture_time = (
                    datetime.combine(date, FIRST_FRAME_TIME)
                    + timedelta(minutes=FRAME_INTERVAL_MIN * f)
                )
                fname = capture_time.strftime("%H-%M-%S") + ".jpg"
                img = _synth_frame(f, d)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=92)
                jpeg_bytes = buf.getvalue()
                total_bytes += len(jpeg_bytes)

                info = tarfile.TarInfo(f"captures/{date_str}/{fname}")
                info.size = len(jpeg_bytes)
                info.mtime = int(capture_time.timestamp())
                tar.addfile(info, BytesIO(jpeg_bytes))

                mean_v = float(np.asarray(img.convert("RGB")).max(axis=2).mean())
                log_records.append({
                    "image": fname,
                    "captured_at": capture_time.astimezone().isoformat(timespec="seconds"),
                    "exposure_time_us": 800,
                    "analogue_gain": 1.0,
                    "colour_gains": [1.72, 1.61],
                    "lens_position": None,
                    "scaler_crop": [0, 0, IMG_W, IMG_H],
                    "profile_calibrated_at": "2026-05-19T13:42:00-04:00",
                    "mean_v": round(mean_v, 1),
                })

            log_bytes = "\n".join(json.dumps(r) for r in log_records).encode() + b"\n"
            log_info = tarfile.TarInfo(f"captures/{date_str}/_pi_capture_log.jsonl")
            log_info.size = len(log_bytes)
            log_info.mtime = int(datetime.combine(date, time(22, 0)).timestamp())
            tar.addfile(log_info, BytesIO(log_bytes))

            days_meta.append({
                "date": date_str,
                "image_count": FRAMES_PER_DAY,
                "total_bytes": total_bytes,
            })

        manifest = {
            "schema_version": BUNDLE_SCHEMA,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pi_hostname": "smoke-test-pi",
            "date_range": [days_meta[0]["date"], days_meta[-1]["date"]],
            "days": days_meta,
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode()
        m_info = tarfile.TarInfo("manifest.json")
        m_info.size = len(manifest_bytes)
        m_info.mtime = int(datetime.now().timestamp())
        tar.addfile(m_info, BytesIO(manifest_bytes))


# --- Test orchestration -----------------------------------------------------

def _check(label: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        sys.exit(1)


def main() -> int:
    bundle_path = _TMP_HOME / "bundle.tar"
    print("\n[1/8] Building synthetic bundle…")
    build_synthetic_bundle(bundle_path)
    _check("bundle written", bundle_path.exists(), f"{bundle_path.stat().st_size} bytes")

    print("\n[2/8] Creating location…")
    loc = locations.create_location("Smoke Test Balcony", notes="synthetic")
    _check("location exists", locations.location_exists(loc.slug), loc.slug)
    _check("captures_dir present", loc.captures_dir.exists())

    print("\n[3/8] Importing bundle…")
    result = bundle.import_bundle(bundle_path, loc)
    _check("days_added == N_DAYS", result.days_added == N_DAYS, f"got {result.days_added}")
    _check("images_added == N_DAYS * FRAMES_PER_DAY",
           result.images_added == N_DAYS * FRAMES_PER_DAY,
           f"got {result.images_added}")
    _check("thumbnails generated", result.thumbnails_generated == N_DAYS * FRAMES_PER_DAY,
           f"got {result.thumbnails_generated}")
    # First day has no prior -> no drift; subsequent days should score high SSIM
    # (synthetic scenes are stable across days), so no drift warnings expected.
    _check("no spurious drift warnings", len(result.drift_warnings) == 0,
           f"got {result.drift_warnings}")

    # Verify a sample day's on-disk layout
    sample_day = sorted(loc.captures_dir.iterdir())[0].name
    sample_dir = loc.captures_dir / sample_day
    _check("JPEGs present", len(list(sample_dir.glob("*.jpg"))) == FRAMES_PER_DAY)
    _check("_pi_capture_log.jsonl present", (sample_dir / "_pi_capture_log.jsonl").exists())
    _check("_mac_meta.json present", (sample_dir / "_mac_meta.json").exists())
    _check("_thumbs/ has thumbnails",
           len(list((sample_dir / "_thumbs").glob("*.jpg"))) == FRAMES_PER_DAY)

    print("\n[4/8] Re-importing same bundle (idempotency)…")
    result2 = bundle.import_bundle(bundle_path, loc)
    _check("no new images on re-import", result2.images_added == 0,
           f"got {result2.images_added}")
    _check("re-imported days are 'updated' not 'added'",
           result2.days_added == 0 and result2.days_updated == N_DAYS,
           f"added={result2.days_added}, updated={result2.days_updated}")

    print("\n[5/8] Auto-culling each day…")
    for day_dir in sorted(loc.captures_dir.iterdir()):
        date = day_dir.name
        payload = auto_cull.auto_cull_day(loc, date)
        n_suggestions = len(payload["suggestions"])
        _check(f"{date}: suggestions written",
               n_suggestions == FRAMES_PER_DAY,
               f"got {n_suggestions}")
        _check(f"{date}: _cull_suggestions.json on disk",
               (day_dir / "_cull_suggestions.json").exists())

    print("\n[6/8] Tagging all days as 'clear'…")
    for day_dir in sorted(loc.captures_dir.iterdir()):
        meta_path = day_dir / "_mac_meta.json"
        meta = json.loads(meta_path.read_text())
        meta["tag"] = "clear"
        meta["tag_source"] = "user"
        meta_path.write_text(json.dumps(meta, indent=2))
    n_clear = heatmap.count_clear_days(
        loc, _date_of(0), _date_of(N_DAYS - 1)
    )
    _check("all days tagged clear", n_clear == N_DAYS, f"got {n_clear}")

    print("\n[7/8] Running heatmap + zones…")
    avg = heatmap.process_range(loc, _date_of(0), _date_of(N_DAYS - 1))
    _check("averaged heatmap shape (H, W)", avg.shape == (IMG_H, IMG_W),
           f"got {avg.shape}")
    _check("averaged heatmap has sun signal", avg.max() > 0,
           f"max sun_minutes={avg.max():.1f}")

    stats = zones.compute_zones(
        loc, _date_of(0), _date_of(N_DAYS - 1), k=4, random_state=42
    )
    _check("stats payload has 4 zones", len(stats["zones"]) == 4)
    _check("zones sorted by mean_sun_minutes_per_day descending",
           all(
               stats["zones"][i]["mean_sun_minutes_per_day"]
               >= stats["zones"][i + 1]["mean_sun_minutes_per_day"]
               for i in range(3)
           ))
    _check("zone PNG written",
           (loc.results_dir / f"zones_{_date_of(0)}_to_{_date_of(N_DAYS - 1)}.png").exists())
    _check("heatmap PNG written",
           (loc.results_dir / f"heatmap_{_date_of(0)}_to_{_date_of(N_DAYS - 1)}.png").exists())
    _check("stats JSON written",
           (loc.results_dir / f"stats_{_date_of(0)}_to_{_date_of(N_DAYS - 1)}.json").exists())
    _check("clear_days_used reported", stats["clear_days_used"] == N_DAYS,
           f"got {stats['clear_days_used']}")
    _check("random_state recorded", stats.get("random_state") == 42)

    print("\n[8/8] Timelapse (only if ffmpeg is present)…")
    if shutil.which("ffmpeg") is None:
        print("  (skipping: ffmpeg not on PATH)")
    else:
        out = timelapse.make_timelapse(
            loc, _date_of(0), _date_of(N_DAYS - 1),
        )
        _check("timelapse mp4 written", out.exists(), f"{out.stat().st_size} bytes")

    print("\nAll smoke checks passed.")
    print(f"Inspect outputs under: {loc.results_dir}")
    return 0


def _date_of(index: int) -> str:
    """Date string for index-th synthetic day, matching build_synthetic_bundle."""
    return (datetime.strptime("2026-05-20", "%Y-%m-%d") + timedelta(days=index)).date().isoformat()


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # Leave the temp dir for post-mortem inspection; print where it is.
        print(f"\nTemp data root left in place at: {_TMP_HOME}")
        print("Delete with: rm -rf", _TMP_HOME)
