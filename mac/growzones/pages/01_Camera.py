"""Camera page — live feed, manual settings, optional auto-calibrate, capture session."""
from __future__ import annotations

import time

import streamlit as st

from growzones.pi_client import PiClient, PiError
from growzones.sidebar import pi_health, render_sidebar


st.set_page_config(page_title="Camera — GrowZones", layout="wide")
client = render_sidebar()
health = pi_health()


# ----- Header + live feed -----

st.title("Camera")

if health is None:
    st.error("Pi is offline. Fix the hostname in the sidebar or check the Pi service.")
    st.stop()

active_session_id = health.get("active_session_id")
has_profile = bool(health.get("has_profile"))
status_line = (
    f"🔴 Recording session `{active_session_id}`"
    if active_session_id
    else ("✅ Profile saved" if has_profile else "⚠️ No profile saved")
)
st.caption(status_line)

stream_url = client.stream_url()
st.markdown(
    f'<img src="{stream_url}" style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" alt="live feed">',
    unsafe_allow_html=True,
)


# ----- Defaults: prefer saved profile, then sensible neutrals -----

NEUTRAL = {
    "exposure_time_us": 10000,
    "analogue_gain": 1.0,
    "colour_gains": [1.7, 1.6],
    "scaler_crop": None,
}

profile = None
if has_profile:
    try:
        profile = client.get_profile()
    except PiError as e:
        st.error(str(e))


def _default(key: str):
    """Profile is the canonical 'saved' state; fall back to a neutral default
    if no profile exists yet. Used only to seed the sliders on first render —
    after that, slider state is owned by session_state."""
    if profile is not None and profile.get(key) is not None:
        return profile.get(key)
    return NEUTRAL.get(key)


# Native sensor dims for scaler_crop default
native_w = 3280
native_h = 2464
if profile is not None and profile.get("sensor"):
    native_w = profile["sensor"].get("native_width", native_w)
    native_h = profile["sensor"].get("native_height", native_h)


# ----- Manual settings (always visible) -----

st.divider()
st.subheader("Manual settings")
st.caption("Sliders apply to the live preview. **Save to profile** persists the values for capture sessions.")

cgains_default = _default("colour_gains") or [1.7, 1.6]
crop_default = _default("scaler_crop") or [0, 0, native_w, native_h]


def _push_to_sliders(values: dict) -> None:
    """Write a settings dict into the slider session_state keys. MUST be
    called before the sliders render — Streamlit disallows writes to a
    widget key after the widget has been instantiated in the same run.
    Buttons elsewhere on the page stash an override and rerun; this runs
    at the top of the next run to apply it.
    """
    st.session_state["slider_exposure"] = int(values.get("exposure_time_us") or 10000)
    st.session_state["slider_gain"] = float(values.get("analogue_gain") or 1.0)
    cg = values.get("colour_gains") or [1.7, 1.6]
    st.session_state["slider_red_gain"] = float(cg[0])
    st.session_state["slider_blue_gain"] = float(cg[1])
    sc = values.get("scaler_crop") or [0, 0, native_w, native_h]
    st.session_state["slider_crop_x"] = int(sc[0])
    st.session_state["slider_crop_y"] = int(sc[1])
    st.session_state["slider_crop_w"] = int(sc[2])
    st.session_state["slider_crop_h"] = int(sc[3])


# Apply any pending override stashed by a button handler in the previous run.
_pending = st.session_state.pop("pending_slider_override", None)
if _pending is not None:
    _push_to_sliders(_pending)

# Seed slider session-state on first render.
_slider_keys = {
    "slider_exposure": int(_default("exposure_time_us") or 10000),
    "slider_gain": float(_default("analogue_gain") or 1.0),
    "slider_red_gain": float(cgains_default[0]),
    "slider_blue_gain": float(cgains_default[1]),
    "slider_crop_x": int(crop_default[0]),
    "slider_crop_y": int(crop_default[1]),
    "slider_crop_w": int(crop_default[2]),
    "slider_crop_h": int(crop_default[3]),
}
for _k, _v in _slider_keys.items():
    st.session_state.setdefault(_k, _v)

