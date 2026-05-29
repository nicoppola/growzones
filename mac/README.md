# GrowZones — Mac side

Streamlit app + Typer CLI for importing tar bundles from the Pi, culling bad
frames, processing per-day heatmaps, and viewing zone overlays.

See [`../PLAN.md`](../PLAN.md) for the architecture and full design.

## Install

Prerequisites: Python 3.13 (`brew install python@3.13`) and ffmpeg
(`brew install ffmpeg`).

```sh
./mac/install.sh
```

Idempotent — re-run safely. It verifies prerequisites, creates `mac/.venv`,
installs the package in editable mode, and confirms the package imports.

Equivalent from the repo root: `make install-mac`.

## Run

```sh
make app             # Streamlit, http://localhost:8501
make cli             # growzones --help
make smoke           # end-to-end pipeline against synthetic data
```

All three activate the venv automatically.

## CLI

```sh
growzones locations list
growzones locations new "Balcony" --notes "South-facing, awning bracket"
growzones import path/to/growzones-export-2026-05-26.tar --location balcony
growzones auto-cull 2026-05-26 --location balcony
growzones tag-day 2026-05-26 --clear --location balcony
growzones process 2026-05-20..2026-05-26 --location balcony
growzones zones 2026-05-20..2026-05-26 --k 4 --location balcony
growzones timelapse 2026-05-20..2026-05-26 --location balcony
```

`--location` defaults to the last one you used (persisted in `data/.last_location`).

## State on disk

Lives under `~/Library/Application Support/growzones/data/` (standard macOS
user-data location, included in Time Machine backups). Created on first run;
nothing in this repo is written to.

```
~/Library/Application Support/growzones/data/
├── locations.json            # name → slug index
├── .last_location            # CLI default
└── locations/<slug>/
    ├── captures/<date>/
    │   ├── <HH-MM-SS>.jpg          # raw, from Pi
    │   ├── _pi_capture_log.jsonl   # from Pi (settings used + timestamps)
    │   ├── _thumbs/                # generated on import (480×360)
    │   ├── _mac_meta.json          # tag, exclusions, drift SSIM
    │   ├── _cull_suggestions.json  # output of auto_cull
    │   ├── _median_v.npy           # cache for SSIM drift compare
    │   └── _heatmap.npz            # per-day sun_minutes cache
    └── results/
        ├── heatmap_<from>_to_<to>.png
        ├── zones_<from>_to_<to>.png
        ├── stats_<from>_to_<to>.json
        └── timelapse_<from>_to_<to>.mp4
```

## Layout

```
mac/
├── growzones/                  # the Python package (pip install -e .)
│   ├── growzones_app.py        # Streamlit entrypoint
│   ├── pages/                  # Streamlit auto-discovered pages
│   │   ├── 01_Locations.py
│   │   ├── 02_Import.py
│   │   ├── 03_Days.py
│   │   ├── 04_Cull.py
│   │   ├── 05_Process.py
│   │   ├── 06_Results.py
│   │   └── 07_Pi.py            # deploy / status / logs (operational)
│   ├── state.py                # st.session_state + .last_location helpers
│   ├── cli.py                  # Typer CLI (growzones <cmd>)
│   ├── locations.py            # locations.json + path resolver
│   ├── bundle.py               # tar import + thumbnails + drift SSIM
│   ├── auto_cull.py            # per-day median + diff scoring
│   ├── sun_mask.py             # HSV + variance/edge boost
│   ├── heatmap.py              # per-day accumulation + multi-day average
│   ├── zones.py                # K-means (k=4, random_state=42) + overlay
│   └── timelapse.py            # ffmpeg + PIL timestamp burn-in
├── smoke_test.py               # synthetic-bundle end-to-end test
└── pyproject.toml
```

## Smoke test

`smoke_test.py` exists to surface interface bugs before you have real captures:

1. Redirects `DATA_ROOT` to a temp dir.
2. Synthesizes a 4-day Pi-style tar (10 frames/day, a moving "sun spot").
3. Runs the full pipeline: import → re-import (idempotency) → auto-cull →
   tag as clear → multi-day average → K-means zones → timelapse.
4. Asserts every expected file exists and the stats payload has the right
   shape, sort order, and clear-day count.

Outputs are left in a temp dir for inspection — see the path it prints at exit.

Run after any refactor:

```sh
make smoke
```
