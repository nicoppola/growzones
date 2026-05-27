"""Import page: upload a Pi-generated `growzones-export-*.tar` bundle and
merge it into the chosen Location.

The destination chooser is the most important UI on this page — every bundle
either accumulates into an existing location or creates a new one. After a
successful import, the sidebar selection auto-switches to the imported-into
location.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from growzones import locations, state
from growzones.locations import Location

try:
    from growzones.bundle import import_bundle, ImportResult
except ImportError:  # pragma: no cover — degraded mode if bundle.py is missing
    import_bundle = None
    ImportResult = None


st.set_page_config(page_title="Import — GrowZones", layout="wide")

_NEW_LOCATION_SENTINEL = "__new__"


def _destination_chooser() -> Location | None:
    """Prominent 'Add to:' dropdown plus inline new-location form.

    Returns the chosen Location, or None if the user picked '+ New location…'
    but hasn't submitted the form yet (so import should be disabled).
    """
    all_locs = locations.list_locations()
    options = [loc.slug for loc in all_locs] + [_NEW_LOCATION_SENTINEL]

    def _format(slug: str) -> str:
        if slug == _NEW_LOCATION_SENTINEL:
            return "+ New location…"
        return next(loc.name for loc in all_locs if loc.slug == slug)

    default_index = 0
    if not all_locs:
        default_index = options.index(_NEW_LOCATION_SENTINEL)

    selected = st.selectbox(
        "Add to",
        options=options,
        index=default_index,
        format_func=_format,
        key="import_destination",
    )

    if selected != _NEW_LOCATION_SENTINEL:
        return next(loc for loc in all_locs if loc.slug == selected)

    st.caption("Create a new location to import into.")
    name = st.text_input(
        "New location name",
        placeholder="e.g. Back fence",
        key="import_new_name",
    )
    notes = st.text_area(
        "Notes (optional)",
        placeholder="South-facing, awning bracket mount",
        key="import_new_notes",
    )
    # Returning None here is deliberate — caller treats it as "not ready yet";
    # location is only created on the Import-click path so a typo in the name
    # field doesn't litter the index with empty locations.
    return None


def _run_import(tar_path: Path, location: Location) -> "ImportResult | None":
    """Execute the import inside a spinner; return the result or None on error."""
    try:
        with st.spinner(f"Importing into {location.name!r}…"):
            return import_bundle(tar_path, location)
    except Exception as e:  # noqa: BLE001 — surface everything to the user
        st.error(f"Import failed: {e}")
        return None


def _show_import_summary(result: "ImportResult", location: Location) -> None:
    """Render counts + any drift warnings from the import result."""
    st.success(f"Imported into {location.name!r}.")
    cols = st.columns(5)
    cols[0].metric("Days added", result.days_added)
    cols[1].metric("Days updated", result.days_updated)
    cols[2].metric("Images added", result.images_added)
    cols[3].metric("Skipped (existing)", result.images_skipped_existing)
    cols[4].metric("Thumbnails", result.thumbnails_generated)

    if result.drift_warnings:
        dates = ", ".join(w["date"] for w in result.drift_warnings)
        st.warning(
            f"Camera may have moved on these days (low SSIM vs prior day): "
            f"{dates}. Check the Days page for details."
        )


def main() -> None:
    state.location_selector_sidebar()
    st.title("Import bundle")
    st.caption(
        "Drop a `growzones-export-*.tar` exported from the Pi's Data tab. "
        "Existing days get new images appended; your prior culling and tags "
        "are never touched."
    )

    if import_bundle is None:
        st.info("Bundle import is coming in a later milestone.")
        st.stop()

    destination = _destination_chooser()
    uploaded = st.file_uploader("Bundle file", type=["tar"])

    can_import = uploaded is not None and (
        destination is not None
        or (
            st.session_state.get("import_destination") == _NEW_LOCATION_SENTINEL
            and (st.session_state.get("import_new_name") or "").strip()
        )
    )

    if not st.button("Import", type="primary", disabled=not can_import):
        return

    # Resolve destination — create the new location now if that was the choice.
    if destination is None:
        try:
            destination = locations.create_location(
                st.session_state["import_new_name"],
                notes=st.session_state.get("import_new_notes", ""),
            )
        except ValueError as e:
            st.error(str(e))
            return

    # Stash the upload to a real path so tarfile can stream from disk.
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp.write(uploaded.getbuffer())
        tar_path = Path(tmp.name)

    try:
        result = _run_import(tar_path, destination)
    finally:
        tar_path.unlink(missing_ok=True)

    if result is None:
        return

    _show_import_summary(result, destination)
    state.set_current_location(destination.slug)
    st.toast(f"Switched to {destination.name!r}")


main()
