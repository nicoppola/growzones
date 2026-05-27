"""Results page: browse processed runs for the current location.

Each `stats_<from>_to_<to>.json` in the location's results dir is one
run — that JSON is the index; the matching heatmap PNG, zone overlay PNG,
and timelapse MP4 are derived by filename suffix. Save buttons download
the artifact bytes to wherever the user's browser is configured to
save downloads.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

import streamlit as st

from growzones import state


st.set_page_config(page_title="Results — GrowZones", layout="wide")

_STATS_RE = re.compile(r"^stats_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.json$")


def _list_runs(results_dir: Path) -> list[Path]:
    """Stats JSON files, newest first by mtime."""
    if not results_dir.exists():
        return []
    return sorted(
        (p for p in results_dir.glob("stats_*.json") if _STATS_RE.match(p.name)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _siblings(stats_path: Path) -> dict[str, Path]:
    """Resolve the heatmap PNG, zone overlay PNG, and timelapse MP4 for a run."""
    m = _STATS_RE.match(stats_path.name)
    if m is None:
        return {}
    start, end = m.group(1), m.group(2)
    results_dir = stats_path.parent
    return {
        "heatmap": results_dir / f"heatmap_{start}_to_{end}.png",
        "zones": results_dir / f"zones_{start}_to_{end}.png",
        "timelapse": results_dir / f"timelapse_{start}_to_{end}.mp4",
    }


def _render_zone_table(stats: dict) -> None:
    """Tabular per-zone summary from the stats JSON."""
    rows = []
    for z in stats.get("zones", []):
        rows.append({
            "Tier": z.get("label", f"zone {z.get('id', '?')}"),
            "Color": z.get("color_hex", ""),
            "Mean sun min/day": z.get("mean_sun_minutes_per_day"),
            "Pixel count": z.get("pixel_count"),
        })
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.caption("No zone stats in this run.")


def _download_button(label: str, path: Path, mime: str, key: str) -> None:
    """Render a download button for an artifact, or a stub if missing."""
    if not path.exists():
        st.caption(f"{label}: not generated")
        return
    with open(path, "rb") as f:
        st.download_button(
            label=f"Save {label}",
            data=f,
            file_name=path.name,
            mime=mime,
            key=key,
        )


def _render_run(stats_path: Path) -> None:
    """One run = heatmap + zone overlay + table + timelapse."""
    try:
        stats = json.loads(stats_path.read_text())
    except json.JSONDecodeError as e:
        st.error(f"Could not parse {stats_path.name}: {e}")
        return

    date_range = stats.get("date_range", ["?", "?"])
    clear_days = stats.get("clear_days_used", "?")
    k = stats.get("k", "?")

    with st.container(border=True):
        st.subheader(f"{date_range[0]} → {date_range[1]}")
        st.caption(
            f"k={k} · {clear_days} clear day(s) used · "
            f"computed {stats.get('computed_at', '?')}"
        )

        siblings = _siblings(stats_path)

        image_cols = st.columns(2)
        if siblings.get("heatmap") and siblings["heatmap"].exists():
            image_cols[0].image(
                str(siblings["heatmap"]),
                caption="Sun-hours heatmap",
                use_container_width=True,
            )
        else:
            image_cols[0].caption("Heatmap PNG missing.")

        if siblings.get("zones") and siblings["zones"].exists():
            image_cols[1].image(
                str(siblings["zones"]),
                caption="Zone overlay on reference frame",
                use_container_width=True,
            )
        else:
            image_cols[1].caption("Zone overlay PNG missing.")

        _render_zone_table(stats)

        timelapse = siblings.get("timelapse")
        if timelapse and timelapse.exists():
            st.video(str(timelapse))
        else:
            st.caption("Timelapse MP4 missing.")

        save_cols = st.columns(3)
        with save_cols[0]:
            _download_button(
                "heatmap",
                siblings.get("heatmap", Path()),
                "image/png",
                key=f"dl_heatmap_{stats_path.name}",
            )
        with save_cols[1]:
            _download_button(
                "zone overlay",
                siblings.get("zones", Path()),
                "image/png",
                key=f"dl_zones_{stats_path.name}",
            )
        with save_cols[2]:
            _download_button(
                "timelapse",
                siblings.get("timelapse", Path()),
                "video/mp4",
                key=f"dl_timelapse_{stats_path.name}",
            )


def main() -> None:
    state.location_selector_sidebar()
    loc = state.get_current_location()
    st.title(f"Results — {loc.name}")

    runs = _list_runs(loc.results_dir)
    if not runs:
        st.info(
            "No processing runs yet for this location. Tag some clear days "
            "on the Days page, then click Run on the Process page."
        )
        return

    st.caption(f"{len(runs)} run(s), newest first.")
    for stats_path in runs:
        _render_run(stats_path)


main()
