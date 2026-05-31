"""Capture scheduler control — proxies the Pi's `/api/capture/*` endpoints
into Streamlit so you don't have to switch tabs to `http://growzones.local/capture`.

Shows live scheduler state (today's saved/skipped, in-window?, current settings)
and lets you change the interval, capture window, and dark-skip threshold, plus
Start/Stop the scheduler. All state lives on the Pi; this page is a thin
client.
"""
from __future__ import annotations

import streamlit as st

from growzones.pi_client import (
    PiClient, PiError, load_pi_host, render_pi_error, save_pi_host,
)


st.set_page_config(page_title="Capture — GrowZones", layout="wide")

# Hard limits mirror pi/app/capture.py.
_MIN_INTERVAL = 30
_MAX_INTERVAL = 3600


def _safe_get(client: PiClient, path: str) -> tuple[dict | None, PiError | None]:
    """Return (payload, error). Pages render whatever they can and only flag
    the parts that failed."""
    try:
        return client.get(path), None
    except PiError as e:
        return None, e


def _hhmm_input(label: str, value: str, key: str) -> str:
    """Streamlit doesn't have a great HH:MM widget — use text_input with a
    light validator. Returns the user's value unchanged; validation happens
    on submit."""
    return st.text_input(label, value=value, key=key, max_chars=5,
                         help="24-hour HH:MM (Pi local time).")


def _valid_hhmm(s: str) -> bool:
    parts = s.split(":")
    if len(parts) != 2: return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= h < 24 and 0 <= m < 60


def main() -> None:
    st.title("Capture scheduler")
    st.caption(
        "Configure when and how often the Pi takes photos. All settings are "
        "stored on the Pi; this page is a thin client over `/api/capture/*`."
    )

    # --- Host ---------------------------------------------------------------
    saved_host = load_pi_host()
    host = st.text_input("Pi hostname", value=saved_host, key="capture_pi_host")
    if host and host != saved_host:
        save_pi_host(host)
    client = PiClient(host=host)

    # --- Live state + config ------------------------------------------------
    cfg, cfg_err = _safe_get(client, "/api/capture/config")
    state_payload, state_err = _safe_get(client, "/api/capture/state")

    if cfg_err or state_err:
        render_pi_error(st, host, cfg_err or state_err)
        return

    assert cfg is not None and state_payload is not None
    scheduler_state = state_payload.get("scheduler", {})
    today = scheduler_state.get("today_stats", {})

    # --- Status banner ------------------------------------------------------
    st.subheader("Status")
    s_cols = st.columns(4)
    running = scheduler_state.get("running", False)
    in_window = scheduler_state.get("in_window")
    s_cols[0].metric("Scheduler", "Running" if running else "Stopped")
    s_cols[1].metric(
        "In window?",
        "Yes" if in_window else "No" if in_window is not None else "—",
    )
    s_cols[2].metric("Saved today", today.get("saved", 0))
    s_cols[3].metric(
        "Skipped today",
        today.get("skipped_dark", 0)
            + today.get("skipped_low_disk", 0)
            + today.get("skipped_out_of_window", 0),
        help=(
            f"dark={today.get('skipped_dark', 0)} · "
            f"low-disk={today.get('skipped_low_disk', 0)} · "
            f"out-of-window={today.get('skipped_out_of_window', 0)} · "
            f"errors={today.get('errors', 0)}"
        ),
    )

    if st.button("Refresh", key="refresh_state"):
        st.rerun()

    # --- Start / Stop -------------------------------------------------------
    st.divider()
    st.subheader("Run")
    rc1, rc2 = st.columns(2)
    if rc1.button("Start scheduler", type="primary", disabled=running):
        try:
            client.post("/api/capture/start")
            st.success("Started.")
            st.rerun()
        except PiError as e:
            if e.status == 409:
                st.error(
                    "Pi refused to start: no camera profile saved. "
                    f"Open http://{host}/setup to run the calibration wizard first."
                )
            else:
                st.error(str(e))
    if rc2.button("Stop scheduler", disabled=not running):
        try:
            client.post("/api/capture/stop")
            st.success("Stopped.")
            st.rerun()
        except PiError as e:
            st.error(str(e))

    # --- Config form --------------------------------------------------------
    st.divider()
    st.subheader("Settings")
    with st.form("capture_cfg"):
        interval = st.number_input(
            "Interval (seconds)",
            min_value=_MIN_INTERVAL,
            max_value=_MAX_INTERVAL,
            value=int(cfg.get("interval_seconds", 900)),
            step=30,
            help=f"How often the scheduler fires ({_MIN_INTERVAL}–{_MAX_INTERVAL}s).",
        )
        c1, c2 = st.columns(2)
        with c1:
            window_start = _hhmm_input("Window start", cfg.get("window_start", "05:00"), "window_start")
        with c2:
            window_stop = _hhmm_input("Window stop", cfg.get("window_stop", "22:00"), "window_stop")
        dark = st.slider(
            "Dark-skip threshold (mean V, 0–255)",
            min_value=0, max_value=255,
            value=int(cfg.get("dark_skip_threshold", 30)),
            help="Frames whose mean V is below this are discarded as nighttime.",
        )
        submitted = st.form_submit_button("Save to Pi", type="primary")

    if submitted:
        if not _valid_hhmm(window_start) or not _valid_hhmm(window_stop):
            st.error("Window times must be HH:MM (24-hour).")
        else:
            try:
                updated = client.patch(
                    "/api/capture/config",
                    {
                        "interval_seconds": int(interval),
                        "window_start": window_start,
                        "window_stop": window_stop,
                        "dark_skip_threshold": int(dark),
                    },
                )
                st.success(
                    f"Saved. Effective config: every {updated['interval_seconds']}s "
                    f"between {updated['window_start']} and {updated['window_stop']}."
                )
            except PiError as e:
                st.error(str(e))


main()
