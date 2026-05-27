"""FastAPI app factory + routes.

This milestone wires up:
  - GET  /                       — Setup tab (or redirect to it if no profile)
  - GET  /stream.mjpg            — MJPEG live stream (used by Setup's Aim step)
  - GET  /api/camera/profile     — currently saved profile (404 if none)
  - GET  /api/capture/state      — idle | streaming | setup | capturing
  - POST /api/setup/detect       — sensor info (one-shot)
  - POST /api/setup/calibrate    — SSE-streamed calibration pipeline
  - POST /api/setup/save-profile — persist the candidate produced by calibrate
  - GET  /api/setup/test-image/{name} — serve test captures from setup_tests/

Live tab, Capture tab, Data tab are stubs returning a "coming soon" page.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import setup_wizard
from .camera import (
    PROFILE_PATH,
    PROFILE_SCHEMA_VERSION,
    SETUP_TESTS_DIR,
    CameraContext,
    CameraProfile,
    SensorInfo,
    iso_now_local,
    load_profile,
    save_profile,
)
from .mjpeg import mjpeg_stream

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open the camera once at startup; close it cleanly on shutdown.

    If a profile is saved, apply it so the camera's current settings start
    from the calibrated values. Without this, the Live tab's first GET
    would return whatever picamera2 defaulted to.
    """
    log.info("Opening camera…")
    camera = CameraContext()
    profile = load_profile()
    if profile is not None:
        with camera.lock(state="setup"):
            camera.apply_profile(profile)
        log.info("Applied saved profile (calibrated %s)", profile.calibrated_at)
    else:
        log.info("No saved profile; Live tab will start from camera defaults")
    app.state.camera = camera
    try:
        yield
    finally:
        log.info("Closing camera…")
        camera.close()


