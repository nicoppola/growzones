# Camera setup walkthrough

The fundamental requirement of GrowZones is that **the camera be physically
fixed, with locked exposure and white balance.** Without that, none of the
cross-image comparison is meaningful. This document walks through getting
calibration right; failing here is the most common way the whole project goes
sideways.

## Before you start

- Camera is physically mounted in its final position. Re-running calibration
  after a move is fine, but it will invalidate all prior captures from this
  scene.
- It's a **clear, sunny midday**. Calibrating in flat overcast light produces
  a profile that will clip whites on the next sunny day. The wizard warns
  about this, but the warning depends on you knowing why.
- The scene as framed contains **both directly-lit and shaded regions**. The
  exposure calibration uses the 99th-percentile V channel — that's only
  meaningful if direct sun is actually in the frame.

## Running the wizard

Open `http://growzones.local/` and go to the **Setup** tab.

1. **Detect sensor** — reports model, native resolution, autofocus capability.
2. **Aim prompt** — you point the camera; everything after this is automated.
3. **Lock focus** (autofocus modules only) — wizard runs continuous AF, lets it
   settle, switches to manual at that lens position.
4. **Calibrate exposure** — wizard iteratively lowers gain (and then exposure
   time) until the 99th-percentile V is ≤ 250. SSE narrates each iteration
   so you can watch it converge.
5. **Calibrate white balance** — one auto pass, locks the resulting colour
   gains.
6. **Test capture** — full-resolution frame appears. If it looks right, click
   Save profile. If hard shadows aren't visible in the frame, the wizard warns
   you and asks to continue anyway.

## What "right" looks like in the test capture

- The brightest sunlit pixels should be **just shy of white** (not pure 255,
  not blown out — that's why we target 99th-percentile ≤ 250).
- Shaded regions should be **clearly darker** but not crushed to black —
  detail visible in the shadows.
- Colors should look approximately natural (no strong tint).

If the brightest pixels are clearly white (clipped), exposure didn't converge
— re-run the wizard, or try at a brighter time of day.

If colors are tinted (everything looks blue/orange), white balance is off —
this happens if you calibrate in mixed light (e.g., direct sun + warm
incandescent reflections). Calibrate in pure daylight.

## After calibration

The profile is written to `/var/lib/growzones/camera_profile.json` and used
by **every subsequent capture**. The Live tab's sliders default to these
values; tweaking them is session-only unless you click "Save to profile."

The capture scheduler refuses to start if no profile exists. If it stops
working after a profile delete or filesystem reset, re-run the Setup wizard.

## Re-running

You can re-run Setup any time:
- After swapping cameras (different sensors need new calibration).
- After moving the camera to a new scene.
- After a major scene change (you re-painted the wall, added an awning).

Re-running overwrites the saved profile. Captures from before and after the
re-calibration shouldn't be averaged together — start a new Location on the
Mac side to keep them separate.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Exposure won't converge after 6 iterations | Direct sun not in frame, or it's overcast | Re-frame, or wait for sun |
| Profile saved but captures look way too dark | Profile calibrated at peak sun, captures running late in the day | Expected — the dark-frame skip will discard the truly black ones; the exposure isn't "wrong" |
| White balance looks tinted blue | Calibrated in shade only | Re-run with direct sun in frame |
| Setup tab keeps redirecting after I save | Service can't read the profile file | Check `/var/lib/growzones/` ownership: should be `pi:pi`, readable by the service user |
