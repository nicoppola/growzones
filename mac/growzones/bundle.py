"""Bundle import: extract a Pi-generated `growzones-export-*.tar` into a
Location, idempotently.

Rules baked in here, in order of importance:
  1. Mac-only state (`_mac_meta.json`, `_cull_suggestions.json`, `_heatmap.npz`)
     is never touched on re-import — re-imports preserve culling work.
  2. JPEG extraction is byte-identity idempotent: re-importing the same bundle
     never rewrites a file or duplicates work.
  3. Every newly-extracted JPEG gets a 480x360 thumbnail under `_thumbs/` so
     the Cull page can render a day grid as ~4 MB instead of ~80 MB.
  4. After a day is imported, we compute the per-pixel median V-channel frame
     and SSIM against the previous imported day's median; that score goes
     into `_mac_meta.json.drift_ssim_vs_prior` so the Days page can warn on
     "camera may have moved" events. The median frame is cached to disk
     (`_median_v.npy`) so subsequent imports don't redecode old JPEGs.

This module is deliberately Streamlit-free — the CLI imports it too.
"""
from __future__ import annotations

import hashlib
import json
import logging
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim

from .locations import Location

log = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = 1
MAC_META_SCHEMA_VERSION = 1

THUMB_SIZE = (480, 360)
THUMB_QUALITY = 70
SSIM_DOWNSAMPLE = (320, 240)
DRIFT_WARN_THRESHOLD = 0.85

_DATE_DIR_FMT = "%Y-%m-%d"
_THUMBS_DIRNAME = "_thumbs"
_MEDIAN_V_FILE = "_median_v.npy"
_MAC_META_FILE = "_mac_meta.json"
_PI_LOG_FILE = "_pi_capture_log.jsonl"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    location_slug: str
    bundle_path: Path
    days_added: int = 0
    days_updated: int = 0
    images_added: int = 0
    images_skipped_existing: int = 0
    thumbnails_generated: int = 0
    drift_warnings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "location_slug": self.location_slug,
            "bundle_path": str(self.bundle_path),
            "days_added": self.days_added,
            "days_updated": self.days_updated,
            "images_added": self.images_added,
            "images_skipped_existing": self.images_skipped_existing,
            "thumbnails_generated": self.thumbnails_generated,
            "drift_warnings": list(self.drift_warnings),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def import_bundle(tar_path: Path, location: Location) -> ImportResult:
    """Extract `tar_path` into `location.captures_dir` per the rules above."""
    tar_path = Path(tar_path)
    result = ImportResult(location_slug=location.slug, bundle_path=tar_path)

    location.captures_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(tar_path, mode="r:*") as tar:
        _validate_manifest(tar)
        # Group members by capture date so we can post-process each day once
        # (median V frame, SSIM, _mac_meta.json) instead of per-file.
        members_by_date = _group_capture_members(tar)
        sorted_dates = sorted(members_by_date.keys())

        for date in sorted_dates:
            day_dir = location.captures_dir / date
            existed = day_dir.exists()
            day_dir.mkdir(parents=True, exist_ok=True)

            newly_extracted = _extract_day(
                tar=tar,
                members=members_by_date[date],
                day_dir=day_dir,
                result=result,
            )

            if existed:
                result.days_updated += 1
            else:
                result.days_added += 1

            _generate_thumbnails(day_dir, newly_extracted, result)
            _finalize_day_meta(
                location=location,
                date=date,
                day_dir=day_dir,
                result=result,
            )

    return result


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _validate_manifest(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.getmember("manifest.json")
    except KeyError as e:
        raise ValueError("Bundle is missing manifest.json at the tar root") from e
    fh = tar.extractfile(member)
    if fh is None:
        raise ValueError("manifest.json could not be read from bundle")
    manifest = json.loads(fh.read())
    version = manifest.get("schema_version")
    if version != BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            f"Unknown bundle schema version {version!r}. Update the Mac CLI."
        )
    return manifest


# ---------------------------------------------------------------------------
# Member traversal
# ---------------------------------------------------------------------------

def _group_capture_members(tar: tarfile.TarFile) -> dict[str, list[tarfile.TarInfo]]:
    """Return {date: [tarinfo, ...]} for every captures/<date>/* member."""
    grouped: dict[str, list[tarfile.TarInfo]] = {}
    for m in tar.getmembers():
        if not m.isfile():
            continue
        parts = Path(m.name).parts
        if len(parts) < 3 or parts[0] != "captures":
            continue
        date = parts[1]
        # Validate the date directory is well-formed before we touch disk.
        try:
            datetime.strptime(date, _DATE_DIR_FMT)
        except ValueError:
            log.warning("Skipping malformed date dir in bundle: %r", date)
            continue
        grouped.setdefault(date, []).append(m)
    return grouped