def create_app() -> FastAPI:
    app = FastAPI(title="GrowZones Pi", lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    _register_routes(app)
    return app


# Routes ---------------------------------------------------------------------

def _register_routes(app: FastAPI) -> None:

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        """Always lands on Setup if no profile exists; otherwise Live."""
        profile = load_profile()
        if profile is None:
            return RedirectResponse(url="/setup", status_code=303)
        return RedirectResponse(url="/live", status_code=303)

    # --- Tabs (server-rendered) ------------------------------------------

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_tab(request: Request):
        profile = load_profile()
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {"profile": profile, "active_tab": "setup"},
        )

    @app.get("/live", response_class=HTMLResponse)
    async def live_tab(request: Request):
        profile = load_profile()
        if profile is None:
            # Live tab needs a profile to make sense (sliders default to it).
            return RedirectResponse(url="/setup", status_code=303)
        camera = request.app.state.camera
        return TEMPLATES.TemplateResponse(
            request,
            "live.html",
            {
                "profile": profile,
                "current_settings": camera.current_settings,
                "sensor": camera.sensor,
                "active_tab": "live",
            },
        )

    @app.get("/capture", response_class=HTMLResponse)
    async def capture_tab(request: Request):
        return _stub_page(request, "Capture", "Scheduled capture controls — coming next milestone.")

    @app.get("/data", response_class=HTMLResponse)
    async def data_tab(request: Request):
        return _stub_page(request, "Data", "Download + storage management — coming next milestone.")

    # --- Live stream (also used by the Setup wizard's Aim step) ----------

    @app.get("/stream.mjpg")
    async def stream(request: Request):
        return await mjpeg_stream(request.app.state.camera)

    # --- Camera state / profile / live settings -------------------------

    @app.get("/api/camera/profile")
    async def get_profile():
        profile = load_profile()
        if profile is None:
            raise HTTPException(status_code=404, detail="No saved profile")
        return profile.to_dict()

    @app.patch("/api/camera/profile")
    async def patch_profile(request: Request):
        """Save the current Live-tab session values into the profile on disk.

        Wired to the [Save to profile] button. The body should be the full
        new profile (same shape as the schema); validates ranges before
        writing.
        """
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        existing = load_profile()
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail="No saved profile to update — run Setup first",
            )
        try:
            new_profile = _merge_profile_patch(existing, payload)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        save_profile(new_profile)
        return JSONResponse({"saved": True, "profile": new_profile.to_dict()})

    @app.get("/api/camera/settings")
    async def get_settings(request: Request):
        camera = request.app.state.camera
        return {
            "current": camera.current_settings,
            "sensor": {
                "model": camera.sensor.model,
                "native_width": camera.sensor.native_width,
                "native_height": camera.sensor.native_height,
                "supports_autofocus": camera.sensor.supports_autofocus,
            },
        }

    @app.patch("/api/camera/settings")
    async def patch_settings(request: Request):
        """Apply session-only overrides on top of the saved profile.

        Body: any subset of {exposure_time_us, analogue_gain, colour_gains,
        lens_position, scaler_crop}. Returns the new effective values.
        """
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        try:
            kwargs = _validated_settings_kwargs(payload, request.app.state.camera.sensor)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        camera = request.app.state.camera
        try:
            with camera.lock(state="live", blocking=False):
                camera.apply_manual(**kwargs)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Camera busy: {e}")
        return {"current": camera.current_settings}

    @app.post("/api/camera/snapshot")
    async def snapshot(request: Request):
        """One full-resolution JPEG with the current settings. Used by the
        Live tab's Snapshot button for framing the scene."""
        from datetime import datetime
        from fastapi.responses import Response
        camera = request.app.state.camera
        # Capture in a thread so we don't block the event loop.
        import asyncio
        from PIL import Image
        import io
        def _grab() -> bytes:
            with camera.lock(state="snapshot"):
                arr = camera.capture_still_array()
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, format="JPEG", quality=92)
            return buf.getvalue()
        jpeg = await asyncio.to_thread(_grab)
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": f'attachment; filename="snapshot-{ts}.jpg"'
            },
        )

    @app.get("/api/capture/state")
    async def get_capture_state(request: Request):
        return {"state": request.app.state.camera.state}

    # --- Setup wizard ----------------------------------------------------

    @app.post("/api/setup/detect")
    async def setup_detect(request: Request):
        sensor = request.app.state.camera.sensor
        return {
            "model": sensor.model,
            "native_width": sensor.native_width,
            "native_height": sensor.native_height,
            "supports_autofocus": sensor.supports_autofocus,
        }

    @app.post("/api/setup/calibrate")
    async def setup_calibrate(request: Request):
        """SSE stream: detect → focus → exposure → WB → test capture."""
        return EventSourceResponse(
            setup_wizard.run_calibration(request.app.state.camera)
        )

    @app.post("/api/setup/save-profile")
    async def setup_save_profile(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        try:
            profile = setup_wizard.save_candidate(payload)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(
            {"saved": True, "path": str(PROFILE_PATH), "profile": profile.to_dict()}
        )

    @app.get("/api/setup/test-image/{name}")
    async def setup_test_image(name: str):
        # Defend against path traversal: only the filename, no slashes.
        if "/" in name or ".." in name:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = SETUP_TESTS_DIR / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, media_type="image/jpeg")


def _stub_page(request: Request, title: str, body: str) -> HTMLResponse:
    """Tiny placeholder for tabs not yet implemented in this milestone."""
    return TEMPLATES.TemplateResponse(
        request,
        "base.html",
        {
            "title": title,
            "active_tab": title.lower(),
            "content_html": f"<p class='muted'>{body}</p>",
        },
    )


# ---------------------------------------------------------------------------
# Validation helpers (kept here rather than in camera.py to keep camera.py
# focused on the hardware side; these are the API-layer contracts).
# ---------------------------------------------------------------------------

# Reasonable per-control bounds. Wider than any real-world value to avoid
# rejecting legitimate inputs, narrow enough to catch obvious bugs (negative
# exposure, gain of 1000).
_EXPOSURE_MIN_US = 1
_EXPOSURE_MAX_US = 10_000_000   # 10 seconds; nothing realistic exceeds this
_GAIN_MIN = 1.0
_GAIN_MAX = 32.0
_COLOUR_GAIN_MIN = 0.1
_COLOUR_GAIN_MAX = 8.0