with st.form("live_settings"):
    exposure = st.slider(
        "Exposure time (µs)",
        min_value=50, max_value=33000,
        step=50,
        key="slider_exposure",
    )
    gain = st.slider(
        "Analogue gain",
        min_value=1.0, max_value=8.0,
        step=0.1,
        key="slider_gain",
    )
    col_r, col_b = st.columns(2)
    red_gain = col_r.slider("Red gain (AWB)", 0.5, 4.0, step=0.05, key="slider_red_gain")
    blue_gain = col_b.slider("Blue gain (AWB)", 0.5, 4.0, step=0.05, key="slider_blue_gain")
    with st.expander("Sensor crop (advanced)", expanded=False):
        cx = st.number_input("x", step=1, key="slider_crop_x")
        cy = st.number_input("y", step=1, key="slider_crop_y")
        cw = st.number_input("width", step=1, key="slider_crop_w")
        ch = st.number_input("height", step=1, key="slider_crop_h")
    if st.form_submit_button("Apply to live", type="primary"):
        try:
            client.patch_settings({
                "exposure_time_us": int(exposure),
                "analogue_gain": float(gain),
                "colour_gains": [float(red_gain), float(blue_gain)],
                "scaler_crop": [int(cx), int(cy), int(cw), int(ch)],
            })
            st.toast("Live settings updated")
        except PiError as e:
            st.error(str(e))


btn_cols = st.columns(4)
if btn_cols[0].button("📸 Snapshot"):
    try:
        st.session_state["last_snapshot"] = client.snapshot()
    except PiError as e:
        st.error(str(e))
if btn_cols[1].button("💾 Save to profile"):
    try:
        client.save_settings_to_profile()
        st.success("Saved current live settings to profile")
        time.sleep(0.3)
        st.rerun()
    except PiError as e:
        st.error(str(e))
reset_disabled = profile is None
if btn_cols[2].button("↺ Reset to profile", disabled=reset_disabled, help=("No profile yet" if reset_disabled else None)):
    try:
        client.patch_settings({
            "exposure_time_us": profile.get("exposure_time_us"),
            "analogue_gain": profile.get("analogue_gain"),
            "colour_gains": profile.get("colour_gains"),
            "scaler_crop": profile.get("scaler_crop"),
            "lens_position": profile.get("lens_position"),
        })
        st.session_state["pending_slider_override"] = profile
        st.toast("Reset to saved profile")
        time.sleep(0.3)
        st.rerun()
    except PiError as e:
        st.error(str(e))
if btn_cols[3].button("🔧 Auto-calibrate…", help="Open the auto-calibrate wizard below"):
    st.session_state["calibrate_phase"] = "aim"

snap = st.session_state.get("last_snapshot")
if snap:
    st.image(snap, caption="Snapshot")


# ----- Auto-calibrate wizard (collapsed unless in flight) -----

calibrate_phase = st.session_state.get("calibrate_phase", "idle")

