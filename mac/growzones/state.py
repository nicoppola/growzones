"""Streamlit session state helpers — the single place every page reads the
current Location from.

Each Streamlit browser tab gets its own `st.session_state`, so two tabs can
hold independent selections (that's what users expect from tabs). On cold start
we fall back to `data/.last_location` (written by `set_current_location`) so
the very first page render after launch already has a location selected.
"""
from __future__ import annotations

import streamlit as st

from . import locations
from .locations import Location

_SLUG_KEY = "current_location_slug"
_NEW_LOCATION_SENTINEL = "__new__"


def get_current_location() -> Location:
    """Resolve the location for this Streamlit session.

    Order of preference: session_state → persisted .last_location. Halts the
    page render with a friendly prompt if no locations exist yet, so individual
    pages don't have to special-case the "fresh install" state.
    """
    slug = st.session_state.get(_SLUG_KEY)
    if slug is None:
        slug = locations.last_location_slug()
        if slug is not None:
            st.session_state[_SLUG_KEY] = slug

    if slug is not None:
        try:
            return locations.get_location(slug)
        except KeyError:
            # Persisted slug points at a deleted location; clear and fall through.
            st.session_state.pop(_SLUG_KEY, None)

    if not locations.list_locations():
        st.warning(
            "No locations yet. Open the Import page to create your first "
            "one by importing a bundle, or use the Locations page to add one "
            "manually."
        )
        st.stop()

    # Locations exist but none selected — pick the first deterministically.
    first = locations.list_locations()[0]
    set_current_location(first.slug)
    return first


def set_current_location(slug: str) -> None:
    """Update both the session state and the on-disk last-location marker."""
    locations.set_last_location(slug)
    st.session_state[_SLUG_KEY] = slug


def location_selector_sidebar() -> Location | None:
    """Render the sidebar location chooser.

    Returns the selected Location, or None when the user has opened the
    "+ New location…" inline form and not yet created it.
    """
    all_locs = locations.list_locations()

    if not all_locs:
        st.sidebar.info("No locations yet — create one below.")
        return _render_new_location_form()

    current_slug = st.session_state.get(_SLUG_KEY) or locations.last_location_slug()
    options = [loc.slug for loc in all_locs] + [_NEW_LOCATION_SENTINEL]
    default_index = options.index(current_slug) if current_slug in options else 0

    def _format(slug: str) -> str:
        if slug == _NEW_LOCATION_SENTINEL:
            return "+ New location…"
        return next(loc.name for loc in all_locs if loc.slug == slug)

    selected = st.sidebar.selectbox(
        "Location",
        options=options,
        index=default_index,
        format_func=_format,
        key="_location_selector",
    )

    if selected == _NEW_LOCATION_SENTINEL:
        return _render_new_location_form()

    if selected != current_slug:
        set_current_location(selected)
    return locations.get_location(selected)


def _render_new_location_form() -> Location | None:
    """Inline name input + Create button. Returns the created Location, or None."""
    with st.sidebar.form("_new_location_form", clear_on_submit=True):
        name = st.text_input("Location name", placeholder="e.g. Back fence")
        notes = st.text_input("Notes (optional)", placeholder="South-facing, awning bracket")
        submitted = st.form_submit_button("Create")
    if not submitted:
        return None
    try:
        loc = locations.create_location(name, notes=notes)
    except ValueError as e:
        st.sidebar.error(str(e))
        return None
    set_current_location(loc.slug)
    st.sidebar.success(f"Created {loc.name!r}")
    st.rerun()
    return loc
