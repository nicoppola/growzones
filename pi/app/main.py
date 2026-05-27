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
    SETUP_TESTS_DIR,
    CameraContext,
    load_profile,
)
from .mjpeg import mjpeg_stream

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open the camera once at startup; close it cleanly on shutdown."""
    log.info("Opening camera…")
    app.state.camera = CameraContext()
    try:
        yield
    finally:
        log.info("Closing camera…")
        app.state.camera.close()


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
        return _stub_page(request, "Live", "Live preview + manual controls — coming next milestone.")

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

    # --- Camera state / profile -----------------------------------------

    @app.get("/api/camera/profile")
    async def get_profile():
        profile = load_profile()
        if profile is None:
            raise HTTPException(status_code=404, detail="No saved profile")
        return profile.to_dict()

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


app = create_app()
