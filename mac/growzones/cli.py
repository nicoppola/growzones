"""GrowZones Mac CLI.

Typer-based entry point exposing the same primitives the Streamlit app calls
under the hood. Day-to-day use is the app; the CLI is for scripting and for
the rare moment you want to run one stage in isolation.

Every subcommand accepts `--location <slug>`. If omitted, falls back to
`last_location()`; if there is no last location, the command errors. The
resolved location is then persisted as the new last location so subsequent
runs default to it.
"""
from __future__ import annotations

import json
from datetime import date as date_cls, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import auto_cull, bundle, heatmap, timelapse, zones
from .locations import (
    Location,
    create_location,
    delete_location,
    get_location,
    last_location,
    list_locations,
    location_exists,
    rename_location,
    set_last_location,
)

app = typer.Typer(help="GrowZones — Mac side CLI", no_args_is_help=True)
locations_app = typer.Typer(help="Manage locations", no_args_is_help=True)
app.add_typer(locations_app, name="locations")

console = Console()

MAC_META_SCHEMA = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_location(slug_or_none: str | None) -> Location:
    """Return the named location, or the last-used one, or exit with error.
    Always writes the resolved slug back as the new last_location."""
    if slug_or_none:
        if not location_exists(slug_or_none):
            typer.secho(
                f"Location {slug_or_none!r} does not exist. "
                f"Run `growzones locations list` to see what's available.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        loc = get_location(slug_or_none)
    else:
        loc = last_location()
        if loc is None:
            typer.secho(
                "No --location given and no default is set. "
                "Pass --location or create one with `growzones locations new`.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
    set_last_location(loc.slug)
    return loc


def _parse_date_range(value: str) -> tuple[str, str]:
    """Parse "YYYY-MM-DD..YYYY-MM-DD" into (from, to). Single "YYYY-MM-DD" is
    treated as a one-day range. Validates both dates parse and order is sane."""
    if ".." in value:
        parts = value.split("..", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise typer.BadParameter(
                f"Date range must look like YYYY-MM-DD..YYYY-MM-DD (got {value!r})"
            )
        date_from, date_to = parts[0], parts[1]
    else:
        date_from = date_to = value
    try:
        start = date_cls.fromisoformat(date_from)
        end = date_cls.fromisoformat(date_to)
    except ValueError as e:
        raise typer.BadParameter(f"Bad date in range {value!r}: {e}") from None
    if end < start:
        raise typer.BadParameter(
            f"Range end {date_to} is before start {date_from}"
        )
    return date_from, date_to


def _iter_dates(date_from: str, date_to: str):
    cur = date_cls.fromisoformat(date_from)
    end = date_cls.fromisoformat(date_to)
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _count_images(loc: Location) -> int:
    if not loc.captures_dir.exists():
        return 0
    return sum(
        1
        for day in loc.captures_dir.iterdir() if day.is_dir()
        for jpg in day.glob("*.jpg") if not jpg.name.startswith("_")
    )


def _last_import_date(loc: Location) -> str:
    """Most recent capture day under this location, or '—' if none."""
    if not loc.captures_dir.exists():
        return "—"
    dates = sorted(
        d.name for d in loc.captures_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )
    return dates[-1] if dates else "—"


# ---------------------------------------------------------------------------
# locations subcommands
# ---------------------------------------------------------------------------

@locations_app.command("list")
def locations_list() -> None:
    """Print all locations with image counts and last-imported date."""
    locs = list_locations()
    if not locs:
        typer.echo("No locations yet. Create one with `growzones locations new <name>`.")
        return
    table = Table(title="Locations")
    table.add_column("Name")
    table.add_column("Slug")
    table.add_column("Created")
    table.add_column("Last day", justify="right")
    table.add_column("Images", justify="right")
    for loc in locs:
        table.add_row(
            loc.name,
            loc.slug,
            loc.created_at.split("T")[0],
            _last_import_date(loc),
            str(_count_images(loc)),
        )
    console.print(table)


@locations_app.command("new")
def locations_new(
    name: str = typer.Argument(..., help="Display name (slug is derived)"),
    notes: str = typer.Option("", "--notes", help="Free-text notes about the spot"),
) -> None:
    """Create a new location."""
    loc = create_location(name, notes=notes)
    typer.secho(
        f"Created location {loc.name!r} (slug: {loc.slug})",
        fg=typer.colors.GREEN,
    )


@locations_app.command("rename")
def locations_rename(
    old_slug: str = typer.Argument(..., help="Existing location slug"),
    new_name: str = typer.Argument(..., help="New display name (slug stays the same)"),
) -> None:
    """Rename a location's display name. The slug is immutable."""
    try:
        loc = rename_location(old_slug, new_name)
    except KeyError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.secho(f"Renamed to {loc.name!r} (slug still {loc.slug})", fg=typer.colors.GREEN)


@locations_app.command("delete")
def locations_delete(
    slug: str = typer.Argument(..., help="Location slug to delete"),
    confirm: bool = typer.Option(False, "--confirm", help="Required: this is irrecoverable"),
) -> None:
    """Delete a location and ALL of its captures/results. Irrecoverable."""
    if not confirm:
        typer.secho(
            f"Refusing to delete {slug!r} without --confirm "
            "(this removes captures and results irrecoverably).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        result = delete_location(slug)
    except KeyError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.secho(f"Deleted location {result['name']!r} ({result['slug']})", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@app.command("import")
def import_bundle(
    bundle_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    location: str = typer.Option(..., "--location", help="Target location name or slug"),
    new: bool = typer.Option(
        False, "--new",
        help="Create the location first. Errors if it already exists.",
    ),
) -> None:
    """Import a Pi-export tar into a location."""
    if new:
        if location_exists(location):
            typer.secho(
                f"--new given but location {location!r} already exists.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        loc = create_location(location)
        typer.echo(f"Created location {loc.name!r} ({loc.slug})")
    else:
        if not location_exists(location):
            typer.secho(
                f"Location {location!r} does not exist. "
                "Pass --new to create it, or fix the typo.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        loc = get_location(location)

    set_last_location(loc.slug)
    result = bundle.import_bundle(bundle_path, loc)

    days_added = getattr(result, "days_added", None)
    images_added = getattr(result, "images_added", None)
    typer.secho(
        f"Imported {bundle_path.name} into {loc.name!r}: "
        f"{images_added if images_added is not None else '?'} images across "
        f"{days_added if days_added is not None else '?'} days.",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# auto-cull
# ---------------------------------------------------------------------------

@app.command("auto-cull")
def cmd_auto_cull(
    date: str = typer.Argument(..., help="YYYY-MM-DD"),
    location: str | None = typer.Option(None, "--location"),
    threshold: int = typer.Option(30, "--threshold", help="Diff score cutoff (0-255 V scale)"),
) -> None:
    """Run the auto-cull suggester for one day."""
    loc = _resolve_location(location)
    try:
        date_cls.fromisoformat(date)
    except ValueError as e:
        raise typer.BadParameter(f"Bad date {date!r}: {e}") from None
    result = auto_cull.auto_cull_day(loc, date, threshold=threshold)
    suggestions = result.get("suggestions", [])
    total = len(suggestions)
    flagged = sum(1 for s in suggestions if s.get("flagged"))
    typer.secho(
        f"Auto-culled {date}: {flagged}/{total} frames flagged "
        f"(threshold={threshold}).",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# tag-day
# ---------------------------------------------------------------------------

@app.command("tag-day")
def cmd_tag_day(
    date: str = typer.Argument(..., help="YYYY-MM-DD"),
    clear: bool = typer.Option(False, "--clear", help="Tag the day as a clear-sky day"),
    cloudy: bool = typer.Option(False, "--cloudy", help="Tag the day as cloudy"),
    untag: bool = typer.Option(False, "--untag", help="Remove any tag"),
    location: str | None = typer.Option(None, "--location"),
) -> None:
    """Set or clear a day's clear/cloudy tag (writes to `_mac_meta.json`)."""
    chosen = [name for name, flag in (("clear", clear), ("cloudy", cloudy), ("untag", untag)) if flag]
    if len(chosen) != 1:
        typer.secho(
            "Pass exactly one of --clear / --cloudy / --untag.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        date_cls.fromisoformat(date)
    except ValueError as e:
        raise typer.BadParameter(f"Bad date {date!r}: {e}") from None

    loc = _resolve_location(location)
    day_dir = loc.captures_dir / date
    if not day_dir.is_dir():
        typer.secho(
            f"No capture directory for {date} in {loc.slug!r}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    if untag:
        new_tag: str | None = None
        new_source: str | None = None
    else:
        new_tag = "clear" if clear else "cloudy"
        new_source = "user"

    _update_mac_meta(day_dir, tag=new_tag, tag_source=new_source)
    label = "untagged" if untag else f"tagged {new_tag}"
    typer.secho(f"{date} in {loc.slug!r}: {label}.", fg=typer.colors.GREEN)


def _update_mac_meta(day_dir: Path, *, tag: str | None, tag_source: str | None) -> None:
    """Read-modify-write `_mac_meta.json` atomically. Creates with defaults if missing."""
    path = day_dir / "_mac_meta.json"
    if path.exists():
        meta = json.loads(path.read_text())
        if meta.get("schema_version") != MAC_META_SCHEMA:
            raise ValueError(
                f"Unknown _mac_meta.json schema_version {meta.get('schema_version')!r}"
            )
    else:
        meta = {
            "schema_version": MAC_META_SCHEMA,
            "tag": None,
            "tag_source": None,
            "excluded_images": [],
            "drift_ssim_vs_prior": None,
        }
    meta["tag"] = tag
    meta["tag_source"] = tag_source
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# process (per-day + range averaging)
# ---------------------------------------------------------------------------

@app.command("process")
def cmd_process(
    range_str: str = typer.Argument(..., help="YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD"),
    location: str | None = typer.Option(None, "--location"),
) -> None:
    """Build per-day heatmaps for each day in range, then average them."""
    date_from, date_to = _parse_date_range(range_str)
    loc = _resolve_location(location)
    # Filter to days that actually have a captures dir — `_iter_dates` enumerates
    # the calendar; skipping missing days here keeps `process_day` from raising
    # FileNotFoundError on gaps in the user's capture history.
    candidate_dates = [
        d for d in _iter_dates(date_from, date_to)
        if (loc.captures_dir / d).is_dir()
    ]
    if not candidate_dates:
        typer.secho(
            f"No capture days found in {loc.slug!r} between {date_from} and {date_to}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(
        f"Processing {len(candidate_dates)} day(s) in {loc.slug!r}: "
        f"{date_from} → {date_to}"
    )
    for d in candidate_dates:
        typer.echo(f"  · {d}")
        try:
            heatmap.process_day(loc, d)
        except ValueError as e:
            # All frames excluded, or no frames in dir. Surface + continue.
            typer.secho(f"    skipped: {e}", fg=typer.colors.YELLOW)
    heatmap.process_range(loc, date_from, date_to)
    typer.secho("Process complete.", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# zones
# ---------------------------------------------------------------------------

@app.command("zones")
def cmd_zones(
    range_str: str = typer.Argument(..., help="YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD"),
    k: int = typer.Option(4, "--k", help="Number of zones (default 4)"),
    location: str | None = typer.Option(None, "--location"),
) -> None:
    """Cluster the averaged heatmap into k zones."""
    date_from, date_to = _parse_date_range(range_str)
    loc = _resolve_location(location)
    result = zones.compute_zones(loc, date_from, date_to, k=k, random_state=42)
    clear = result.get("clear_days_used")
    typer.secho(
        f"Zones computed (k={k}, clear days used={clear}) for "
        f"{date_from}..{date_to} in {loc.slug!r}.",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# timelapse
# ---------------------------------------------------------------------------

@app.command("timelapse")
def cmd_timelapse(
    range_str: str = typer.Argument(..., help="YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD"),
    day: str | None = typer.Option(None, "--day", help="Single day; overrides the range"),
    fps: int = typer.Option(8, "--fps", help="Output framerate (default 8)"),
    location: str | None = typer.Option(None, "--location"),
) -> None:
    """Build an h264 mp4 timelapse from non-excluded frames."""
    date_from, date_to = _parse_date_range(range_str)
    if day is not None:
        try:
            date_cls.fromisoformat(day)
        except ValueError as e:
            raise typer.BadParameter(f"Bad --day {day!r}: {e}") from None
    loc = _resolve_location(location)
    output = timelapse.make_timelapse(
        loc, date_from, date_to, fps=fps, single_day=day,
    )
    typer.secho(f"Timelapse written to {output}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
