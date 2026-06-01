"""Streamlit entrypoint for the GrowZones Mac app.

Run with:  streamlit run growzones/growzones_app.py
"""
from __future__ import annotations

import streamlit as st

from growzones import bundles
from growzones.sidebar import render_sidebar


st.set_page_config(page_title="GrowZones", layout="wide")
render_sidebar()

st.title("GrowZones")
st.markdown(
    "Pi-based sun mapping for balcony plant planning. "
    "Use the **Camera** page to see the live feed, run auto-calibrate, "
    "tweak settings, and start a capture session. Use the **Bundles** page "
    "to download finished sessions to this Mac."
)

col_local, col_paths = st.columns(2)
with col_local:
    st.subheader("Local bundles")
    local = bundles.list_local_bundles()
    if not local:
        st.caption("No bundles downloaded yet.")
    else:
        mb = bundles.total_local_bytes() / (1024**2)
        st.caption(f"{len(local)} bundle(s) · {mb:.1f} MB total")
        for b in local[:5]:
            st.write(f"• `{b['id']}` — {b['image_count']} images")
with col_paths:
    st.subheader("Data location")
    st.code(str(bundles.data_dir()), language="text")