def _extract_day(
    *,
    tar: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    day_dir: Path,
    result: ImportResult,
) -> list[Path]:
    """Extract every member for one date. Returns paths of *newly-written* JPEGs."""
    newly_extracted: list[Path] = []
    for m in members:
        name = Path(m.name).name
        if name == _PI_LOG_FILE:
            _merge_pi_capture_log(tar, m, day_dir / _PI_LOG_FILE)
            continue
        if not name.lower().endswith(".jpg"):
            # Unknown sidecar — copy it verbatim so we don't silently drop things.
            target = day_dir / name
            if target.exists():
                result.images_skipped_existing += 1
                continue
            _extract_member_atomic(tar, m, target)
            continue

        target = day_dir / name
        if target.exists() and _same_bytes(tar, m, target):
            result.images_skipped_existing += 1
            continue
        if target.exists():
            # Different bytes already on disk — preserve user/local copy; never
            # overwrite. This shouldn't happen in normal use; log loudly.
            log.warning(
                "Refusing to overwrite existing %s (bytes differ from bundle)",
                target,
            )
            result.images_skipped_existing += 1
            continue
        _extract_member_atomic(tar, m, target)
        result.images_added += 1
        newly_extracted.append(target)
    return newly_extracted


def _extract_member_atomic(
    tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path
) -> None:
    """Stream a tar member to a sibling .tmp file, then rename. Avoids leaving
    half-written files visible if the import crashes mid-extract."""
    fh = tar.extractfile(member)
    if fh is None:
        raise ValueError(f"Could not read {member.name!r} from bundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "wb") as out:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(target)


def _same_bytes(tar: tarfile.TarFile, member: tarfile.TarInfo, on_disk: Path) -> bool:
    """Cheap-fast first (size), then hash if sizes match."""
    if on_disk.stat().st_size != member.size:
        return False
    fh = tar.extractfile(member)
    if fh is None:
        return False
    bundle_h = hashlib.sha1()
    while True:
        chunk = fh.read(1024 * 1024)
        if not chunk:
            break
        bundle_h.update(chunk)
    disk_h = hashlib.sha1()
    with open(on_disk, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            disk_h.update(chunk)
    return bundle_h.digest() == disk_h.digest()


# ---------------------------------------------------------------------------
# Pi capture log: append-only merge keyed on "image"
# ---------------------------------------------------------------------------

def _merge_pi_capture_log(
    tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path
) -> None:
    fh = tar.extractfile(member)
    if fh is None:
        return
    incoming_lines = fh.read().decode("utf-8").splitlines()

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text("\n".join(incoming_lines) + ("\n" if incoming_lines else ""))
        tmp.replace(target)
        return

    existing_lines = target.read_text().splitlines()
    have_images: set[str] = set()
    for line in existing_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "image" in obj:
            have_images.add(obj["image"])

    added: list[str] = []
    for line in incoming_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "image" in obj and obj["image"] in have_images:
            continue
        # Header lines (no "image" key) we let through; harmless to have a
        # second header line if a day spans two exports.
        added.append(line)

    if not added:
        return
    tmp = target.with_suffix(target.suffix + ".tmp")
    merged = existing_lines + added
    tmp.write_text("\n".join(merged) + "\n")
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

def _generate_thumbnails(
    day_dir: Path, jpeg_paths: Iterable[Path], result: ImportResult
) -> None:
    thumbs_dir = day_dir / _THUMBS_DIRNAME
    thumbs_dir.mkdir(exist_ok=True)
    for src in jpeg_paths:
        dst = thumbs_dir / src.name
        if dst.exists():
            continue
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail(THUMB_SIZE)
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                im.save(tmp, format="JPEG", quality=THUMB_QUALITY)
                tmp.replace(dst)
            result.thumbnails_generated += 1
        except Exception:
            log.exception("Failed to write thumbnail for %s", src)


# ---------------------------------------------------------------------------
# Per-day median V + SSIM drift
# ---------------------------------------------------------------------------

def _finalize_day_meta(
    *,
    location: Location,
    date: str,
    day_dir: Path,
    result: ImportResult,
) -> None:
    """Compute/refresh the median V frame for this day, score SSIM against
    the previous imported day, persist drift_ssim_vs_prior into _mac_meta.json.
    Never touches existing tag/excluded_images fields."""
    median_v = _compute_or_load_median_v(day_dir)
    if median_v is None:
        # No JPEGs in this day directory — skip; nothing to score.
        return

    prior_median = _previous_day_median_v(location.captures_dir, date)
    drift_ssim: float | None
    if prior_median is None:
        drift_ssim = None
    else:
        drift_ssim = _ssim_score(prior_median, median_v)
        if drift_ssim < DRIFT_WARN_THRESHOLD:
            result.drift_warnings.append({
                "date": date,
                "ssim_score": round(float(drift_ssim), 4),
            })

    _update_mac_meta(day_dir, drift_ssim_vs_prior=drift_ssim)


def _compute_or_load_median_v(day_dir: Path) -> np.ndarray | None:
    """Return the per-pixel median V-channel frame for `day_dir`, computing it
    from the JPEGs if the cache file is missing or stale.

    Cache invalidation: we recompute whenever the JPEG count on disk doesn't
    match the count stored alongside the cache, OR whenever the cache file
    is missing. Cheap and robust to re-imports that add new images.
    """
    jpegs = sorted(p for p in day_dir.iterdir() if p.suffix.lower() == ".jpg")
    if not jpegs:
        return None

    cache_path = day_dir / _MEDIAN_V_FILE
    count_marker = day_dir / "_median_v_count.txt"
    if cache_path.exists() and count_marker.exists():
        try:
            if int(count_marker.read_text().strip()) == len(jpegs):
                return np.load(cache_path)
        except (ValueError, OSError):
            pass

    median = _compute_median_v(jpegs)
    # np.save auto-appends '.npy' to any Path/str not already ending in '.npy',
    # so pass an open file handle and the suffix you actually want sticks.
    tmp = cache_path.with_suffix(".npy.tmp")
    with open(tmp, "wb") as f:
        np.save(f, median)
    tmp.replace(cache_path)
    count_tmp = count_marker.with_suffix(".txt.tmp")
    count_tmp.write_text(str(len(jpegs)))
    count_tmp.replace(count_marker)
    return median


def _compute_median_v(jpegs: list[Path]) -> np.ndarray:
    """Stack V channels of every JPEG (downsampled to SSIM_DOWNSAMPLE) and
    take per-pixel median. Downsampling here keeps stacked memory ~6 MB even
    for a 60-frame day."""
    frames: list[np.ndarray] = []
    for p in jpegs:
        try:
            with Image.open(p) as im:
                im = im.convert("RGB").resize(SSIM_DOWNSAMPLE, Image.BILINEAR)
                arr = np.asarray(im, dtype=np.uint8)
                # V = max(R, G, B) per HSV convention.
                frames.append(arr.max(axis=2))
        except Exception:
            log.exception("Failed to read %s for median frame", p)
    if not frames:
        # Fallback: an all-zero frame so SSIM remains computable.
        return np.zeros(SSIM_DOWNSAMPLE[::-1], dtype=np.uint8)
    stack = np.stack(frames, axis=0)
    return np.median(stack, axis=0).astype(np.uint8)


def _previous_day_median_v(captures_dir: Path, date: str) -> np.ndarray | None:
    """Find the most recent (by date) sibling day with a cached median V frame.

    Falls back to computing it from JPEGs if the prior day exists but has no
    cache yet (e.g. data imported pre-drift-detection)."""
    if not captures_dir.exists():
        return None
    candidates: list[str] = []
    for child in captures_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name >= date:
            continue
        try:
            datetime.strptime(child.name, _DATE_DIR_FMT)
        except ValueError:
            continue
        candidates.append(child.name)
    if not candidates:
        return None
    prior = max(candidates)
    prior_dir = captures_dir / prior
    return _compute_or_load_median_v(prior_dir)


def _ssim_score(a: np.ndarray, b: np.ndarray) -> float:
    """Single-channel SSIM in [0, 1]-ish (skimage may return slightly < 0
    for completely uncorrelated frames; clip for sanity)."""
    if a.shape != b.shape:
        # Shouldn't happen — both go through SSIM_DOWNSAMPLE — but be safe.
        with Image.fromarray(a) as im:
            a = np.asarray(im.resize(b.shape[::-1], Image.BILINEAR))
    score = float(ssim(a, b, data_range=255))
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# _mac_meta.json create / update
# ---------------------------------------------------------------------------

def _update_mac_meta(day_dir: Path, *, drift_ssim_vs_prior: float | None) -> None:
    """Create a fresh meta with defaults if missing; otherwise ONLY refresh
    `drift_ssim_vs_prior`. Tag + exclusions are user data — leave them alone."""
    meta_path = day_dir / _MAC_META_FILE
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt %s; recreating from scratch", meta_path)
            meta = _fresh_mac_meta()
        if meta.get("schema_version") != MAC_META_SCHEMA_VERSION:
            raise ValueError(
                f"Unknown _mac_meta.json schema version "
                f"{meta.get('schema_version')!r} at {meta_path}"
            )
        meta["drift_ssim_vs_prior"] = _round_or_none(drift_ssim_vs_prior)
    else:
        meta = _fresh_mac_meta()
        meta["drift_ssim_vs_prior"] = _round_or_none(drift_ssim_vs_prior)

    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(meta_path)


def _fresh_mac_meta() -> dict[str, Any]:
    return {
        "schema_version": MAC_META_SCHEMA_VERSION,
        "tag": None,
        "tag_source": None,
        "excluded_images": [],
        "drift_ssim_vs_prior": None,
    }


def _round_or_none(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)
