# GrowZones v2 — design

## What this is

A Raspberry Pi Zero 2 W with a camera that takes locked-exposure photos at a
fixed interval, plus a Streamlit app on the Mac that drives the Pi over the
LAN. The Mac app is the entire UI: live preview, calibration, settings, start
/ stop a capture session, browse and download the resulting bundles.

The longer-term goal is sun mapping for plant placement — figure out which
spots on the balcony get the most direct light. v2 only does the **capture
side** of that. Image analysis (sun masks, heatmaps, zone overlays,
timelapses) is intentionally deferred.

## Architecture

```
[Pi Zero 2 W]                          [Your Mac]
  picamera2                              Streamlit app at localhost:8501
  FastAPI       ── HTTP / JSON ────►       Camera page · Bundles page
  /stream.mjpg                             ~/Library/Application Support/
  /api/sessions                              growzones/bundles/<id>/
```

- **Pi is a headless capture appliance.** No web UI on the Pi itself.
- **Mac is the entire user surface.** Talks to the Pi over LAN.
- Data flows one direction: Pi → tar → Mac. The Mac never pushes captured
  data back; it only PATCHes settings and starts/stops sessions.
- LAN only, no auth.

## Pi side (`pi/`)

FastAPI service running as systemd on port 80.

Modules (`pi/app/`):
- `camera.py` — picamera2 wrapper, single shared threading.Lock, preview ↔
  still mode switching. Has a stub mode (no hardware) so the package imports
  cleanly on the Mac.
- `mjpeg.py` — `/stream.mjpg` multipart/x-mixed-replace, ~5 fps.
- `calibrate.py` — runs on a background thread. Sensor detect, lock focus
  (AF cameras), iterative exposure (target 99th-percentile V ≤ 250, halve
  gain then exposure, max 6 iterations), lock white balance, save a test
  capture. Mac polls `/api/setup/status` at ~1 Hz.
- `sessions.py` — one active session at a time. Worker thread drives an
  interval timer; each tick captures a full-res still, dark-frame skips if
  mean V < 30, writes the JPEG + a JSONL log line.
- `export.py` — streaming uncompressed tar of a session.
- `storage.py` — `/var/lib/growzones/` layout.
- `main.py` — FastAPI app, CORS=*, lifespan loads profile.

Endpoints (JSON unless noted):

| Method | Path                                     | Purpose                                |
|--------|------------------------------------------|----------------------------------------|
| GET    | `/stream.mjpg`                           | Live MJPEG                             |
| GET    | `/api/health`                            | `{ok, has_profile, pi_free_bytes, active_session_id}` |
| GET    | `/api/camera/profile`                    | Saved profile (404 if none)            |
| GET    | `/api/camera/settings`                   | Currently applied live settings        |
| PATCH  | `/api/camera/settings`                   | Update live settings                   |
| POST   | `/api/camera/settings/save-to-profile`   | Persist live settings → profile        |
| POST   | `/api/camera/snapshot`                   | One full-res JPEG, with current live settings |
| POST   | `/api/setup/calibrate`                   | Start the calibration pipeline         |
| GET    | `/api/setup/status`                      | Poll-friendly progress + candidate     |
| POST   | `/api/setup/save-profile`                | Persist the calibration candidate      |
| GET    | `/api/setup/test-image/{name}`           | Serve a test capture from the wizard   |
| POST   | `/api/sessions`                          | Start a session (`{interval_seconds}`) |
| POST   | `/api/sessions/{id}/stop`                | Stop the active session                |
| GET    | `/api/sessions`                          | List all sessions                      |
| GET    | `/api/sessions/{id}/export`              | Stream the session tar                 |
| DELETE | `/api/sessions/{id}`                     | Remove the session on the Pi           |

State layout under `/var/lib/growzones/`:
```
camera_profile.json
setup_tests/<timestamp>.jpg
sessions/
  <session_id>/
    manifest.json
    capture_log.jsonl
    HH-MM-SS.jpg
    ...
```

## Mac side (`mac/growzones/`)

Streamlit multi-page app.

Modules:
- `pi_client.py` — `requests` wrapper around every endpoint. `PiClient(host)`.
  Errors: `PiUnreachable`, `PiHttpError`.
- `bundles.py` — local bundle storage under
  `~/Library/Application Support/growzones/bundles/<id>/`. `import_tar` for
  downloaded bundles; helpers to list / open in Finder / delete.
- `sidebar.py` — Pi-hostname input + health badge. Imported by every page.
- `growzones_app.py` — entry script (welcome view).
- `pages/01_Camera.py` — live feed (MJPEG `<img>` embed) + calibration wizard
  + sliders + session control.
- `pages/02_Bundles.py` — merged table of Pi sessions and local bundles, with
  Download / Open / Delete-on-Pi actions.

## Bundle model

- One **bundle = one capture session.** Created when the user clicks Start;
  closed on Stop. ID is the start timestamp (`YYYY-MM-DDTHH-MM-SS`).
- Sessions stay on the Pi until manually deleted via the Mac.
- Sessions stay on the Mac after download. No automatic cleanup either side.
- No location concept. Bundles are a flat list.

## Schemas

All JSON has `schema_version: 1`.

`camera_profile.json` (Pi):
```json
{
  "schema_version": 1,
  "calibrated_at": "...",
  "sensor": {"model": "IMX219", "native_width": 3280, "native_height": 2464, "supports_autofocus": false},
  "exposure_time_us": 800,
  "analogue_gain": 1.0,
  "colour_gains": [1.72, 1.61],
  "lens_position": null,
  "scaler_crop": [0, 0, 3280, 2464]
}
```

Session `manifest.json`:
```json
{
  "schema_version": 1,
  "session_id": "2026-05-31T14-30-00",
  "started_at": "...",
  "stopped_at": "...",
  "interval_seconds": 900,
  "profile_snapshot": { ... },
  "image_count": 17,
  "total_bytes": 34123456
}
```

`capture_log.jsonl`:
```json
{"image": "14-30-00.jpg", "captured_at": "...", "mean_v": 142}
```

## Verification (end-to-end)

1. `curl http://growzones.local/api/health` → `{"ok": true, "has_profile": false}`.
2. `make app` → browser at `localhost:8501`; sidebar shows green dot.
3. Camera page renders the live MJPEG before calibration (using the camera's
   default auto settings).
4. Run auto-calibrate → aim → Continue → progress bar walks through detect /
   focus / exposure / WB / test → test capture appears → Save profile.
5. Sliders show the saved values; Apply to live changes the stream brightness.
6. Snapshot button returns a full-res JPEG inline.
7. Start a session at 10s interval, leave for 2 minutes, Stop.
8. Bundles page shows the new session with `On Pi: ●`, `On Mac: ○`. Download
   → row updates to `On Mac: ●`. Open folder → Finder.
9. Delete on Pi → arm → confirm → row updates to `On Pi: ○`.

## Out of scope for v2

- Sun-mask processing, heatmaps, zones, timelapses
- Auto-cull / tagging / clear-vs-cloudy
- Per-location grouping
- Drift detection
- Camera scheduling windows (no sunrise/sunset awareness — the user just
  starts the session in the morning and stops it in the evening; dark-frame
  skip handles the rest)
- In-app image browsing (open Finder instead)
- CLI / smoke test against synthetic data
- Pi-side web UI (deleted)

These come back in v3 once v2's capture loop is trusted.
