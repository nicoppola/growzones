# GrowZones — Pi-based sun mapping for balcony plant planning

## Context

You want to figure out where on your balcony plants will actually thrive by measuring how much direct sunlight each spot gets over multiple days. A Pi Zero 2 W + camera takes timed photos sunrise→sunset; your Mac does the image analysis; a web UI on the pi lets you control the camera. Once you import the images into your mac, you can cull bad frames, and view a zone map plus timelapse.

The single most important constraint baked into the design: **the camera must be physically fixed, with locked exposure and white balance.** Without that, none of the cross-image comparison is meaningful.

## Architecture

Two components, talking over your home Wi-Fi:

```
[Pi Zero 2 W]                            [Your Mac]
  picamera2  ───┐                          ┌── Streamlit app (localhost:8501)
  FastAPI    ───┤  live view               │     · Import bundle
  storage    ───┘  capture controls        │     · Browse / cull (auto-suggested)
                   [Download] button       │     · Tag clear/cloudy
       ▲                                   │     · Process
       │  HTTP (LAN) ─── .tar export ──►   │     · View heatmap, zones, timelapse
       │                                   │
   You ── browser                          └── OpenCV + ffmpeg under the hood
```

**Clean split of concerns:** Pi is a capture appliance (live view, take photos, export them). Mac is the data-exploration surface (import, cull, process, view results). The data flows one direction — Pi → tar → Mac — and never comes back. Results live on the Mac only.

Sync model: **browser-driven tar download**, not rsync. Click a date range in the Pi UI → `.tar` to your Downloads folder → in the Mac app, click Import → it extracts and merges. No SSH/rsync setup, no Pi hostname to remember, and the Pi can be offline while you process.

Web auth: **none.** Home LAN is trusted. Both the Pi UI and the Mac Streamlit app are unauthenticated; both bind to LAN-only addresses.

---

## Component 1: Pi side (`pi/`)

Python 3.11 + FastAPI + picamera2 + uvicorn. Runs as a systemd service.

### Camera setup — automated wizard on the Pi UI
A **Setup tab** in the Pi web UI runs end-to-end auto-calibration in one click, narrating each step. Re-run any time you swap cameras or change scene. Produces `/var/lib/growzones/camera_profile.json` which every capture from then on uses.

**Steps the wizard runs (mostly automated; one human step):**
1. **Detect sensor** — `libcamera-hello --list-cameras` + camera controls query. Reports model, native resolution, autofocus capability. Example: *"Detected IMX219 (Pi Camera v2). Native 3280×2464. Autofocus: No."*
2. **Aim prompt** (only human step) — "Point the camera at your scene, then click Continue." Embedded live preview.
3. **Lock focus** (autofocus modules only) — runs continuous AF, lets it settle, switches to `AfMode=Manual` at the resulting `LensPosition`. Reports: *"Focus locked at position 4.2."*
4. **Calibrate exposure** — iterative loop:
   a. Set `AeEnable=True`, capture a frame, record the auto-chosen `ExposureTime` and `AnalogueGain` as the starting point.
   b. Set `AeEnable=False` with those values, capture, measure **99th-percentile V** (not max — a single hot pixel would mislead). If ≤ 250, done.
   c. If > 250, halve `AnalogueGain` first (clamp floor at 1.0); if already at 1.0, halve `ExposureTime`. Re-capture, re-measure.
   d. Cap at 6 iterations; if still clipping, save anyway with a warning.
   Reports each iteration's values + measured 99th-percentile V via SSE so the UI can show the convergence.
5. **Calibrate white balance** — one `AwbEnable=True` pass, captures the resulting `ColourGains`, then sets `AwbEnable=False` with those values locked. Reports the gains and notes if they're far from daylight standard.
6. **Test capture** — shows resulting full-res frame with [Save profile] / [Re-run] buttons.
7. **Sanity check before saving** — if frame brightness variance is low (no hard shadows visible), wizard warns: *"This doesn't look like direct sun is in the frame. Exposure calibration is only accurate at midday clear-sky. Continue anyway?"* — because calibrating in flat light produces a profile that'll clip whites on the next sunny day.

**The resulting `camera_profile.json`** stores: sensor name, calibration timestamp, locked `ExposureTime`, `AnalogueGain`, `ColourGains`, `LensPosition` (if AF), and the test-capture filename for reference. The capture scheduler refuses to start if this file is missing — forces you through setup first.

Use `picamera2` (not legacy `picamera`); the Pi Zero 2 W and current Raspberry Pi OS use libcamera.

