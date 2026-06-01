"""Filesystem layout for Pi-side state.

All paths live under /var/lib/growzones/:
    camera_profile.json
    setup_tests/<timestamp>.jpg
    sessions/<session_id>/manifest.json
    sessions/<session_id>/capture_log.jsonl
    sessions/<session_id>/<HH-MM-SS>.jpg
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("GROWZONES_STATE_DIR", "/var/lib/growzones"))
PROFILE_PATH = ROOT / "camera_profile.json"
SETUP_TESTS_DIR = ROOT / "setup_tests"
SESSIONS_DIR = ROOT / "sessions"

SCHEMA_VERSION = 1


def ensure_layout() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    SETUP_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict[str, Any] | None:
    if not PROFILE_PATH.exists():
        return None
    return json.loads(PROFILE_PATH.read_text())


def save_profile(profile: dict[str, Any]) -> None:
    PROFILE_PATH.write_text(json.dumps(profile, indent=2))


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def list_session_ids() -> list[str]:
    if not SESSIONS_DIR.exists():
        return []
    return sorted(p.name for p in SESSIONS_DIR.iterdir() if p.is_dir())


def session_manifest_path(session_id: str) -> Path:
    return session_dir(session_id) / "manifest.json"


def session_log_path(session_id: str) -> Path:
    return session_dir(session_id) / "capture_log.jsonl"


def load_manifest(session_id: str) -> dict[str, Any]:
    return json.loads(session_manifest_path(session_id).read_text())


def write_manifest(session_id: str, manifest: dict[str, Any]) -> None:
    session_manifest_path(session_id).write_text(json.dumps(manifest, indent=2))


def append_capture_log(session_id: str, entry: dict[str, Any]) -> None:
    path = session_log_path(session_id)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def session_image_paths(session_id: str) -> list[Path]:
    d = session_dir(session_id)
    if not d.exists():
        return []
    return sorted(d.glob("*.jpg"))


def session_size_bytes(session_id: str) -> int:
    return sum(p.stat().st_size for p in session_dir(session_id).rglob("*") if p.is_file())


def delete_session(session_id: str) -> None:
    d = session_dir(session_id)
    if d.exists():
        shutil.rmtree(d)


def free_bytes() -> int:
    ensure_layout()
    usage = shutil.disk_usage(ROOT)
    return usage.free


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
