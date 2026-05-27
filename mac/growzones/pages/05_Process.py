"""Process page: orchestrates the per-day → multi-day → zones → timelapse
pipeline for the current location's selected date range.

This is the only page that calls multiple core modules in sequence — it's the
orchestrator. The underlying primitives (`heatmap.process_day`, `zones.compute_zones`,
`timelapse.make_timelapse`) are also runnable from the CLI for scripting.
"""
from __future__ import annotations

import json
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path

import streamlit as st

from growzones import state

try:
    from growzones.heatmap import process_day, process_range
except ImportError:  # pragma: no cover
    process_day = None
    process_range = None

try:
    from growzones.zones import compute_zones
except ImportError:  # pragma: no cover
    compute_zones = None

try:
    from growzones.timelapse import make_timelapse
except ImportError:  # pragma: no cover
    make_timelapse = None


st.set_page_config(page_title="Process — GrowZones", layout="wide")

_MAC_META_SCHEMA_VERSION = 1
_MIN_CLEAR_DAYS_FOR_TRUST = 3


def _load_meta(day_dir: Path) -> dict | None:
    """Return the parsed _mac_meta.json or None if missing."""
    meta_path = day_dir / "_mac_meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return None


def _all_dates(captures_dir: Path) -> list[date_cls]:
    """Every YYYY-MM-DD subdir of captures, parsed."""
    if not captures_dir.exists():
        return []
    out: list[date_cls] = []
    for child in captures_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            out.append(datetime.strptime(child.name, "%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(out)


def _clear_dates(captures_dir: Path) -> list[date_cls]:
    """Only the dates whose _mac_meta.json tag == 'clear'."""
    out: list[date_cls] = []
    for d in _all_dates(captures_dir):
        meta = _load_meta(captures_dir / d.strftime("%Y-%m-%d"))
        if meta and meta.get("tag") == "clear":
            out.append(d)
    return out


def _dates_in_range(
    captures_dir: Path, start: date_cls, end: date_cls
) -> list[date_cls]:
    """Capture dates in [start, end] (inclusive)."""
    return [d for d in _all_dates(captures_dir) if start <= d <= end]


def _clear_dates_in_range(
    captures_dir: Path, start: date_cls, end: date_cls
) -> list[date_cls]:
    return [d for d in _clear_dates(captures_dir) if start <= d <= end]


def _run_pipeline(
    loc, start: date_cls, end: date_cls, k: int, dates_to_process: list[date_cls]
) -> bool:
    """Run process_day for each date → process_range → compute_zones →
    make_timelapse. Returns True on full success."""
    if process_day is None or process_range is None:
        st.info("Per-day processing is coming in a later milestone.")
        return False

    progress = st.progress(0.0, text="Processing per-day heatmaps…")
    total = max(len(dates_to_process), 1)
    for i, d in enumerate(dates_to_process):
        try:
            process_day(loc, d.strftime("%Y-%m-%d"))
        except Exception as e:  # noqa: BLE001
            st.error(f"process_day failed for {d}: {e}")
            return False
        progress.progress((i + 1) / total, text=f"Processed {d}")
    progress.empty()

    try:
        with st.spinner("Averaging clear-day heatmaps…"):
            process_range(loc, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    except ValueError as e:
        st.error(f"Multi-day averaging failed: {e}")
        return False

    if compute_zones is None:
        st.warning("Zone computation isn't available yet — skipping.")
    else:
        try:
            with st.spinner(f"Clustering into {k} zones…"):
                compute_zones(
                    loc,
                    start.strftime("%Y-%m-%d"),
                    end.strftime("%Y-%m-%d"),
                    k=k,
                )
        except Exception as e:  # noqa: BLE001
            st.error(f"compute_zones failed: {e}")
            return False

    if make_timelapse is None:
        st.warning("Timelapse generation isn't available yet — skipping.")
    else:
        try:
            with st.spinner("Rendering timelapse (this can take a minute)…"):
                make_timelapse(
                    loc,
                    start.strftime("%Y-%m-%d"),
                    end.strftime("%Y-%m-%d"),
                )
        except Exception as e:  # noqa: BLE001
            st.error(f"make_timelapse failed: {e}")
            return False

    return True


def main() -> None:
    state.location_selector_sidebar()
    loc = state.get_current_location()
    st.title(f"Process — {loc.name}")

    all_dates = _all_dates(loc.captures_dir)
    if not all_dates:
        st.info("No capture days yet. Import a bundle on the Import page.")
        return

    clear_all = _clear_dates(loc.captures_dir)
    default_start = clear_all[0] if clear_all else all_dates[0]
    default_end = clear_all[-1] if clear_all else all_dates[-1]

    picked = st.date_input(
        "Date range",
        value=(default_start, default_end),
        min_value=all_dates[0],
        max_value=all_dates[-1],
        key="process_range",
    )
    if not isinstance(picked, tuple) or len(picked) != 2:
        st.caption("Pick a start and end date.")
        return
    start, end = picked
    if start > end:
        st.error("Start date must be on or before end date.")
        return

    in_range = _dates_in_range(loc.captures_dir, start, end)
    clear_in_range = _clear_dates_in_range(loc.captures_dir, start, end)

    summary_cols = st.columns(3)
    summary_cols[0].metric("Capture days in range", len(in_range))
    summary_cols[1].metric("Clear days in range", len(clear_in_range))
    summary_cols[2].metric(
        "Min for trustworthy avg", _MIN_CLEAR_DAYS_FOR_TRUST
    )

    if len(clear_in_range) == 0:
        st.error(
            "No `clear`-tagged days in this range. Tag at least one day as "
            "`clear` on the Days page first."
        )
        return
    if len(clear_in_range) < _MIN_CLEAR_DAYS_FOR_TRUST:
        st.warning(
            f"Only {len(clear_in_range)} clear day(s) in range. Results "
            "won't be trustworthy yet; aim for at least "
            f"{_MIN_CLEAR_DAYS_FOR_TRUST}."
        )

    k = st.slider("Number of zones (k)", min_value=2, max_value=8, value=4)

    if not st.button("Run", type="primary"):
        return

    ok = _run_pipeline(loc, start, end, k, dates_to_process=in_range)
    if ok:
        st.success("Processing complete.")
        st.page_link("pages/06_Results.py", label="View Results →")


main()