### Endpoints
- `GET /` — single-page web UI (HTMX + a couple of small JS bits for the live view).
- `GET /stream.mjpg` — MJPEG multipart stream. ~5–10 fps, downscaled (e.g. 640×480) — leaves room for capture jobs.
- `GET /api/camera/settings` / `PATCH /api/camera/settings` — `exposure_time_us` (int), `analogue_gain` (float), `colour_gains` (`[red, blue]` floats), `scaler_crop` (`[x, y, width, height]` ints in **sensor-native pixels**, not normalized — picamera2 convention). Manual override on top of the saved profile; session-only unless Save-to-profile is hit.
- `POST /api/camera/snapshot` — grab one full-resolution frame with current settings (for framing).
- `GET /api/camera/profile` — current saved profile (or 404 if not yet calibrated).
- `PATCH /api/camera/profile` — write the current Live-tab slider values into the saved profile (used by the [Save to profile] button on the Live tab). Body: same shape as the profile schema. Validates ranges before persisting.
- `POST /api/setup/detect` — runs sensor detection step, returns model/resolution/AF capability.
- `POST /api/setup/calibrate` — runs the full calibration pipeline (focus → exposure → WB → test capture). Streams progress events via SSE so the UI can narrate each step live. Returns final candidate profile + test-image URL; does not save yet.
- `POST /api/setup/save-profile` — persists the candidate profile to `/var/lib/growzones/camera_profile.json`.
- `GET /api/capture/config` / `PATCH …` — `interval_seconds`, `active` (bool), `window_start` and `window_stop` (HH:MM in the **Pi's local timezone** — set this with `sudo timedatectl set-timezone America/New_York` during install; the Pi's `/etc/timezone` is the source of truth), `dark_skip_threshold` (default `30` — frames with mean V below this are discarded before writing).
- `POST /api/capture/start` / `/stop`.
- `GET /api/capture/state` — `idle` | `capturing` (with timestamp). Used by the live stream UI to overlay "capturing image…" during the brief stream pause.
- `GET /api/days` — list of capture days with image counts.
- `GET /api/days/{date}/images` — list of images for a day with metadata.
- `GET /api/days/{date}/images/{name}` — image bytes.
- `GET /api/storage` — disk usage breakdown by day; total free space.
- `DELETE /api/days/{date}` — delete a single day's captures + sidecars. Returns count + bytes freed. Requires `?confirm=true` query param.
- `DELETE /api/days?from=YYYY-MM-DD&to=YYYY-MM-DD` — delete a date range. Returns per-day counts + total bytes freed. Requires `?confirm=true`.
- `GET /api/export?from=YYYY-MM-DD&to=YYYY-MM-DD` — streams a `.tar` of captures + sidecars + a `manifest.json` at the root. `Content-Disposition: attachment; filename=growzones-export-<today>.tar` (where `<today>` is the export date in YYYY-MM-DD, Pi local time). **Uncompressed tar**, **streamed chunk-by-chunk** (Python's `tarfile` in stream mode → FastAPI `StreamingResponse`); memory usage is constant regardless of bundle size, so even multi-GB exports won't OOM the Pi Zero.
- `GET /api/export/day/{date}` — same thing, single day. Useful for grab-today bundles.
- `GET /api/export/all` — same thing, everything on disk. Convenience for "back up everything."
- `GET /api/export/size?from=YYYY-MM-DD&to=YYYY-MM-DD` — pre-flight for the export endpoints above. Returns `{bytes, image_count, day_count, estimated_seconds_at_50mbps}` so the UI can show "~3.2 GB — expect ~9 min on LAN" and a Confirm button before triggering an actual stream. Cheap (just walks the manifest); accepts the same date-range args as `/api/export`. Also accepts `?scope=today` or `?scope=all` as shortcuts matching the other endpoints.

### Bundle layout
```
growzones-export-2026-05-26.tar
├── manifest.json                   # schema_version, exported_at, days: [{date, image_count}]
└── captures/
    └── 2026-05-20/
        ├── _pi_capture_log.jsonl   # one line per capture: settings used + timestamp + mean_v
        ├── 06-15-00.jpg
        ├── 06-30-00.jpg
        └── ...
```

The Pi only knows raw images + a per-day capture log (`_pi_capture_log.jsonl`). Tags (clear/cloudy) and exclusion flags are Mac-only state, written to `data/captures/<date>/_mac_meta.json` after import and never round-tripped back to the Pi.

### Capture scheduler
- APScheduler. One job runs every `interval_seconds` during the configured window (default `05:00`–`22:00` local time — covers daylight at any temperate latitude in any season).
- **Dark-frame skip**: after capture (with the locked-exposure profile, NOT auto-exposed), compute mean V over the full frame. If below `dark_skip_threshold` (default 30/255), discard before writing to disk. The whole point is that a locked midday-sun exposure naturally produces near-black frames at night/dawn — that IS the daylight signal. No sunrise/sunset math needed; the camera is its own light meter.
- Writes `captures/YYYY-MM-DD/HH-MM-SS.jpg` and appends one JSON-encoded line to `captures/YYYY-MM-DD/_pi_capture_log.jsonl` with all camera settings used. JSONL append (open with `"a"`, write the line + `\n`, `flush()` + `fsync()`) is atomic at the line level; halves the inode count vs per-image sidecars and shrinks the tar.
- Default interval: **900s (15 min)**. At a 17-hour window with ~10 useful daylight hours, that's ≈40 saved images/day, ~80 MB/day. Per-pixel daily sun-hours quantized to 15-min buckets (0.0, 0.25, 0.5 … hr) — plenty fine for plant placement, and culling 40 thumbnails by hand is easy.

### Camera deconfliction (live stream ↔ scheduled capture)
The Pi camera can only do one thing at a time. When a scheduled capture is due:
1. Capture worker acquires a global camera lock.
2. Live MJPEG stream's frame loop blocks on the lock — server stops sending frames.
3. Capture endpoint sets `capture_state = "capturing"`. The Live tab UI polls `/api/capture/state` once per second; when it sees `capturing`, it overlays a "Capturing image…" message on the (now-frozen) last frame so you know why the stream paused.
4. Capture worker switches camera config (preview → still), takes the shot, writes the file + sidecar, switches back, releases the lock. ~300–800 ms total.
5. Stream loop unblocks, frames resume, UI's next poll sees `idle` and removes the overlay.

This is preferable to "refuse to capture while streaming" — captures are the primary purpose; the stream momentarily yielding is the correct precedence.

### Web UI tabs (kept simple — server-rendered HTML + HTMX)
1. **Setup** — camera calibration wizard (see Camera setup section above). Shows current profile if one is saved; [Re-run setup] button. First-run UX redirects here if no profile exists.
2. **Live** — MJPEG view, exposure/gain sliders, digital zoom (ScalerCrop), snapshot button. Sliders default to the saved profile values; tweaks here are session-only — a [Save to profile] button writes the current slider values into `camera_profile.json` so they persist (and a [Reset to profile] button discards local tweaks). Overlays "Capturing image…" during scheduled captures (~0.5 s). Includes a hostname-aware URL hint so you can bookmark `http://growzones.local/`.
3. **Capture** — start/stop toggle; editable interval (slider/input); editable capture window (HH:MM start / HH:MM stop, default 05:00–22:00); editable dark-skip threshold; today's saved/discarded counts; warns and pauses capture if free space < 500 MB. Refuses to start if no camera profile is saved.
4. **Data** — two sections:
   - **Download**: date-range picker, [Download Range], [Download Today] shortcut, [Download All]. Clicking a Download button first calls `/api/export/size` and shows a confirm dialog with the byte count and an LAN-speed time estimate ("~3.2 GB — expect ~9 min") before starting the actual tar stream — multi-GB downloads over Wi-Fi from a Pi Zero take long enough that surprise-waiting feels broken.
   - **Storage**: disk usage breakdown by day (bar chart), date-range picker, [Delete Range] with two-step red confirmation showing exactly what will be deleted (day count, image count, MB freed). Per-day [Delete] buttons too.

*Four tabs. The Pi is a capture appliance — no data browsing, no culling, no results display. All of that lives in the Mac Streamlit app.*

### Hardening
- systemd unit with `Restart=always` so power blips and crashes recover automatically.
- mDNS hostname `growzones.local` via raspi-config (Avahi is pre-installed).
- NTP via systemd-timesyncd (default on Pi OS) so timestamps stay accurate. Service gated on `time-sync.target` — the Pi Zero 2 W has no RTC, so a cold boot would otherwise schedule captures against whatever wrong clock the kernel started with until sync completes.
- Bind to `0.0.0.0` on LAN only; no port forwarding, no auth.

---

## Component 2: Mac side (`mac/growzones/`)

A **Streamlit app** (`streamlit run growzones_app.py` → opens at `http://localhost:8501`). All Mac-side workflows live here: manage locations, import bundles, browse images, cull, tag days, run processing, view results. CLI commands are kept too as the underlying primitives (and for scripting), but day-to-day use is the app.

### Locations (top-level concept)
You'll probably evaluate more than one spot (balcony, back fence, herb planter at the side door). Each is a **Location** — a named container for captures, culling decisions, and results. Locations are 100% a Mac concept; the Pi just captures whatever it's pointed at.

**The core idea:** location is chosen at **import time**. Every bundle you download is imported into either an existing location (accumulating data) or a brand-new one. This makes "keep adding to Balcony over weeks" and "start evaluating Back Fence" use the exact same UX — just pick a different option in the same dropdown.

- **Import page** (the moment that matters) has a prominent "Add to: [Balcony ▾]" chooser at the top, with "+ New location…" as an inline option. Picking an existing name merges; picking new creates the location and imports in one step. After import, the app auto-switches its current selection to whatever you just imported into.
- **Sidebar dropdown** is for *browsing* — pick which location you want Days/Cull/Process/Results to show. Independent of import.
- All pages operate only on the currently-selected location's data. Implementation: a `state.py` module exposes `get_current_location()` that reads from `st.session_state['current_location_slug']` (mirrored to `data/.last_location` for persistence across app restarts). Every page calls this at the top.
- Index file at `data/locations.json`: `[{name, slug, created_at, notes}]`. Notes field for "south-facing, on the awning bracket" type reminders.

**Accumulating data into a location is the normal workflow:**
1. Pi has been capturing for a week. Download bundle.
2. Import → "Add to: Balcony" → bundle merges. New days appear; days you already had get any new images appended; your prior culling decisions and tags are untouched.
3. Repeat next week — same location, more data piles up. Re-run Process and the average gets more accurate.

**Starting a new location is the same flow, different choice:**
1. Move the Pi to a new spot. Re-run Camera setup on the Pi (new scene → re-calibrate exposure).
2. Capture for a few days. Download bundle.
3. Import → "+ New location…" → name it "Back fence" → bundle lands there. Balcony data untouched.

Edge case: a single bundle is assumed to be one location. If you physically moved the Pi mid-bundle, download two date-ranged bundles instead. The Mac doesn't auto-detect scene changes in v1.

### Streamlit app pages
0. **Locations** (Manage view, accessible from sidebar) — list all locations with image counts and last-imported date; rename / delete / edit notes. Creation also possible here, but more commonly happens inline from the Import page.
1. **Import** — drag-drop or file-picker for `growzones-export-*.tar`. **Prominent at top: "Add to: [dropdown of existing locations + '+ New location…']"** — this is where the location decision is made. Drop the file, pick the destination, click Import. Validates `manifest.json`, extracts to `data/locations/<location>/captures/`, shows what got added/updated, then auto-switches the sidebar selection to the imported-into location.
2. **Days** — table of captured days with image counts, auto-suggested clear/cloudy tag (from intraday brightness variance), and your override. Days whose `drift_ssim_vs_prior` (computed at import) falls below ~0.85 show a "Camera may have moved" warning chip — flags windstorm-bumped or kicked-tripod scenarios where the zone map would silently go wrong.
3. **Cull** — pick a day, see a grid of pre-generated 480×360 thumbnails (cached at import time under `_thumbs/`) with pre-checked auto-flag suggestions (frames that differ a lot from the day's median frame, after masking shadow regions — catches you walking through, birds, leaves blowing in close, etc.). You confirm/uncheck; clicking Save persists `excluded` flags.
4. **Process** — pick a date range, see clear-day count, click Run. Orchestrates the full pipeline by calling `growzones process` → `growzones zones` (default `k=4`) → `growzones timelapse` in sequence (the app is the orchestrator; the CLIs are the primitives). Streams progress for each stage. All intermediate results cached per location, so re-running with one more clear day is fast.
5. **Results** — heatmap, zone overlay on reference frame, per-zone sun-hour table, embedded timelapse video. Save buttons to export the overlay PNG / timelapse MP4 to a folder you choose.

### Underlying CLI commands (used by the app, also runnable standalone)
All commands take `--location <name>` (defaults to the last-selected location, persisted in `data/.last_location`). `--location` is required if no default is set.

- `growzones locations list` / `new <name> [--notes "..."]` / `rename <old> <new>` / `delete <name>` — manage locations.
- `growzones import <bundle.tar> --location <name> [--new]` — extracts into `data/locations/<location>/captures/`. If `--new`, creates the location first (errors if it already exists). Without `--new`, errors if the location doesn't exist (prevents typos silently creating a "balconey" sibling of "balcony"). Idempotent merge: adds any new image files; **never touches Mac-only state** (`_mac_meta.json` per day, holding tags + exclusion flags), so re-importing the same days preserves your culling work. Reads `manifest.json` to know what to expect; refuses with a clear error if schema_version is unknown.
- `growzones auto-cull <date> --location <name>` — compute day median, flag outliers, write suggestions into `data/locations/<location>/captures/<date>/_cull_suggestions.json`. Streamlit Cull page reads this.
- `growzones tag-day <date> --clear|--cloudy|--untag --location <name>` — bulk-tag helper. Only `clear`-tagged days contribute to multi-day averaging in `process`.
- `growzones process <date-range> --location <name>` — per clear, non-excluded day:
  1. For each image: build sun mask (see below). Store as compressed numpy array.
  2. Sum masks across the day → `sun_minutes[y, x]` for that day, save to `_heatmap.npz`.
  Then average per-day `_heatmap.npz` arrays across all clear days in range → in-memory `sun_minutes_per_day[y, x]` consumed by `growzones zones` next.
- `growzones zones <date-range> --k 4` — K-means on normalized features `(sun_minutes/max_window_minutes, x/W, y/H)` — all three land in [0, 1] and are equally weighted. Without this normalization the pixel coords (0–3280) would swamp sun_minutes (0–600), making zones degenerate to spatial blobs ignoring the actual sun signal. `KMeans(..., random_state=42)` so cluster boundaries are reproducible run-to-run. Output color overlay PNG + per-zone stats. Default k=4 — degrades gracefully if data is bimodal (one tiny cluster); bump to 5 once you've seen results from a real week of data.
- `growzones timelapse <date-range> [--day DATE]` — ffmpeg from non-excluded frames. **8 fps, h264** (at 15-min sampling, 8 fps gives ~7 seconds per day — watchable; 30 fps would blink past in 2 seconds). Burns the capture timestamp into the bottom-right corner via PIL so you can see the time advance while watching.

### Mac-side data layout
Root: **`~/Library/Application Support/growzones/`** — standard macOS user-data location, created on first run, included in Time Machine backups, survives moving or reinstalling the code. Resolved via `pathlib.Path.home() / "Library/Application Support/growzones"` in `locations.py`; never via the process CWD. All `data/...` paths elsewhere in this plan are relative to this root.

Everything inside `data/` is namespaced by location:
```
~/Library/Application Support/growzones/data/
├── locations.json                                 # index: name, slug, created_at, notes
├── .last_location                                 # CLI default
└── locations/
    ├── balcony/
    │   ├── captures/
    │   │   └── 2026-05-20/
    │   │       ├── 06-15-00.jpg              ← from Pi (untouched after import)
    │   │       ├── ...
    │   │       ├── _pi_capture_log.jsonl    ← from Pi (per-day capture settings + timestamps)
    │   │       ├── _thumbs/                  ← bundle.py: 480×360 JPEGs for Cull grid
    │   │       │   ├── 06-15-00.jpg
    │   │       │   └── ...
    │   │       ├── _cull_suggestions.json    ← growzones auto-cull
    │   │       ├── _mac_meta.json            ← Streamlit Days/Cull: tag + per-image excluded + drift_ssim
    │   │       └── _heatmap.npz              ← growzones process cache
    │   └── results/
    │       ├── reference.jpg
    │       ├── heatmap_2026-05-20_to_2026-05-26.png
    │       ├── zones_2026-05-20_to_2026-05-26.png
    │       ├── stats_2026-05-20_to_2026-05-26.json
    │       └── timelapse_2026-05-20_to_2026-05-26.mp4
    └── back-fence/
        └── ... (same structure, fully independent)
```
Underscored files = Mac state. Plain files = from Pi. `_mac_meta.json` is the source of truth for your culling/tagging decisions; it survives re-imports. Locations are fully independent — deleting one leaves the others untouched.

### Auto-cull algorithm (per day)
For each day after import:
1. **Per-pixel median frame** across all of the day's captures (V channel only — saves 3× memory and is what we score against). Median is robust to short interference (you walking past, birds) — gives a "what was usually here" baseline.
2. **Per-pixel variance** across the day, same V channel. Pixels with variance above the **75th percentile** are "expected-to-change" — these are the moving-shadow regions where the sun signal lives. (75th percentile keeps the threshold self-tuning across scenes.)
3. **Diff scoring per frame**: compute `diff = |frame_V - median_V|`; then score = mean of `diff` over pixels that are NOT in the expected-to-change set. This way:
   - A frame where you walked through the *whole scene* scores high (most pixels differ unexpectedly).
   - A frame that captures only the normal shadow march scores low (the changing pixels are excluded).
4. **Flag** any frame whose score > T (default `30`, on the 0–255 V scale). Tunable in the Cull page; lower = more aggressive flagging.
5. Write all per-frame scores + flags to `_cull_suggestions.json` (see Data schemas). The Streamlit Cull page renders the day's grid with flagged frames pre-checked; user confirms or unchecks per-frame and clicks Save — confirmed exclusions go to `_mac_meta.json.excluded_images`.

This catches you walking through, birds close to camera, a closed parasol that wasn't there yesterday. It won't catch subtle stuff (a small bug on the lens) — that's what manual review is for. Memory note: ~40 V-only frames at 3280×2464 ≈ ~320 MB stacked for median (up to ~480 MB for an extended window); fine on a Mac, would not fit on the Pi (another reason processing is Mac-side).

### Reference frame selection
The zone overlay needs a background image to draw on top of. Default: from the most recent clear day, take all non-excluded frames captured within ±1 hr of local solar noon, and pick the one whose `mean_v` is the median of that group. Brightest-frame would be the most blown-out — bad as a background for a colored overlay; the median frame from midday is well-exposed across the whole scene with no transient (bird, person) baked in. Local solar noon is approximated as the midpoint of the configured capture window — close enough for picking a reference frame; precise sun-angle computation isn't needed in v1. Streamlit Results page has a "Change reference frame" button if the auto-pick is wrong.

### Sun detection algorithm (per image)
Tuned for an **angled** view where you have both sunlit and shaded patches in frame at once:
1. Convert to HSV; take V channel.
2. Pixel is "directly lit" if `V > T_v` where `T_v` ≈ 220 (tunable, calibrated on a known sunny patch).
3. Boost: dilate the result and require the pixel sits within a region where local V variance > threshold — direct sun creates **hard shadow edges**, diffuse light doesn't. This kills false positives from white walls/sky.
4. Output binary mask.

All three steps are in v1. The calibrated exposure (99th-percentile V ≤ 250) leaves only ~30 levels of headroom between bright diffuse-lit surfaces and direct-sun pixels, so the variance/edge boost is needed from the start, not a future-tense optimization.

### Why "zones" are pixel-space, not square-meters
You picked an angled view → perspective distortion → a pixel near the camera is much smaller IRL than a pixel far away. So the zone map is **visual** ("this corner gets ~6 sun-hours/day, this strip gets ~2") rather than per-m². That's fine for plant placement; you read the overlay by eye against the reference frame.

If you later want per-m² numbers, you'd do a one-time homography calibration (mark 4 known points on the balcony floor) and rectify. Not in v1.

### Multi-day averaging
- Only "clear" days contribute to the average.
- If you have <3 tagged clear days, surface a warning — the average isn't trustworthy yet.
- Recompute is cheap (cached per-day heatmaps), so adding a new clear day just re-averages.

---

## Data schemas

All JSON files use `schema_version: 1` as the first field. Bumping breaks compatibility; readers must validate and error clearly on unknown versions.

### Pi-written files

**`/var/lib/growzones/camera_profile.json`** — produced by Setup wizard, consumed by every capture and the Live tab default.
```json
{
  "schema_version": 1,
  "calibrated_at": "2026-05-26T13:42:00-04:00",
  "sensor": {
    "model": "IMX219",
    "native_width": 3280,
    "native_height": 2464,
    "supports_autofocus": false
  },
  "exposure_time_us": 800,
  "analogue_gain": 1.0,
  "colour_gains": [1.72, 1.61],
  "lens_position": null,
  "scaler_crop": [0, 0, 3280, 2464],
  "test_capture_path": "/var/lib/growzones/setup_tests/2026-05-26T13-42-00.jpg"
}
```
Field rules: `colour_gains` is `[red, blue]`. `lens_position` is float if `sensor.supports_autofocus`, else `null`. `scaler_crop` is `[x, y, width, height]` in sensor-native pixels.

**Per-day capture log `_pi_capture_log.jsonl`** — one JSON object per line, appended at capture time. **Diagnostic only** — the Mac side does not consume it for processing in v1; it's preserved so you can debug "why does this image look weird." First line of each new day's file is a header line with `schema_version`; subsequent lines are capture records.

Header line:
```json
{"schema_version": 1, "date": "2026-05-26"}
```
Per-capture line:
```json
{"image": "14-15-00.jpg", "captured_at": "2026-05-26T14:15:00-04:00", "exposure_time_us": 800, "analogue_gain": 1.0, "colour_gains": [1.72, 1.61], "lens_position": null, "scaler_crop": [0, 0, 3280, 2464], "profile_calibrated_at": "2026-05-26T13:42:00-04:00", "mean_v": 142}
```
`captured_at` is ISO 8601 with Pi-local TZ offset. `mean_v` is the pre-write dark-skip check value (0–255). `image` is the filename (not full path) — that's the join key back to the JPEG.

**Bundle root `manifest.json`** — at the root of every tar export.
```json
{
  "schema_version": 1,
  "exported_at": "2026-05-26T18:00:00-04:00",
  "pi_hostname": "growzones",
  "date_range": ["2026-05-20", "2026-05-26"],
  "days": [
    {"date": "2026-05-20", "image_count": 42, "total_bytes": 87654321},
    {"date": "2026-05-21", "image_count": 40, "total_bytes": 82345678}
  ]
}
```
Mac `bundle.py` accepts `schema_version` 1; anything else errors with "Unknown bundle schema version N. Update the Mac CLI."

### Mac-written files

**`data/locations.json`** — index of all locations.
```json
{
  "schema_version": 1,
  "locations": [
    {
      "name": "Balcony",
      "slug": "balcony",
      "created_at": "2026-05-20T09:00:00-04:00",
      "notes": "South-facing, awning bracket mount"
    }
  ]
}
```
**Slug rules**: lowercase the name → replace any character outside `[a-z0-9]` with `-` → collapse runs of `-` → strip leading/trailing `-`. On collision, append `-2`, `-3`, etc. Slugs are immutable once created (used as directory names); renames change `name` only.

**`data/locations/<slug>/captures/<date>/_mac_meta.json`** — per-day Mac state. Source of truth for tagging + exclusions.
```json
{
  "schema_version": 1,
  "tag": "clear",
  "tag_source": "user",
  "excluded_images": ["12-30-00.jpg", "13-15-00.jpg"],
  "drift_ssim_vs_prior": 0.92
}
```
`tag`: `null | "clear" | "cloudy"` — drives whether the day contributes to multi-day averaging in `growzones process` (only `"clear"` does). `tag_source`: `"auto"` (from variance-based suggestion) or `"user"` (manual override) — used so that re-running auto-suggest doesn't overwrite a user-set tag. `excluded_images`: filenames (not full paths) of individual frames the user has confirmed should be skipped during processing. *To exclude an entire day, mark every image excluded — there's no separate "exclude day" tag, since `tag=null` + no exclusions is already the "don't use" state.* `drift_ssim_vs_prior`: float in [0, 1] from `bundle.py`'s post-import SSIM check against the previous imported day's median V frame; `null` if this is the first day in the location. Days below ~0.85 surface a "Camera may have moved" warning on the Days page.

**`data/locations/<slug>/captures/<date>/_cull_suggestions.json`** — auto-cull output; the Cull page reads these to pre-check the grid. The user's confirmed choices go to `_mac_meta.json.excluded_images`; this file is advisory only and is overwritten on every `growzones auto-cull` run.
```json
{
  "schema_version": 1,
  "computed_at": "2026-05-26T15:00:00-04:00",
  "threshold": 30,
  "suggestions": [
    {"image": "12-30-00.jpg", "diff_score": 47, "flagged": true},
    {"image": "12-45-00.jpg", "diff_score": 8, "flagged": false}
  ]
}
```
*Note: the `30` default here (auto-cull diff threshold, 0–255 V scale) is **unrelated to** the Pi's `dark_skip_threshold` (also defaulting to 30). They happen to share a number by coincidence; in code they should have distinct, descriptive names (`AUTO_CULL_DIFF_THRESHOLD` vs `DARK_FRAME_V_THRESHOLD`) to avoid confusion.*

**`data/locations/<slug>/captures/<date>/_heatmap.npz`** — numpy archive with one array `sun_minutes` of shape `(H, W)`, dtype `uint16`. No JSON; numpy's own format.

**`data/locations/<slug>/results/stats_<from>_to_<to>.json`** — per-zone summary written by `growzones zones`.
```json
{
  "schema_version": 1,
  "computed_at": "2026-05-26T16:00:00-04:00",
  "date_range": ["2026-05-20", "2026-05-26"],
  "clear_days_used": 5,
  "k": 4,
  "random_state": 42,
  "zones": [
    {"id": 0, "label": "tier 1", "color_hex": "#f4d03f", "mean_sun_minutes_per_day": 412, "pixel_count": 142080},
    {"id": 1, "label": "tier 2", "color_hex": "#f39c12", "mean_sun_minutes_per_day": 248, "pixel_count": 167936},
    {"id": 2, "label": "tier 3", "color_hex": "#a0795c", "mean_sun_minutes_per_day":  98, "pixel_count": 234112},
    {"id": 3, "label": "tier 4", "color_hex": "#566573", "mean_sun_minutes_per_day":  18, "pixel_count": 333952}
  ]
}
```
`label` is auto-derived by sorting zones by `mean_sun_minutes_per_day` (descending) and assigning numbered "tier 1..k" labels — tier 1 is always the sunniest. Same scheme for any k.

---

## Installation prerequisites

### Pi (one-time setup before running anything)

Three phases: (A) provision the SD card with Raspberry Pi Imager, (B) boot + SSH in, (C) run the install script.

#### A. Provision the SD card in Raspberry Pi Imager
Use the official Raspberry Pi Imager (download from raspberrypi.com if you don't have it).

1. **Choose Device**: Raspberry Pi Zero 2 W.
2. **Choose OS**: "Raspberry Pi OS (other)" → **Raspberry Pi OS Lite (64-bit)**. Lite is intentional — no desktop = more RAM for our service. Bookworm release.
3. **Choose Storage**: your microSD card.
4. **Before clicking Write**, click the gear icon (or press `Ctrl+Shift+X`) to open **Advanced Options**. Set:
   - ☑ **Set hostname**: `growzones`
   - ☑ **Enable SSH** — use password authentication (simpler) or paste your public key
   - ☑ **Set username and password**: e.g. `pi` / a strong password
   - ☑ **Configure wireless LAN**: your home Wi-Fi SSID + password + country code
   - ☑ **Set locale settings**: your timezone (used for capture window times) + keyboard layout
5. Click **Save**, then **Write**. Wait for verify to complete (~5–10 min).
6. Eject the card; put it in the Pi; connect camera CSI cable; power on. First boot takes 2–3 minutes (it's resizing the filesystem and applying the configuration you set in Imager).

This produces a headless Pi — no monitor, keyboard, or mouse ever required.

#### B. SSH in
From your Mac:
```sh
ssh pi@growzones.local
```
If that fails, the Pi probably hasn't joined Wi-Fi yet (give it another minute) or mDNS isn't resolving (check that your Mac has Bonjour, which is standard).

#### C. Run the install script
From your Mac, copy the project onto the Pi:
```sh
scp -r pi/ pi@growzones.local:/home/pi/growzones
```
Then SSH in and run the install script — it does everything below idempotently (safe to re-run):
```sh
ssh pi@growzones.local
cd /home/pi/growzones
./install.sh
```

**What `install.sh` does** (transparently — you can read the script):
1. Enables the camera interface via `sudo raspi-config nonint do_camera 0`.
2. `sudo apt update && sudo apt install -y python3-picamera2 python3-libcamera python3-kms++ python3-pip python3-venv python3-numpy python3-pillow libcamera-apps avahi-daemon` — `picamera2` is NOT pip-installable; it ships only as an apt package alongside libcamera bindings.
3. Creates a venv with `--system-site-packages` (so apt-installed `picamera2`, `numpy`, `pillow` are importable):
   `python3 -m venv --system-site-packages /home/pi/growzones/venv`
4. `pip install -e /home/pi/growzones` — installs `pyproject.toml` pip deps inside the venv.
5. Creates `/var/lib/growzones/` (with `setup_tests/` subdir) under `pi:pi` ownership. All Pi-writable state lives under this one root — camera profile, setup-test captures, and (once captures start) the day directories.
6. Installs the systemd unit from `pi/systemd/growzones.service` to `/etc/systemd/system/` and runs `sudo systemctl enable --now growzones`.
7. Prints the final URL: `Setup complete. Open http://growzones.local/ in your browser.`

Each step is wrapped in a check ("if already done, skip"), so re-running after a partial failure picks up where it left off.

#### Verify
From your Mac, open `http://growzones.local/`. Should load the Setup tab (first-run, no profile yet). If it doesn't:
- `ssh pi@growzones.local 'systemctl status growzones'` — service running?
- `ssh pi@growzones.local 'journalctl -u growzones -f'` — app errors?
- `dns-sd -G v4 growzones.local` (on Mac) or `avahi-resolve -n growzones.local` (on Pi) — mDNS working?

### Mac (one-time)

1. **Python 3.11+**: `brew install python@3.11` or via pyenv.
2. **ffmpeg** (system binary, not pip): `brew install ffmpeg`. Verify: `ffmpeg -version`.
3. **Install the project**:
   ```sh
   cd mac/
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```
4. **Verify**: `streamlit run growzones_app.py` → browser opens `http://localhost:8501`.

---

## Critical files to create

**Pi (`pi/`)**
- `app/main.py` — FastAPI app factory, routes.
- `app/camera.py` — picamera2 wrapper: profile loading, stream + snapshot config switching, ScalerCrop handling.
- `app/setup_wizard.py` — sensor detection, AF/exposure/WB calibration pipeline, profile persistence. SSE event stream so UI can narrate.
- `app/capture.py` — APScheduler, capture job with dark-frame skip, sidecar JSON writer. Refuses to start if no profile.
- `app/storage.py` — filesystem layout, image listing, disk-usage helper, day/range delete.
- `app/templates/` — `setup.html`, `live.html`, `capture.html`, `data.html`, plus `base.html`.
- `app/static/` — minimal JS for slider→PATCH, SSE consumer for setup narration, HTMX from CDN.
- `systemd/growzones.service` — service unit (`Restart=always`, `WorkingDirectory=/home/pi/growzones`, runs as `pi` user, `ExecStart=/home/pi/growzones/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80`). Includes `After=time-sync.target` and `Wants=time-sync.target` so the capture scheduler never starts against an unsynced clock (Pi Zero 2 W has no RTC).
- `install.sh` — idempotent post-flash install script (see Installation prerequisites for what it does). Each step checks "is this already done?" before acting, so re-runs after partial failures are safe. Marked executable: `chmod +x install.sh`.
- `pyproject.toml` — **pip** deps only: `fastapi`, `uvicorn`, `apscheduler`, `jinja2`, `python-multipart`, `sse-starlette`. `picamera2`, `numpy`, `pillow` come from apt (see Installation prerequisites — they are NOT pip-installable on Pi OS).
- `README.md` — points at the Installation prerequisites section of the plan + lists known troubleshooting items.

**Mac (`mac/growzones/`)**
- `growzones_app.py` — Streamlit entrypoint with sidebar location selector + 6 pages (Locations Manage, Import, Days, Cull, Process, Results). Uses the Streamlit multipage pattern (`pages/` directory of script files; Streamlit auto-discovers them as sidebar entries). Thin layer that calls into the core modules below.
- `state.py` — Streamlit session state helpers. `get_current_location()` reads `st.session_state['current_location_slug']` (falling back to `data/.last_location` on cold start), validates it exists, returns the location object. Every page calls this at the top; if no location exists yet, displays a "Create your first location" prompt and halts the page render. *Two browser tabs hold independent `current_location_slug` selections by design — that's what users expect from tabs. Each sidebar switch also writes to `data/.last_location` so a fresh tab or CLI run starts on whichever location you last touched.*
- `cli.py` — Typer entrypoint exposing the same primitives. The Streamlit app calls these under the hood; also runnable standalone for scripting. All commands take `--location`.
- `locations.py` — read/write `data/locations.json`, slugify names, manage `.last_location`, resolve `data/locations/<slug>/` paths for the rest of the codebase to use. Exposes `growzones_data_dir()` returning `pathlib.Path.home() / "Library/Application Support/growzones" / "data"` (creates it if absent); every other module routes through this rather than touching CWD-relative paths.
- `bundle.py` — tar import: validate `manifest.json`, extract image files into `data/locations/<location>/captures/`, leave any existing `_mac_meta.json` and `_cull_suggestions.json` untouched (Mac-only state survives re-imports). For each newly-extracted image, also write a thumbnail to `_thumbs/<HH-MM-SS>.jpg` (PIL, 480×360, JPEG quality 70) — the Cull page reads thumbs, not originals, so a 40-frame day grid ships ~4 MB to the browser instead of ~80 MB. After extraction, compute the day's per-pixel median V-channel frame and the SSIM score against the previous imported day's median (if any); persist as `drift_ssim_vs_prior` in `_mac_meta.json` so the Days page can flag camera-bump events.
- `auto_cull.py` — per-day median frame, diff scoring, suggestion writer.
- `sun_mask.py` — per-image HSV thresholding, optional shadow-edge boost.
- `heatmap.py` — per-day accumulation, multi-day averaging. Disk-cached per day.
- `zones.py` — K-means clustering, overlay rendering, reference frame picker.
- `timelapse.py` — ffmpeg invocation, PIL timestamp burn-in.
- `pyproject.toml` — deps: `streamlit`, `opencv-python`, `numpy`, `scikit-learn` (KMeans), `scikit-image` (SSIM for drift detection), `typer`, `pillow`. **ffmpeg is a system binary**, not a pip dep (`brew install ffmpeg`).

**Shared**
- `docs/CAMERA_SETUP.md` — exposure/WB calibration walkthrough with a screenshot of expected histogram. Critical — this is the thing that goes wrong first.

---

## Step 0 (do before writing any code)

Spend 30 minutes searching for existing wheels to avoid reinventing. Specific things to check:
- **picamera2-webui** / **picamera2-WebUI-Lite** — existing FastAPI/Flask wrappers around picamera2 with live view and settings controls. If one is close enough, fork it and add the setup wizard + capture scheduler + export endpoints rather than building the camera UI from scratch.
- **MotionEye** — heavier but battle-tested; probably overkill but worth knowing it exists.
- **TimelapsePi** style projects — many exist; cherry-pick capture-scheduler patterns.

If a good base exists, the Pi component shrinks to ~a day's work instead of three. **This research is the difference between "weekend project" and "month-long project."**

---

## Build order

1. **Step 0 research** (above).
2. **Pi: setup wizard + camera profile.** Sensor detection, exposure/WB/AF calibration, profile save. Test by re-running with a different camera if you have one — should auto-adapt.
3. **Pi: live MJPEG + settings sliders.** Frame the balcony from the couch. Verify saved profile values populate the sliders.
4. **Pi: capture scheduler + storage + systemd hardening + mDNS hostname.** Run for one full day, confirm no gaps, stable file sizes, exposure stays consistent morning→afternoon (no drift means the lock is working), confirm `growzones.local` resolves.
5. **Pi: export endpoint + Download button. Mac: locations module + bare-bones Streamlit sidebar (location selector + create) + Import page + sun_mask + per-day heatmap.** Generate one heatmap, eyeball it against memory of where the sun actually hit.
6. **Mac: Streamlit Days + Cull pages with auto-cull suggestions.**
7. **Mac: Process page + zones + overlay + timelapse generation.** K=4 to start, tune k by eye. Process produces all artifacts (heatmap, zone overlay PNG, per-zone stats JSON, timelapse MP4).
8. **Mac: Results page.** Displays whatever Process generated. All viewing happens here.
9. Run for 2 weeks, collect 3+ clear days, refine thresholds.

---

## Hardware checklist (not code, but it'll bite you if you skip)

- [ ] Rigid camera mount (the **#1** thing — any drift = broken zone map).
- [ ] Weatherproof enclosure with clear window for the lens, plus desiccant pack for condensation.
- [ ] Power: outdoor-rated USB cable runs to a covered outlet, or a USB battery + solar (separate project).
- [ ] Wi-Fi signal test from the mount spot before you weatherproof everything.
- [ ] microSD card ≥ 32 GB; at ~80 MB/day a single card holds many months of capture. Use the Data tab's [Download All] for periodic archival to your Mac.

---

## Verification

End-to-end smoke test (do this before going outside):
1. Run Pi service on desk. Confirm `http://growzones.local/` loads. Setup tab runs wizard end-to-end, narrates each step, and saves a profile. (Indoor calibration will trigger the "no direct sun in frame" warning — that's correct behavior.)
2. Swap cameras (if you have two): re-run Setup, confirm new sensor is detected and a fresh profile saved.
3. Live tab: MJPEG renders, snapshot button writes a file, sliders adjust stream brightness in real-time.
4. Run capture for 5 min at 10s interval; confirm 30 files + sidecars on disk.
5. Click Download in the Pi UI → tar lands in your Downloads folder.
6. `streamlit run growzones_app.py` → Import page: "Add to: + New location…" → name it "test" → drop the bundle → click Import. App auto-switches to "test." Days page shows the 5-min window. Tag it `clear`.
7. Cull page: auto-suggestions appear; manually exclude 1 frame to confirm the toggle works.
8. Process page (k=4): click Run, watch progress complete in seconds. Results page shows a heatmap, 4-zone overlay, and embedded timelapse (meaningless on a 5-min sample, but valid).

Outdoor verification:
- Capture one full sunny day. Confirm: no capture gaps, no clipped highlights, no exposure drift between morning and afternoon (compare a shaded wall area — its V should be roughly constant).
- Compare generated zone overlay to your eyeball memory of where the sun was. If it's nonsense, the exposure isn't locked or `T_v` is wrong.

## Scope explicitly out of v1

- Per-m² (real-world area) zone sizes — would need homography calibration.
- Pan/tilt motors. Plant species recommendations. Mobile app. Notifications.
- Authentication. Public/internet-facing access. HTTPS.
- Native Mac app — Streamlit is good enough.
- Cross-season aggregation (May data vs. November data is meaningfully different — keep them separate; v2 could let you compare seasons side by side).
