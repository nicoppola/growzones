# GrowZones — Pi side

Capture appliance: live MJPEG preview, scheduled JPEG captures with locked
exposure, and a tar-export endpoint the Mac app pulls from.

See [`../PLAN.md`](../PLAN.md) for the architecture and the full installation
walkthrough (Raspberry Pi Imager → SSH → `install.sh`).

## TL;DR install

After provisioning the SD card with Raspberry Pi Imager (see PLAN.md
"Installation prerequisites → A. Provision the SD card"), from your Mac:

```sh
scp -r pi/ pi@growzones.local:/home/pi/growzones
ssh pi@growzones.local
cd /home/pi/growzones
./install.sh
```

Then open `http://growzones.local/` in your browser. First load redirects to
the Setup tab if no camera profile exists.

## Layout

```
pi/
├── app/                  # FastAPI app (main, camera, capture, storage, setup_wizard)
│   ├── templates/        # Jinja2 server-rendered tabs (setup, live, capture, data)
│   └── static/           # tiny JS for sliders + SSE narration
├── systemd/
│   └── growzones.service # installed to /etc/systemd/system/ by install.sh
├── install.sh            # idempotent — re-run safely
└── pyproject.toml        # pip deps (picamera2 comes from apt, not pip)
```

## State on disk

All Pi-writable state lives under `/var/lib/growzones/`:

```
/var/lib/growzones/
├── camera_profile.json       # produced by Setup wizard
├── setup_tests/              # test captures from the wizard
└── captures/
    └── YYYY-MM-DD/
        ├── HH-MM-SS.jpg
        └── _pi_capture_log.jsonl
```

## Troubleshooting

- **`http://growzones.local/` won't load** — confirm the service is up:
  `systemctl status growzones`, and that mDNS resolves:
  `dns-sd -G v4 growzones.local` (on Mac) or `avahi-resolve -n growzones.local`
  (on Pi).
- **App errors** — `journalctl -u growzones -f` to follow logs.
- **`picamera2` import error inside the venv** — the venv must be created with
  `--system-site-packages` so apt-installed picamera2 is visible. `install.sh`
  handles this; if you created the venv by hand, recreate it.
- **Camera not detected** — `libcamera-hello --list-cameras` should list it.
  If empty, check the CSI ribbon orientation (Pi Zero uses the narrow 22-pin
  connector — needs the right adapter cable).
- **Capture scheduler refuses to start** — that's by design when
  `camera_profile.json` is missing. Open the Setup tab to calibrate.

## Development

```sh
# On the Pi (or a Linux box with picamera2 + a camera attached):
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Use port 8000 in development so the systemd service can keep port 80.
