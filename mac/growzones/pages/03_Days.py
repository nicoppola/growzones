"""Days page: per-day overview with clear/cloudy tagging and drift warnings.

The drift chip (when `drift_ssim_vs_prior` < 0.85) is the early-warning system
for camera-bump events — without it, a windstorm-jostled tripod would silently
poison every subsequent zone map.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import streamlit as st
from PIL import Image

from growzones import state


st.set_page_config(page_title="Days — GrowZones", layout="wide")

_TAG_OPTIONS = [None, "clear", "cloudy"]
_DRIFT_THRESHOLD = 0.85
_MAC_META_SCHEMA_VERSION = 1


def _tag_label(tag: str | None) -> str:
    return "—" if tag is None else tag


def _list_day_dirs(captures_dir: Path) -> list[Path]:
    """All YYYY-MM-DD subdirectories of `captures_dir`, sorted by date."""
    if not captures_dir.exists():
        return []
    days: list[Path] = []
    for child in captures_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            datetime.strptime(child.name, "%Y-%m-%d")
        except ValueError:
            continue
        days.append(child)
    return sorted(days, key=lambda p: p.name)


def _load_meta(day_dir: Path) -> dict:
    """Read _mac_meta.json or return a fresh default."""
    meta_path = day_dir / "_mac_meta.json"
    if not meta_path.exists():
        return {
            "schema_version": _MAC_META_SCHEMA_VERSION,
            "tag": None,
            "tag_source": None,
            "excluded_images": [],
            "drift_ssim_vs_prior": None,
        }
    data = json.loads(meta_path.read_text())
    if data.get("schema_version") != _MAC_META_SCHEMA_VERSION:
        raise ValueError(
            f"Unknown _mac_meta.json schema version "
            f"{data.get('schema_version')!r} at {meta_path}"
        )
    return data


def _save_meta(day_dir: Path, meta: dict) -> None:
    """Atomic write of _mac_meta.json."""
    meta_path = day_dir / "_mac_meta.json"
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(meta_path)


def _suggest_tag(jpegs: list[Path]) -> str | None:
    """Auto-suggest 'clear' / 'cloudy' from intraday brightness variance.

    Clear days have a strong moving-shadow signal → high variance of mean V
    across the day. Cloudy days are flat. Threshold picked by eyeballing —
    tunable later. Returns None when there aren't enough frames to decide.
    """
    if len(jpegs) < 4:
        return None
    means: list[float] = []
    for p in jpegs:
        try:
            with Image.open(p) as im:
                arr = np.asarray(im.convert("RGB").resize((160, 120)))
                means.append(float(arr.max(axis=2).mean()))
        except Exception:
            continue
    if len(means) < 4:
        return None
    spread = float(np.std(means))
    return "clear" if spread > 15.0 else "cloudy"


def _render_day_row(day_dir: Path) -> None:
    """Single row: date, image count, tag selector, drift chip."""
    date = day_dir.name
    jpegs = sorted(p for p in day_dir.glob("*.jpg") if not p.name.startswith("_"))
    meta = _load_meta(day_dir)

    suggested = meta.get("tag")
    if suggested is None and meta.get("tag_source") != "user":
        suggested = _suggest_tag(jpegs)

    with st.container(border=True):
        cols = st.columns([2, 1, 2, 2])
        cols[0].markdown(f"**{date}**")
        cols[1].metric("Images", len(jpegs))

        current_tag = meta.get("tag")
        index = _TAG_OPTIONS.index(current_tag) if current_tag in _TAG_OPTIONS else 0
        new_tag = cols[2].selectbox(
            "Tag",
            options=_TAG_OPTIONS,
            index=index,
            format_func=_tag_label,
            key=f"tag_{date}",
            help=(
                f"Auto-suggested: {_tag_label(suggested)}"
                if suggested is not None
                else "Tag this day to include or exclude it from processing."
            ),
        )
        if new_tag != current_tag:
            meta["tag"] = new_tag
            meta["tag_source"] = "user"
            _save_meta(day_dir, meta)
            st.toast(f"{date}: tagged {_tag_label(new_tag)}")

        drift = meta.get("drift_ssim_vs_prior")
        if drift is None:
            cols[3].caption("First day or no drift score")
        elif drift < _DRIFT_THRESHOLD:
            cols[3].warning(f"⚠ Camera may have moved (SSIM {drift:.2f})")
        else:
            cols[3].caption(f"Drift SSIM {drift:.2f}")


def main() -> None:
    state.location_selector_sidebar()
    loc = state.get_current_location()
    st.title(f"Days — {loc.name}")

    days = _list_day_dirs(loc.captures_dir)
    if not days:
        st.info(
            "No capture days yet for this location. Import a bundle on the "
            "Import page to populate it."
        )
        return

    st.caption(
        f"{len(days)} capture day(s). Tag clear-sky days as `clear` so they "
        "contribute to multi-day averaging on the Process page."
    )

    for day_dir in days:
        _render_day_row(day_dir)


main()
