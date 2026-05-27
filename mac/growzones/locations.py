"""Locations: a Location is a named container for captures, culling decisions,
and processing results. Locations are 100% a Mac concept — the Pi just captures
whatever it's pointed at, and on import we decide which Location the bundle
belongs to.

State lives under ~/Library/Application Support/growzones/data/ — see PLAN.md
"Mac-side data layout". This module is the single source of truth for resolving
that path; never use bare relative paths anywhere else in the codebase.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DATA_ROOT = Path.home() / "Library/Application Support/growzones" / "data"
LOCATIONS_INDEX = DATA_ROOT / "locations.json"
LAST_LOCATION_FILE = DATA_ROOT / ".last_location"
LOCATIONS_INDEX_SCHEMA = 1

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def growzones_data_dir() -> Path:
    """Resolve (and create) the Mac-side data root. Use everywhere instead of
    CWD-relative paths so the app works regardless of where streamlit was
    launched."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    return DATA_ROOT


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Lowercase → non-[a-z0-9] → '-' → collapse runs → strip."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a slug from name {name!r}")
    return slug


def _slug_with_dedupe(name: str, existing: set[str]) -> str:
    """Slugify then append -2, -3, … on collision. Slugs are directory names
    and never change after creation; renames only touch `name`."""
    base = slugify(name)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


# ---------------------------------------------------------------------------
# Location model
# ---------------------------------------------------------------------------

@dataclass
class Location:
    name: str
    slug: str
    created_at: str
    notes: str = ""

    @property
    def dir(self) -> Path:
        return growzones_data_dir() / "locations" / self.slug

    @property
    def captures_dir(self) -> Path:
        return self.dir / "captures"

    @property
    def results_dir(self) -> Path:
        return self.dir / "results"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Index persistence
# ---------------------------------------------------------------------------

def _load_index() -> dict:
    if not LOCATIONS_INDEX.exists():
        return {"schema_version": LOCATIONS_INDEX_SCHEMA, "locations": []}
    data = json.loads(LOCATIONS_INDEX.read_text())
    if data.get("schema_version") != LOCATIONS_INDEX_SCHEMA:
        raise ValueError(
            f"Unknown locations.json schema version {data.get('schema_version')!r}"
        )
    return data


def _save_index(data: dict) -> None:
    growzones_data_dir()
    tmp = LOCATIONS_INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(LOCATIONS_INDEX)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_locations() -> list[Location]:
    return [Location(**loc) for loc in _load_index()["locations"]]


def get_location(slug: str) -> Location:
    for loc in list_locations():
        if loc.slug == slug:
            return loc
    raise KeyError(f"Location {slug!r} not found")


def location_exists(slug: str) -> bool:
    try:
        get_location(slug)
        return True
    except KeyError:
        return False


def create_location(name: str, notes: str = "") -> Location:
    if not name.strip():
        raise ValueError("name cannot be blank")
    data = _load_index()
    existing_slugs = {loc["slug"] for loc in data["locations"]}
    slug = _slug_with_dedupe(name, existing_slugs)
    loc = Location(
        name=name.strip(),
        slug=slug,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        notes=notes,
    )
    loc.dir.mkdir(parents=True, exist_ok=True)
    loc.captures_dir.mkdir(parents=True, exist_ok=True)
    loc.results_dir.mkdir(parents=True, exist_ok=True)
    data["locations"].append(loc.to_dict())
    _save_index(data)
    set_last_location(slug)
    return loc


def rename_location(slug: str, new_name: str) -> Location:
    if not new_name.strip():
        raise ValueError("new_name cannot be blank")
    data = _load_index()
    for entry in data["locations"]:
        if entry["slug"] == slug:
            entry["name"] = new_name.strip()
            _save_index(data)
            return Location(**entry)
    raise KeyError(f"Location {slug!r} not found")


def update_location_notes(slug: str, notes: str) -> Location:
    data = _load_index()
    for entry in data["locations"]:
        if entry["slug"] == slug:
            entry["notes"] = notes
            _save_index(data)
            return Location(**entry)
    raise KeyError(f"Location {slug!r} not found")


def delete_location(slug: str) -> dict:
    """Remove the index entry AND the whole location directory tree.
    Deletes captures, results, everything — irrecoverable."""
    data = _load_index()
    for i, entry in enumerate(data["locations"]):
        if entry["slug"] == slug:
            loc = Location(**entry)
            if loc.dir.exists():
                shutil.rmtree(loc.dir)
            del data["locations"][i]
            _save_index(data)
            if last_location_slug() == slug:
                LAST_LOCATION_FILE.unlink(missing_ok=True)
            return {"slug": slug, "name": loc.name}
    raise KeyError(f"Location {slug!r} not found")


# ---------------------------------------------------------------------------
# Last-selected location (used by CLI default + Streamlit cold-start)
# ---------------------------------------------------------------------------

def last_location_slug() -> str | None:
    if not LAST_LOCATION_FILE.exists():
        return None
    return LAST_LOCATION_FILE.read_text().strip() or None


def set_last_location(slug: str) -> None:
    if not location_exists(slug):
        raise KeyError(f"Cannot set last_location to {slug!r}: doesn't exist")
    growzones_data_dir()
    LAST_LOCATION_FILE.write_text(slug)


def last_location() -> Location | None:
    """Resolve the persisted slug to a Location, or None if unset/stale."""
    slug = last_location_slug()
    if slug is None:
        return None
    try:
        return get_location(slug)
    except KeyError:
        return None
