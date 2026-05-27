"""H264 timelapse generator.

8 fps over 15-min-sampled captures gives ~7 seconds per day, which is the
sweet spot for watching shadows actually move; 30 fps blinks past in 2
seconds and is useless. Each frame gets its capture timestamp burned into
the bottom-right corner via PIL before being handed to ffmpeg — that's the
only way the watcher can tell what time of day they're seeing.

Excluded frames (per `_mac_meta.json.excluded_images`) are skipped so the
timelapse reflects the same culling decisions as the heatmap.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .locations import Location

FPS_DEFAULT = 8
H264_CRF = 23
TIMESTAMP_FONT_SIZE = 36
TIMESTAMP_PADDING = 20


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def make_timelapse(
    location: Location,
    date_from: str,
    date_to: str,
    *,
    fps: int = FPS_DEFAULT,
    single_day: str | None = None,
) -> Path:
    """Generate an h264 mp4 timelapse via ffmpeg.

    Frames are pulled from `location.captures_dir/<date>/*.jpg`, timestamped
    via the matching `_pi_capture_log.jsonl` entry, and skipped if listed in
    that day's `_mac_meta.json.excluded_images`.

    The output lands under `location.results_dir`:
      - timelapse_<from>_to_<to>.mp4    (range mode)
      - timelapse_<single_day>.mp4      (single-day mode)
    """
    if fps < 1:
        raise ValueError(f"fps must be >= 1, got {fps}")

    if single_day is not None:
        dates = [single_day]
        out_name = f"timelapse_{single_day}.mp4"
    else:
        dates = _date_range(date_from, date_to)
        out_name = f"timelapse_{date_from}_to_{date_to}.mp4"

    frames = _gather_frames(location, dates)
    if not frames:
        raise ValueError(
            f"No frames found for {location.slug} in "
            f"{single_day or f'{date_from}..{date_to}'} "
            "(all excluded? wrong date range? no images imported?)"
        )

    location.results_dir.mkdir(parents=True, exist_ok=True)
    output = location.results_dir / out_name

    with tempfile.TemporaryDirectory(prefix="growzones_timelapse_") as tmpdir:
        tmp = Path(tmpdir)
        font = _load_font(TIMESTAMP_FONT_SIZE)
        for i, (img_path, captured_at) in enumerate(frames, start=1):
            stamped = _burn_timestamp(img_path, captured_at, font=font)
            stamped.save(tmp / f"frame_{i:05d}.jpg", format="JPEG", quality=85)
        _run_ffmpeg(tmp, output, fps=fps)

    return output


# ---------------------------------------------------------------------------
# Frame gathering
# ---------------------------------------------------------------------------

def _gather_frames(location: Location, dates: list[str]) -> list[tuple[Path, datetime]]:
    """Walk the captures dirs in date order, drop excluded images, return
    (jpeg_path, captured_at) pairs in chronological order."""
    frames: list[tuple[Path, datetime]] = []
    for date in dates:
        day_dir = location.captures_dir / date
        if not day_dir.is_dir():
            continue
        excluded = _excluded_images(day_dir)
        captured_at = _capture_timestamps(day_dir)
        for jpg in sorted(day_dir.glob("*.jpg")):
            if jpg.name.startswith("_"):
                continue
            if jpg.name in excluded:
                continue
            ts = captured_at.get(jpg.name) or _fallback_timestamp(date, jpg.name)
            frames.append((jpg, ts))
    return frames


def _excluded_images(day_dir: Path) -> set[str]:
    meta_path = day_dir / "_mac_meta.json"
    if not meta_path.exists():
        return set()
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return set()
    return set(meta.get("excluded_images", []) or [])


def _capture_timestamps(day_dir: Path) -> dict[str, datetime]:
    """Parse `_pi_capture_log.jsonl` into {image_filename: captured_at}."""
    log_path = day_dir / "_pi_capture_log.jsonl"
    out: dict[str, datetime] = {}
    if not log_path.exists():
        return out
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            image = entry.get("image")
            captured = entry.get("captured_at")
            if not image or not captured:
                continue
            try:
                out[image] = datetime.fromisoformat(captured)
            except ValueError:
                continue
    return out


def _fallback_timestamp(date: str, filename: str) -> datetime:
    """Derive a timestamp from the filename (HH-MM-SS.jpg) when the capture
    log is missing or doesn't list this image. Naive (no tz)."""
    stem = Path(filename).stem  # e.g. "14-15-00"
    try:
        return datetime.strptime(f"{date} {stem}", "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return datetime.strptime(date, "%Y-%m-%d")


# ---------------------------------------------------------------------------
# Per-frame timestamp burn-in
# ---------------------------------------------------------------------------

def _burn_timestamp(jpg_path: Path, captured_at: datetime, *, font: ImageFont.ImageFont) -> Image.Image:
    """Open the JPEG, draw the timestamp in the bottom-right with a
    semi-transparent dark background, return the resulting RGB image."""
    img = Image.open(jpg_path).convert("RGB")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    text = captured_at.strftime("%b %-d  %H:%M")
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad = TIMESTAMP_PADDING
    x1 = img.width - text_w - pad * 2
    y1 = img.height - text_h - pad * 2
    x2 = img.width
    y2 = img.height
    draw.rectangle([x1, y1, x2, y2], fill=(0, 0, 0, 160))
    draw.text((x1 + pad, y1 + pad - bbox[1]), text, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _load_font(size: int) -> ImageFont.ImageFont:
    """Pick the first available system font; fall back to PIL's default."""
    candidates = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def _run_ffmpeg(frames_dir: Path, output: Path, *, fps: int) -> None:
    """Encode frame_%05d.jpg → libx264 mp4. Raises on non-zero exit with the
    captured stderr — ffmpeg's error messages are the only useful signal."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install with `brew install ffmpeg`."
        )
    cmd = [
        ffmpeg, "-y",
        "-framerate", str(fps),
        "-i", str(frames_dir / "frame_%05d.jpg"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", str(H264_CRF),
        "-preset", "medium",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"ffmpeg failed (exit {e.returncode}):\n{e.stderr}"
        ) from e


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _date_range(date_from: str, date_to: str) -> list[str]:
    """Inclusive list of YYYY-MM-DD strings between two dates."""
    start = date_cls.fromisoformat(date_from)
    end = date_cls.fromisoformat(date_to)
    if end < start:
        raise ValueError(f"date_to ({date_to}) is before date_from ({date_from})")
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out
