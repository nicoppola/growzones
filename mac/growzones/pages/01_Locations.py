"""Manage view for Locations: list, create, rename, edit notes, delete.

Creation is also possible from the Import page (the more common entry point);
this page exists so you can clean up after typos and add notes any time.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from growzones import locations, state
from growzones.locations import Location


st.set_page_config(page_title="Locations — GrowZones", layout="wide")


def _image_count(loc: Location) -> int:
    """Walk the location's captures dir and count .jpg files."""
    captures = loc.captures_dir
    if not captures.exists():
        return 0
    return sum(
        1
        for day_dir in captures.iterdir()
        if day_dir.is_dir()
        for jpg in day_dir.glob("*.jpg")
    )


def _render_create_form() -> None:
    """Form for creating a brand-new location."""
    with st.expander("Create new location", expanded=False):
        with st.form("create_location_form", clear_on_submit=True):
            name = st.text_input("Name", placeholder="e.g. Back fence")
            notes = st.text_area(
                "Notes (optional)",
                placeholder="South-facing, awning bracket mount",
            )
            submitted = st.form_submit_button("Create")
        if not submitted:
            return
        try:
            loc = locations.create_location(name, notes=notes)
        except ValueError as e:
            st.error(str(e))
            return
        state.set_current_location(loc.slug)
        st.toast(f"Created {loc.name!r}")
        st.rerun()


def _render_location_row(loc: Location) -> None:
    """Single editable row per location: rename, edit notes, delete."""
    img_count = _image_count(loc)
    with st.container(border=True):
        cols = st.columns([3, 2, 1, 3])
        cols[0].markdown(f"**{loc.name}**  \n`{loc.slug}`")
        cols[1].caption(f"Created: {loc.created_at}")
        cols[2].metric("Images", img_count)
        cols[3].caption(loc.notes or "_no notes_")

        with st.expander("Edit / delete"):
            new_name = st.text_input(
                "Rename",
                value=loc.name,
                key=f"rename_{loc.slug}",
            )
            new_notes = st.text_area(
                "Notes",
                value=loc.notes,
                key=f"notes_{loc.slug}",
            )
            save_col, delete_col = st.columns(2)

            if save_col.button("Save", key=f"save_{loc.slug}"):
                try:
                    if new_name != loc.name:
                        locations.rename_location(loc.slug, new_name)
                    if new_notes != loc.notes:
                        locations.update_location_notes(loc.slug, new_notes)
                except (KeyError, ValueError) as e:
                    st.error(str(e))
                else:
                    st.toast(f"Saved {new_name!r}")
                    st.rerun()

            confirm = delete_col.checkbox(
                "I understand this deletes all captures and results",
                key=f"confirm_delete_{loc.slug}",
            )
            if delete_col.button(
                "Delete",
                key=f"delete_{loc.slug}",
                type="primary",
                disabled=not confirm,
            ):
                try:
                    locations.delete_location(loc.slug)
                except KeyError as e:
                    st.error(str(e))
                else:
                    st.toast(f"Deleted {loc.name!r}")
                    st.rerun()


def main() -> None:
    state.location_selector_sidebar()
    st.title("Locations")
    st.caption(
        "Each location is a named container for captures, culling decisions, "
        "and results. They are fully independent — deleting one leaves the "
        "others untouched."
    )

    _render_create_form()

    all_locs = locations.list_locations()
    if not all_locs:
        st.info("No locations yet — use the form above or import a bundle.")
        return

    st.divider()
    for loc in all_locs:
        _render_location_row(loc)


main()
