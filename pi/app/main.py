"""FastAPI app factory + routes.

The Pi is a pure JSON/MJPEG API — no templates, no static files. The Mac
Streamlit app is the only client.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import export as export_mod
from . import mjpeg, storage
from .calibrate import CalibrationRunner
from .camera import Camera
from .sessions import NoProfileSaved, SessionAlreadyActive, SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("growzones")


class SettingsPatch(BaseModel):
    exposure_time_us: int | None = None
    analogue_gain: float | None = None
    colour_gains: list[float] | None = None
    scaler_crop: list[int] | None = None
    lens_position: float | None = None


class StartSessionBody(BaseModel):
    interval_seconds: int = 900


def create_app() -> FastAPI:
    camera = Camera()
    sessions = SessionManager(camera)
    calibrator = CalibrationRunner(camera)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage.ensure_layout()
        camera.start()
        # If a profile is saved, apply it so the live preview reflects locked settings
        profile = storage.load_profile()
        if profile is not None:
            camera.apply_settings({
                "exposure_time_us": profile.get("exposure_time_us"),
                "analogue_gain": profile.get("analogue_gain"),
                "colour_gains": profile.get("colour_gains"),
                "lens_position": profile.get("lens_position"),
                "scaler_crop": profile.get("scaler_crop"),
            })
        log.info("growzones-pi ready")
        yield
        camera.stop()

    app = FastAPI(title="growzones-pi", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/stream.mjpg")
    def stream() -> StreamingResponse:
        return StreamingResponse(mjpeg.stream_generator(camera), media_type=mjpeg.content_type())

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "has_profile": storage.PROFILE_PATH.exists(),
            "pi_free_bytes": storage.free_bytes(),
            "active_session_id": sessions.active_id(),
        }

    @app.get("/api/camera/profile")
    def get_profile() -> dict[str, Any]:
        profile = storage.load_profile()
        if profile is None:
            raise HTTPException(404, "No camera profile saved")
        return profile

    @app.get("/api/camera/settings")
    def get_settings() -> dict[str, Any]:
        return camera.live_settings()

    @app.patch("/api/camera/settings")
    def patch_settings(body: SettingsPatch) -> dict[str, Any]:
        camera.apply_settings(body.model_dump(exclude_none=True))
        return camera.live_settings()

    @app.post("/api/camera/settings/save-to-profile")
    def save_settings_to_profile() -> dict[str, Any]:
        live = camera.live_settings()
        if not live:
            raise HTTPException(400, "No live settings to save")
        sensor = camera.sensor_info()
        profile = storage.load_profile() or {}
        profile.update({
            "schema_version": storage.SCHEMA_VERSION,
            "calibrated_at": storage.now_iso(),
            "sensor": sensor,
            "exposure_time_us": live.get("exposure_time_us", profile.get("exposure_time_us")),
            "analogue_gain": live.get("analogue_gain", profile.get("analogue_gain")),
            "colour_gains": list(live.get("colour_gains", profile.get("colour_gains") or [])),
            "lens_position": live.get("lens_position", profile.get("lens_position")),
            "scaler_crop": list(live.get("scaler_crop", profile.get("scaler_crop") or [])),
        })
        storage.save_profile(profile)
        return profile

    @app.post("/api/camera/snapshot")
    def snapshot() -> Response:
        jpeg, _meta = camera.capture_still()
        return Response(content=jpeg, media_type="image/jpeg")

    @app.post("/api/setup/calibrate")
    def start_calibrate() -> dict[str, Any]:
        if not calibrator.start():
            raise HTTPException(409, "Calibration already running")
        return {"ok": True}

    @app.get("/api/setup/status")
    def setup_status() -> dict[str, Any]:
        return calibrator.status()

    @app.post("/api/setup/save-profile")
    def save_profile_from_candidate() -> dict[str, Any]:
        candidate = calibrator.candidate()
        if candidate is None:
            raise HTTPException(400, "No calibration candidate to save")
        storage.save_profile(candidate)
        # Saving the profile means "use this now" — push it onto the live
        # camera so the MJPEG preview, snapshots, and any session captures
        # immediately reflect the saved values.
        camera.apply_settings({
            "exposure_time_us": candidate.get("exposure_time_us"),
            "analogue_gain": candidate.get("analogue_gain"),
            "colour_gains": candidate.get("colour_gains"),
            "lens_position": candidate.get("lens_position"),
            "scaler_crop": candidate.get("scaler_crop"),
        })
        return candidate

    @app.get("/api/setup/test-image/{name}")
    def test_image(name: str) -> FileResponse:
        path = storage.SETUP_TESTS_DIR / name
        if not path.exists() or ".." in name:
            raise HTTPException(404, "Test image not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/sessions")
    def start_session(body: StartSessionBody) -> dict[str, Any]:
        try:
            return sessions.start(body.interval_seconds)
        except NoProfileSaved as e:
            raise HTTPException(400, str(e))
        except SessionAlreadyActive as e:
            raise HTTPException(409, str(e))

    @app.post("/api/sessions/{session_id}/stop")
    def stop_session(session_id: str) -> dict[str, Any]:
        try:
            return sessions.stop(session_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        return sessions.list_sessions()

    @app.get("/api/sessions/{session_id}/export")
    def export_session(session_id: str) -> StreamingResponse:
        if not storage.session_dir(session_id).exists():
            raise HTTPException(404, "Session not found")
        gen = export_mod.stream_session_tar(session_id)
        return StreamingResponse(
            gen,
            media_type="application/x-tar",
            headers={"Content-Disposition": f'attachment; filename="{export_mod.tar_filename(session_id)}"'},
        )

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        if sessions.active_id() == session_id:
            raise HTTPException(409, "Cannot delete the active session; stop it first")
        if not storage.session_dir(session_id).exists():
            raise HTTPException(404, "Session not found")
        storage.delete_session(session_id)
        return {"ok": True, "deleted": session_id}

    return app


app = create_app()
