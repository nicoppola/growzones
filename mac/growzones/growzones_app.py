"""Streamlit entrypoint for the GrowZones Mac app.

Renders the sidebar location chooser (the one piece of UI shared by every
page) and a welcome card on the main area. The six workflow pages live under
`pages/` and are auto-discovered by Streamlit.

Run with:  streamlit run growzones_app.py
"""
from __future__ import annotations

import streamlit as st

from growzones import state
from growzones.locations import list_locations


st.set_page_config(page_title="GrowZones", layout="wide")


def _welcome_card() -> None:
    """Top-of-page intro shown when the user lands on the entrypoint."""
    st.title("GrowZones")
    st.write(
        "Map how much direct sunlight each spot on your balcony gets, using "
        "time-lapse photos from a Pi camera. Process them on your Mac to get "
        "a per-pixel sun-hours heatmap and a zone overlay you can compare "
        "against your eyeball memory of where the sun actually hits."
    )
    st.markdown("See [PLAN.md](../../PLAN.md) for the full design.")

    st.subheader("How to use")
    st.markdown(
        "1. **Pick or create a location** in the sidebar (a balcony, a back "
        "fence — whatever you're evaluating).\n"
        "2. **Import** a `.tar` bundle downloaded from the Pi's web UI.\n"
        "3. On the **Days** page, tag clear-sky days as `clear`.\n"
        "4. On the **Cull** page, drop any frames where you walked through "
        "or a bird flew past.\n"
        "5. On the **Process** page, pick a date range and click Run.\n"
        "6. View the heatmap, zone overlay, and timelapse on **Results**."
    )


def _get_started_card() -> None:
    """Shown when no locations exist yet — fresh-install state."""
    st.info(
        "No locations yet. A location is a named container for a set of "
        "captures (one physical camera position). Most people create one "
        "while importing their first bundle on the Import page; you can also "
        "create one manually on the Locations page."
    )


def main() -> None:
    loc = state.location_selector_sidebar()
    if loc is not None and loc.notes:
        st.sidebar.caption(loc.notes)

    _welcome_card()

    if not list_locations():
        _get_started_card()


main()
