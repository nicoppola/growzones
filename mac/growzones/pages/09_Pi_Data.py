"""Pi Data — browse what the Pi has captured, pull bundles down, and
optionally hand them straight to `bundle.import_bundle()` so you don't have
to download a tar manually and drag it into the Import page.

Proxies the Pi's `/api/storage`, `/api/days`, `/api/export*`, and `DELETE
/api/days*` endpoints. The Import page remains the manual upload fallback.
"""
from __future__ import annotations

import tempfile
from datetime import date as _date
from pathlib import Path

import streamlit as st

from growzones import locations as locations_mod, state
from growzones.bundle import import_bundle
from growzones.pi_client import (
    PiClient, PiError, load_pi_host, render_pi_error, save_pi_host,
)


st.set_page_config(page_title="Pi Data — GrowZones", layout="wide")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int | float | None) -> str:
    if n is None: return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_duration(seconds: int | float | None) -> str:
    if seconds is None: return "—"
    seconds = int(seconds)
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# ---------------------------------------------------------------------------
# Two-step confirm: first click sets a flag in session_state, second click runs.
# ---------------------------------------------------------------------------

def _confirm_button(label: str, key: str, *, danger_label: str | None = None) -> bool:
    """Returns True only on the second click in a row (with a Cancel option in
    between). Resets after the action."""
    armed_key = f"_armed::{key}"
    if not st.session_state.get(armed_key):
        if st.button(label, key=key):
            st.session_state[armed_key] = True
            st.rerun()
        return False
    c1, c2 = st.columns(2)
    confirmed = c1.button(danger_label or f"Confirm: {label}", key=f"{key}::confirm", type="primary")
    if c2.button("Cancel", key=f"{key}::cancel"):
        st.session_state[armed_key] = False
        st.rerun()
    if confirmed:
        st.session_state[armed_key] = False
        return True
    return False


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def main() -> None:
    state.location_selector_sidebar()
    st.title("Pi Data")
    st.caption(
        "Browse what the Pi has captured, pull bundles directly into a "
        "location, and delete days you don't need. All actions hit the Pi's "
        "JSON API — same surface as the Pi's own `/data` page."
    )

    # --- Host ---------------------------------------------------------------
    saved_host = load_pi_host()
    host = st.text_input("Pi hostname", value=saved_host, key="pi_data_host")
    if host and host != saved_host:
        save_pi_host(host)
    client = PiClient(host=host)

    # --- Disk usage banner --------------------------------------------------
    try:
        usage = client.get("/api/storage")
    except PiError as e:
        render_pi_error(st, host, e)
        return

    u_cols = st.columns(3)
    u_cols[0].metric("Disk free", _fmt_bytes(usage.get("disk_free_bytes")))
    u_cols[1].metric("Disk used", _fmt_bytes(usage.get("disk_used_bytes")))
    u_cols[2].metric("Captures size", _fmt_bytes(usage.get("captures_total_bytes")))

    days = usage.get("by_day") or []
    if not days:
        st.info("No captures on the Pi yet. Start the scheduler from the Capture page.")
        return

    # --- Days table ---------------------------------------------------------
    st.divider()
    st.subheader(f"{len(days)} day{'s' if len(days) != 1 else ''} on disk")
    st.dataframe(
        [
            {
                "date": d["date"],
                "images": d["image_count"],
                "size": _fmt_bytes(d["total_bytes"]),
            }
            for d in days
        ],
        hide_index=True,
        use_container_width=True,
    )

    earliest, latest = days[0]["date"], days[-1]["date"]

    # --- Pull range ---------------------------------------------------------
    st.divider()
    st.subheader("Pull bundle")
    r_cols = st.columns(2)
    with r_cols[0]:
        date_from = st.date_input(
            "From", value=_parse_iso(earliest), key="pi_data_from",
            min_value=_parse_iso(earliest), max_value=_parse_iso(latest),
        )
    with r_cols[1]:
        date_to = st.date_input(
            "To", value=_parse_iso(latest), key="pi_data_to",
            min_value=_parse_iso(earliest), max_value=_parse_iso(latest),
        )
    range_from, range_to = str(date_from), str(date_to)
    if range_from > range_to:
        st.error("'From' must be on or before 'To'.")
        return

    # Pre-flight size
    if st.button("Estimate size", key="estimate_size"):
        try:
            size_info = client.get("/api/export/size", **{"from_": range_from, "to": range_to})
            st.info(
                f"**{size_info['day_count']} days** · "
                f"**{size_info['image_count']} images** · "
                f"**{_fmt_bytes(size_info['bytes'])}** · "
                f"~{_fmt_duration(size_info['estimated_seconds_at_50mbps'])} on a 50 Mbps LAN."
            )
        except PiError as e:
            st.error(str(e))

    # Pull + import
    all_locs = locations_mod.list_locations()
    if not all_locs:
        st.warning(
            "No locations exist. Create one on the **Locations** page (or by "
            "importing a tar manually on the **Import** page) before pulling."
        )
    else:
        slugs = [l.slug for l in all_locs]
        cur_slug = state.get_current_location().slug
        default = slugs.index(cur_slug) if cur_slug in slugs else 0
        dest_slug = st.selectbox(
            "Import into location",
            options=slugs,
            index=default,
            format_func=lambda s: next(l.name for l in all_locs if l.slug == s),
            key="pi_data_dest",
        )

        if st.button("Pull + import", type="primary", key="pull_import"):
            dest_location = locations_mod.get_location(dest_slug)
            with st.status(f"Pulling {range_from} … {range_to} from {host}…", expanded=True) as status:
                with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    status.write("Downloading tar…")
                    written = client.stream_tar(
                        "/api/export", tmp_path,
                        from_=range_from, to=range_to,
                    )
                    status.write(f"Got {_fmt_bytes(written)}; importing…")
                    result = import_bundle(tmp_path, dest_location)
                except PiError as e:
                    status.update(label="Pull failed", state="error")
                    render_pi_error(st, host, e)
                    return
                except Exception as e:  # bundle.py raises ValueError on bad manifests etc.
                    status.update(label="Import failed", state="error")
                    st.error(f"Import failed: {e}")
                    return
                finally:
                    tmp_path.unlink(missing_ok=True)
                status.update(label="Done", state="complete")
            st.success(
                f"Imported into **{dest_location.name}**: "
                f"{result.days_added} new day(s), {result.days_updated} updated, "
                f"{result.images_added} images added, "
                f"{result.images_skipped_existing} already present."
            )
            if result.drift_warnings:
                st.warning(
                    f"{len(result.drift_warnings)} drift warning(s) — see Days page."
                )

    # --- Delete -------------------------------------------------------------
    st.divider()
    st.subheader("Delete from Pi")
    st.caption(
        "Frees space on the Pi. Already-imported data on the Mac is untouched."
    )

    d_cols = st.columns(2)
    with d_cols[0]:
        st.markdown("**Single day**")
        delete_day = st.selectbox(
            "Day to delete",
            options=[d["date"] for d in days],
            key="pi_data_del_day",
        )
        if _confirm_button(
            f"Delete {delete_day}",
            key=f"del_day::{delete_day}",
            danger_label=f"Confirm delete {delete_day}",
        ):
            try:
                resp = client.delete(f"/api/days/{delete_day}", confirm="true")
                st.success(
                    f"Deleted {resp.get('image_count', 0)} images "
                    f"({_fmt_bytes(resp.get('bytes_freed', 0))})."
                )
                st.rerun()
            except PiError as e:
                render_pi_error(st, host, e)

    with d_cols[1]:
        st.markdown("**Date range**")
        rc1, rc2 = st.columns(2)
        with rc1:
            del_from = st.date_input(
                "From", value=_parse_iso(earliest), key="pi_data_del_from",
                min_value=_parse_iso(earliest), max_value=_parse_iso(latest),
            )
        with rc2:
            del_to = st.date_input(
                "To", value=_parse_iso(latest), key="pi_data_del_to",
                min_value=_parse_iso(earliest), max_value=_parse_iso(latest),
            )
        if str(del_from) > str(del_to):
            st.error("'From' must be on or before 'To'.")
        elif _confirm_button(
            f"Delete {del_from}…{del_to}",
            key=f"del_range::{del_from}::{del_to}",
            danger_label=f"Confirm delete {del_from} … {del_to}",
        ):
            try:
                resp = client.delete(
                    "/api/days",
                    **{"from_": str(del_from), "to": str(del_to), "confirm": "true"},
                )
                st.success(
                    f"Deleted {resp.get('day_count', 0)} days, "
                    f"{resp.get('image_count', 0)} images "
                    f"({_fmt_bytes(resp.get('bytes_freed', 0))})."
                )
                st.rerun()
            except PiError as e:
                render_pi_error(st, host, e)


def _parse_iso(s: str) -> _date:
    return _date.fromisoformat(s)


main()
