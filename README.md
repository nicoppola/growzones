# GrowZones

A Raspberry Pi + Mac project for mapping how much direct sunlight each spot on
your balcony actually gets across the day, so you can place plants where they
will thrive.

A Pi Zero 2 W with a camera takes locked-exposure photos at fixed intervals;
a Streamlit app on your Mac imports the bundles, runs a sun-mask + multi-day
average, and produces a colored "zone" overlay you can read against your
eyeball memory of where the sun actually hits.

## Architecture

```
[Pi Zero 2 W]                                  [Your Mac]
  picamera2     ─┐                              ┌── Streamlit at localhost:8501
  FastAPI       ─┤  live preview                │     Locations · Import · Days
  APScheduler   ─┤  scheduled captures          │     Cull · Process · Results
  storage       ─┘  tar export                  │
                                                │  + `growzones` CLI
       ▲                                        │  + per-day heatmaps, k=4 zones,
       │  HTTP (LAN) ─── .tar export ──►        │    h264 timelapse
       │                                        │
   You ── browser                               └── data: ~/Library/App Support/growzones/
```

Pi is a capture appliance — live view + scheduled JPEGs + tar download.
Mac is the data-exploration surface — import, cull, process, view results.
Data flows one direction only: Pi → tar → Mac.

See [`PLAN.md`](PLAN.md) for the full design (architecture, schemas,
algorithm choices, the rationale behind every interesting decision).

## Quickstart

### Mac side

Prerequisites: Python 3.13 (`brew install python@3.13`) and ffmpeg
(`brew install ffmpeg`).

```sh
./mac/install.sh     # or: make install-mac
make smoke           # run the end-to-end pipeline against a synthetic bundle
make app             # launch Streamlit at http://localhost:8501
make cli             # show the growzones CLI help
```

`mac/install.sh` is idempotent — re-run safely. It checks prerequisites,
creates `mac/.venv`, installs the package in editable mode, and verifies
imports.

The smoke test is genuinely end-to-end (synthesizes a Pi-style tar, imports it,
runs auto-cull → tag → heatmap → zones → timelapse). If `make smoke` passes,
the Mac side is healthy.

### Pi side

1. Provision the SD card with Raspberry Pi Imager — set hostname `growzones`,
   enable SSH, configure Wi-Fi, set your timezone. See PLAN.md
   "Installation prerequisites → A. Provision the SD card" for the full
   walkthrough.

2. Get the code onto the Pi — pick whichever you prefer:
   - **Clone the repo on the Pi:** `git clone <repo-url>` somewhere, then
     `cd <clone>/pi && ./install.sh`
   - **rsync just the Pi tree from your Mac:** `make pi-deploy PI_HOST=growzones.local`

   `pi/install.sh` is path-agnostic — it substitutes the actual project
   path into the systemd unit at install time, so it works regardless of
   where the repo or `pi/` tree lives on the Pi.

3. Open `http://growzones.local/` — first load redirects to the Setup tab.

## Repo layout

```
.
├── PLAN.md             single source of truth for the design
├── Makefile            install-mac, smoke, app, cli, pi-deploy, clean
├── pi/                 FastAPI capture appliance (runs on the Pi)
│   ├── app/            camera, mjpeg, capture, storage, export, setup_wizard
│   ├── systemd/        growzones.service
│   ├── install.sh      idempotent provisioning script
│   └── README.md
├── mac/                Streamlit + CLI (runs on macOS)
│   ├── growzones/      Python package
│   │   ├── growzones_app.py
│   │   ├── pages/      01_Locations … 06_Results
│   │   └── *.py        locations, bundle, sun_mask, auto_cull, heatmap,
│   │                   zones, timelapse, cli, state
│   ├── smoke_test.py   end-to-end pipeline test against synthetic data
│   └── README.md
├── docs/
│   └── CAMERA_SETUP.md walkthrough + troubleshooting for the calibration wizard
└── .gitignore
```

## State on disk

- **Pi**: `/var/lib/growzones/` (camera_profile.json, setup_tests/, captures/)
- **Mac**: `~/Library/Application Support/growzones/data/` (locations.json,
  per-location captures + results)

Nothing in this repo is written to at runtime; both apps write only to their
respective state dirs.

## License

This is a personal project; no license file is included. Treat as
"all rights reserved" unless you're me. If you fork it for your own balcony,
have fun.
