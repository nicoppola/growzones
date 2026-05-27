# GrowZones — Mac side

Streamlit app + CLI for importing tar bundles from the Pi, culling bad frames,
processing per-day heatmaps, and viewing zone overlays.

See [`../PLAN.md`](../PLAN.md) for the architecture and full design.

## Install

Prerequisites: Python 3.11+ (`brew install python@3.11`), ffmpeg
(`brew install ffmpeg`).

```sh
cd mac/
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

```sh
streamlit run growzones/growzones_app.py
```
Opens `http://localhost:8501`.

The CLI is also available as `growzones <command> --location <name>` once
installed — see [PLAN.md](../PLAN.md) for the full command list.

## State on disk

Lives under `~/Library/Application Support/growzones/data/` (standard macOS
user-data location, included in Time Machine backups). The app creates it on
first run; nothing in this repo gets written to.

```
~/Library/Application Support/growzones/
└── data/
    ├── locations.json         # index of all locations
    ├── .last_location         # CLI default
    └── locations/
        └── <slug>/
            ├── captures/<date>/...
            └── results/...
```

## Layout

```
mac/
├── growzones/                # the Python package
│   ├── growzones_app.py      # Streamlit entrypoint
│   ├── pages/                # Streamlit multipage scripts (Locations, Import, Days, Cull, Process, Results)
│   ├── state.py              # session + last-location helpers
│   ├── cli.py                # Typer CLI (growzones <cmd>)
│   ├── locations.py          # locations.json + path resolver
│   ├── bundle.py             # tar import + thumbnails + drift SSIM
│   ├── auto_cull.py          # per-day median + diff scoring
│   ├── sun_mask.py           # HSV + variance/edge boost
│   ├── heatmap.py            # per-day accumulation + multi-day average
│   ├── zones.py              # K-means (k=4, random_state=42) + overlay
│   └── timelapse.py          # ffmpeg + PIL timestamp burn-in
└── pyproject.toml
```
