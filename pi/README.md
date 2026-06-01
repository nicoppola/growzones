# GrowZones — Pi side

Capture appliance: live MJPEG, per-session timed JPEGs with locked exposure,
and a tar-export endpoint the Mac app pulls from. No web UI on the Pi — the
Mac Streamlit app is the only client.

See [`../PLAN.md`](../PLAN.md) for the architecture.

## Install

Provision the SD card with Raspberry Pi Imager (hostname `growzones`, enable
SSH, set Wi-Fi + timezone). Then pick one:

```sh
# Option A: clone the repo on the Pi
ssh pi@growzones.local
git clone <repo-url> ~/growzones-repo
cd ~/growzones-repo/pi
./install.sh
```

```sh
# Option B: rsync the pi/ tree from your Mac
make pi-deploy PI_HOST=growzones.local
```

`install.sh` is idempotent — safe to re-run. It substitutes the actual project
path into the systemd unit so the repo can live anywhere on the Pi.

Verify from your Mac:

```sh
curl http://growzones.local/api/health
# → {"ok": true, "has_profile": false, ...}
```

Then launch the Mac app and use the Camera page to calibrate.

## Endpoints

| Path                                     | What                                |
|------------------------------------------|-------------------------------------|
| `GET  /stream.mjpg`                      | Live MJPEG                          |
| `GET  /api/health`                       | health + has_profile + free bytes   |
| `*    /api/camera/...`                   | profile, live settings, snapshot    |
| `POST /api/setup/calibrate` + `GET status` | calibration wizard               |
| `*    /api/sessions[/{id}[/stop|export]]` | session lifecycle + tar export      |

See [`../PLAN.md`](../PLAN.md) for full schema details.

## Layout

```
pi/
├── app/
│   ├── main.py        FastAPI factory + routes + CORS
│   ├── camera.py      picamera2 wrapper, shared lock
│   ├── mjpeg.py       /stream.mjpg multipart generator
│   ├── calibrate.py   background calibration pipeline + status tracker
│   ├── sessions.py    session lifecycle + interval worker
│   ├── export.py      streaming tar
│   └── storage.py     /var/lib/growzones/ layout helpers
├── systemd/growzones.service
├── install.sh
└── pyproject.toml     fastapi + uvicorn only (picamera2/numpy/pillow from apt)
```

## State on disk

```
/var/lib/growzones/
├── camera_profile.json
├── setup_tests/
└── sessions/
    └── <session_id>/
        ├── manifest.json
        ├── capture_log.jsonl
        └── HH-MM-SS.jpg
```

## Troubleshooting

- **`/api/health` doesn't respond** — `systemctl status growzones` to check
  the service; `journalctl -u growzones -f` to follow logs. mDNS:
  `dns-sd -G v4 growzones.local` (Mac) or `avahi-resolve -n growzones.local`
  (Pi).
- **`picamera2` import error inside the venv** — the venv must be created
  with `--system-site-packages` so apt-installed picamera2 is visible.
  `install.sh` does this; if you created the venv by hand, recreate it.
- **Camera not detected** — `libcamera-hello --list-cameras` should list it.
  If empty, check the CSI ribbon orientation.
- **Calibration starts but never finishes** — check the calibration step
  history via `GET /api/setup/status` from the Mac, or `journalctl` on the
  Pi. The wizard caps exposure iterations at 6 and saves anyway with a
  warning if it's still clipping.

## Development

```sh
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Use port 8000 in dev so the systemd service can keep port 80.
