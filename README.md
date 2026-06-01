# GrowZones

A Raspberry Pi Zero 2 W + camera that takes locked-exposure photos at a fixed
interval, plus a Mac Streamlit app that drives it over the LAN. End goal:
figure out where on your balcony plants get enough sun.

v2 is just the capture loop — live preview, calibration, sessions, download.
Image analysis (heatmaps, zone overlays, timelapses) is deferred to a later
version. See [`PLAN.md`](PLAN.md) for the full v2 design.

## Architecture

```
[Pi Zero 2 W]                          [Your Mac]
  picamera2                              Streamlit app — localhost:8501
  FastAPI  ───── HTTP / JSON ────►         Camera page · Bundles page
```

The Pi is a headless capture appliance. The Mac is the entire UI.

## Quickstart

### Mac

Prerequisite: Python 3.13 (`brew install python@3.13`).

```sh
./mac/install.sh        # or: make install-mac
make app                # launch Streamlit at http://localhost:8501
```

The first launch shows the home page. Open **Camera** in the sidebar to see
the live feed, run calibration, and start a capture session. Open **Bundles**
to download finished sessions to this Mac.

### Pi

1. Provision the SD card with Raspberry Pi Imager. Set hostname `growzones`,
   enable SSH, configure Wi-Fi, set your timezone.
2. Get the code onto the Pi:
   - `make pi-deploy PI_HOST=growzones.local` (rsync + run `install.sh`), or
   - clone the repo on the Pi and run `pi/install.sh` directly.
3. Verify from the Mac: `curl http://growzones.local/api/health`.

`pi/install.sh` is idempotent — re-run safely. It enables the camera,
installs the apt-side picamera2/numpy/pillow packages, creates a venv with
`--system-site-packages`, pip installs the Pi app, creates `/var/lib/growzones/`,
and installs + starts the systemd unit.

## Repo layout

```
.
├── PLAN.md             v2 design
├── Makefile            install-mac, app, pi-deploy, clean
├── pi/                 FastAPI capture appliance (runs on the Pi)
│   ├── app/            camera, mjpeg, calibrate, sessions, export, storage, main
│   ├── systemd/        growzones.service
│   └── install.sh      idempotent provisioning script
├── mac/                Streamlit app (runs on macOS)
│   └── growzones/
│       ├── growzones_app.py   entry script
│       ├── sidebar.py         Pi hostname + health badge
│       ├── pi_client.py       requests wrapper around the Pi API
│       ├── bundles.py         local bundle storage helpers
│       └── pages/
│           ├── 01_Camera.py
│           └── 02_Bundles.py
└── .gitignore
```

## State on disk

- **Pi**: `/var/lib/growzones/` (`camera_profile.json`, `setup_tests/`,
  `sessions/<id>/`)
- **Mac**: `~/Library/Application Support/growzones/` (`.pi_host`,
  `bundles/<id>/`)

## License

Personal project; no license file included. Treat as all-rights-reserved.
