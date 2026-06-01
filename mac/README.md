# GrowZones — Mac side

Streamlit app that drives the Pi capture appliance. Live MJPEG, auto-
calibrate wizard, settings sliders, capture session control, bundle browser.

See [`../PLAN.md`](../PLAN.md) for the architecture.

## Install

Prerequisite: Python 3.13 (`brew install python@3.13`).

```sh
./mac/install.sh
```

Idempotent — re-run safely. Creates `mac/.venv`, installs the package in
editable mode, verifies imports.

Equivalent from the repo root: `make install-mac`.

## Run

```sh
make app             # Streamlit at http://localhost:8501
```

In the sidebar, enter the Pi hostname (default `growzones.local`). A green
dot means the Pi is reachable.

## Pages

- **Camera** — Live MJPEG feed at the top. Below it:
  - **Settings & calibration**: if no profile exists, a one-button auto-
    calibrate wizard (aim → run → save). If a profile exists, sliders for
    exposure / gain / colour gains / crop, plus Snapshot, Save-to-profile,
    Reset, and Re-run calibrate.
  - **Capture session**: start a new session by setting an interval and
    clicking Start. Watch images accumulate; click Stop when done.
- **Bundles** — Merged table of Pi sessions + local bundles. Per row:
  Download (if not on Mac yet), Open folder (if on Mac), Delete on Pi
  (two-step arm/confirm).

## State on disk

Lives under `~/Library/Application Support/growzones/`:

```
~/Library/Application Support/growzones/
├── .pi_host                       # last-used Pi hostname
└── bundles/
    └── <session_id>/
        ├── manifest.json
        ├── capture_log.jsonl
        └── HH-MM-SS.jpg
```

Nothing in this repo is written to.

## Layout

```
mac/
├── growzones/
│   ├── growzones_app.py    Streamlit entry
│   ├── sidebar.py          Pi hostname + health badge
│   ├── pi_client.py        requests wrapper around the Pi API
│   ├── bundles.py          local bundle storage helpers
│   └── pages/
│       ├── 01_Camera.py
│       └── 02_Bundles.py
└── pyproject.toml          streamlit, pillow, requests
```