with st.expander("Auto-calibrate (optional starting point)", expanded=(calibrate_phase != "idle")):
    st.caption(
        "Auto-calibrate proposes exposure, gain, and white-balance values from the current scene. "
        "It's a starting point — you can still fine-tune with the sliders above before saving. "
        "Best results outdoors at midday under direct sun."
    )
    if calibrate_phase == "idle":
        if st.button("Run auto-calibrate", key="start_calibrate"):
            st.session_state["calibrate_phase"] = "aim"
            st.rerun()
    elif calibrate_phase == "aim":
        st.info("Aim the camera using the live feed above, then click Continue.")
        c1, c2 = st.columns([1, 4])
        if c1.button("Continue", type="primary", key="aim_continue"):
            try:
                client.start_calibrate()
                st.session_state["calibrate_phase"] = "running"
                st.rerun()
            except PiError as e:
                st.error(str(e))
        if c2.button("Cancel", key="aim_cancel"):
            st.session_state["calibrate_phase"] = "idle"
            st.rerun()
    elif calibrate_phase == "running":
        try:
            status = client.setup_status()
        except PiError as e:
            st.error(str(e))
            status = {}
        st.progress(status.get("progress", 0.0), text=status.get("message", "..."))
        st.caption(f"Step: **{status.get('step')}**")
        history = status.get("history") or []
        if history:
            for h in history[-6:]:
                st.write(f"`{h.get('step')}` — {h.get('message')}")
        state = status.get("state")
        if state == "done":
            st.session_state["calibrate_phase"] = "review"
            st.rerun()
        elif state == "error":
            st.error(status.get("error", "Calibration failed"))
            if st.button("Reset", key="error_reset"):
                st.session_state["calibrate_phase"] = "idle"
                st.rerun()
        else:
            time.sleep(1.0)
            st.rerun()
    elif calibrate_phase == "review":
        try:
            status = client.setup_status()
        except PiError as e:
            st.error(str(e))
            status = {}
        cand = status.get("candidate") or {}
        st.success(status.get("message") or "Calibration complete")
        rev_cols = st.columns(2)
        with rev_cols[0]:
            st.write("**Candidate values**")
            st.json({k: cand.get(k) for k in ("exposure_time_us", "analogue_gain", "colour_gains", "lens_position", "scaler_crop")})
        with rev_cols[1]:
            test_url = status.get("test_image_url")
            if test_url:
                st.image(f"{client.base_url()}{test_url}", caption="Test capture")
        act_cols = st.columns(3)
        if act_cols[0].button("Apply to sliders & save", type="primary", key="apply_and_save"):
            try:
                saved = client.save_calibration()
                # Stash the values; the top of the next rerun pushes them into
                # the slider session_state keys BEFORE the widgets render
                # (Streamlit forbids writing widget state after instantiation).
                st.session_state["pending_slider_override"] = saved
                st.session_state["calibrate_phase"] = "idle"
                st.success("Profile saved. Sliders now reflect the calibrated values.")
                time.sleep(0.5)
                st.rerun()
            except PiError as e:
                st.error(str(e))
        if act_cols[1].button("Re-run", key="rerun_calibrate"):
            try:
                client.start_calibrate()
                st.session_state["calibrate_phase"] = "running"
                st.rerun()
            except PiError as e:
                st.error(str(e))
        if act_cols[2].button("Discard", key="discard_calibrate"):
            st.session_state["calibrate_phase"] = "idle"
            st.rerun()


# ----- Capture session control -----

st.divider()
st.subheader("Capture session")

if active_session_id:
    sessions_list = []
    try:
        sessions_list = client.list_sessions()
    except PiError as e:
        st.error(str(e))
    active = next((s for s in sessions_list if s["id"] == active_session_id), None)
    if active is None:
        st.warning(f"Active session `{active_session_id}` not yet in list. Refresh shortly.")
    else:
        st.metric("Started at", active.get("started_at") or "—")
        a, b, c = st.columns(3)
        a.metric("Images captured", active.get("image_count", 0))
        b.metric("Size", f"{active.get('total_bytes', 0) / (1024**2):.1f} MB")
        c.metric("Interval (s)", active.get("interval_seconds", 0))
        if st.button("⏹ Stop session", type="primary"):
            try:
                client.stop_session(active_session_id)
                st.toast("Session stopped")
                time.sleep(0.5)
                st.rerun()
            except PiError as e:
                st.error(str(e))
else:
    if not has_profile:
        st.info("Save a profile (with the slider values + Save to profile) before starting a session.")
    else:
        with st.form("start_session"):
            interval = st.number_input(
                "Interval (seconds between captures)",
                min_value=5, max_value=3600,
                value=int(st.session_state.get("last_interval", 900)),
                step=5,
            )
            submit = st.form_submit_button("▶ Start session", type="primary")
            if submit:
                st.session_state["last_interval"] = int(interval)
                try:
                    client.start_session(int(interval))
                    st.toast("Session started")
                    time.sleep(0.5)
                    st.rerun()
                except PiError as e:
                    st.error(str(e))