def _validated_settings_kwargs(payload: dict, sensor: SensorInfo) -> dict:
    """Return a kwargs dict safe to pass to CameraContext.apply_manual.

    Rejects unknown keys and out-of-range values with a clear ValueError.
    Coerces numeric types (JSON ints will arrive as int; we want float for
    gains).
    """
    allowed = {
        "exposure_time_us",
        "analogue_gain",
        "colour_gains",
        "lens_position",
        "scaler_crop",
    }
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"Unknown setting(s): {sorted(extra)}")
    out: dict = {}
    if "exposure_time_us" in payload:
        v = int(payload["exposure_time_us"])
        if not (_EXPOSURE_MIN_US <= v <= _EXPOSURE_MAX_US):
            raise ValueError(f"exposure_time_us out of range [{_EXPOSURE_MIN_US}, {_EXPOSURE_MAX_US}]")
        out["exposure_time_us"] = v
    if "analogue_gain" in payload:
        v = float(payload["analogue_gain"])
        if not (_GAIN_MIN <= v <= _GAIN_MAX):
            raise ValueError(f"analogue_gain out of range [{_GAIN_MIN}, {_GAIN_MAX}]")
        out["analogue_gain"] = v
    if "colour_gains" in payload:
        cg = payload["colour_gains"]
        if not (isinstance(cg, (list, tuple)) and len(cg) == 2):
            raise ValueError("colour_gains must be [red, blue]")
        r, b = float(cg[0]), float(cg[1])
        for name, val in (("red", r), ("blue", b)):
            if not (_COLOUR_GAIN_MIN <= val <= _COLOUR_GAIN_MAX):
                raise ValueError(
                    f"colour_gains {name}={val} out of range [{_COLOUR_GAIN_MIN}, {_COLOUR_GAIN_MAX}]"
                )
        out["colour_gains"] = (r, b)
    if "lens_position" in payload:
        if not sensor.supports_autofocus:
            raise ValueError("lens_position rejected: sensor has no autofocus")
        out["lens_position"] = float(payload["lens_position"])
    if "scaler_crop" in payload:
        sc = payload["scaler_crop"]
        if not (isinstance(sc, (list, tuple)) and len(sc) == 4):
            raise ValueError("scaler_crop must be [x, y, width, height]")
        x, y, w, h = (int(v) for v in sc)
        if w <= 0 or h <= 0:
            raise ValueError("scaler_crop width/height must be > 0")
        if x < 0 or y < 0:
            raise ValueError("scaler_crop x/y must be >= 0")
        if x + w > sensor.native_width or y + h > sensor.native_height:
            raise ValueError(
                f"scaler_crop exceeds sensor bounds ({sensor.native_width}x{sensor.native_height})"
            )
        out["scaler_crop"] = (x, y, w, h)
    return out


def _merge_profile_patch(existing: CameraProfile, payload: dict) -> CameraProfile:
    """Build a new CameraProfile from an existing one + a PATCH body.

    The PATCH body for /api/camera/profile is the full target profile shape
    (the Live tab sends what it wants the new state to be). We validate
    every field and bump `calibrated_at` to now so it's clear this profile
    was edited manually rather than calibrated fresh.
    """
    sensor_payload = payload.get("sensor", existing.sensor.__dict__)
    try:
        sensor = SensorInfo(**sensor_payload)
    except TypeError as e:
        raise ValueError(f"Invalid sensor: {e}")
    # Reuse the settings validator (raises on bad values).
    kwargs = _validated_settings_kwargs(
        {k: payload[k] for k in (
            "exposure_time_us", "analogue_gain", "colour_gains",
            "lens_position", "scaler_crop"
        ) if k in payload},
        sensor,
    )
    return CameraProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        calibrated_at=iso_now_local(),
        sensor=sensor,
        exposure_time_us=kwargs.get("exposure_time_us", existing.exposure_time_us),
        analogue_gain=kwargs.get("analogue_gain", existing.analogue_gain),
        colour_gains=(
            list(kwargs["colour_gains"]) if "colour_gains" in kwargs
            else existing.colour_gains
        ),
        lens_position=kwargs.get("lens_position", existing.lens_position),
        scaler_crop=(
            list(kwargs["scaler_crop"]) if "scaler_crop" in kwargs
            else existing.scaler_crop
        ),
        test_capture_path=existing.test_capture_path,
    )


app = create_app()
