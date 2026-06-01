"""Shared Streamlit sidebar — Pi hostname, health badge, deploy button.

Every page calls `render_sidebar()` at the top; the helper returns a PiClient
bound to the currently-configured host so the page can make API calls.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import streamlit as st

from growzones import bundles
from growzones.pi_client import PiClient, PiError


REPO_ROOT = Path(__file__).resolve().parents[2]


def render_sidebar() -> PiClient:
    st.sidebar.title("GrowZones")
    saved_host = bundles.load_pi_host()
    host = st.sidebar.text_input(
        "Pi hostname",
        value=st.session_state.get("pi_host", saved_host),
        help="Hostname or IP of your Pi (mDNS makes `growzones.local` work on a home LAN).",
    )
    st.session_state["pi_host"] = host
    if host != saved_host:
        bundles.save_pi_host(host)

    client = PiClient(host)
    badge_col, label_col, refresh_col = st.sidebar.columns([1, 4, 1])
    badge_slot = badge_col.empty()
    label_slot = label_col.empty()
    caption_slot = st.sidebar.empty()
    if refresh_col.button("↻", key="refresh_health", help="Re-check Pi status"):
        st.rerun()

    badge_slot.markdown("⏳")
    label_slot.markdown("_checking…_")
    caption_slot.caption(f"Pinging {host}…")

    try:
        info = client.health()
        badge_slot.markdown("🟢")
        label = "online"
        if info.get("active_session_id"):
            label += " · capturing"
        elif info.get("has_profile"):
            label += " · ready"
        else:
            label += " · not calibrated"
        label_slot.markdown(label)
        free_gb = info.get("pi_free_bytes", 0) / (1024**3)
        caption_slot.caption(f"{free_gb:.1f} GB free on Pi")
        st.session_state["pi_health"] = info
    except PiError as e:
        badge_slot.markdown("🔴")
        label_slot.markdown("offline")
        caption_slot.caption(str(e))
        st.session_state["pi_health"] = None

    _render_deploy(host)
    return client


def _render_deploy(host: str) -> None:
    if st.sidebar.button("⬆ Deploy code to Pi", use_container_width=True):
        st.session_state["deploy_running"] = True
        st.session_state["deploy_output"] = ""

    if st.session_state.get("deploy_running"):
        with st.sidebar.status(f"Deploying to {host}…", expanded=True) as status:
            output_slot = st.empty()
            lines: list[str] = []
            try:
                proc = subprocess.Popen(
                    ["make", "-C", str(REPO_ROOT), "pi-deploy", f"PI_HOST={host}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,  # line-buffered
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line.rstrip())
                    output_slot.code("\n".join(lines[-15:]), language="text")
                rc = proc.wait()
                st.session_state["deploy_output"] = "\n".join(lines)
                if rc == 0:
                    status.update(label=f"Deployed to {host}", state="complete")
                else:
                    status.update(label=f"Deploy failed (exit {rc})", state="error")
            except FileNotFoundError as e:
                st.session_state["deploy_output"] = f"make not found: {e}"
                status.update(label="make not found", state="error")
        st.session_state["deploy_running"] = False

    output = st.session_state.get("deploy_output")
    if output:
        with st.sidebar.expander("Last deploy output", expanded=False):
            st.code(output, language="text")


def pi_health() -> dict | None:
    return st.session_state.get("pi_health")
