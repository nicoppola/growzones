"""Local bundle storage for downloaded Pi sessions.

Layout under ~/Library/Application Support/growzones/:
    .pi_host
    bundles/
      <session_id>/
        manifest.json
        capture_log.jsonl
        HH-MM-SS.jpg
        ...
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

APP_NAME = "growzones"
DEFAULT_PI_HOST = "growzones.local"


def data_dir() -> Path:
    p = Path.home() / "Library/Application Support" / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def bundles_dir() -> Path:
    p = data_dir() / "bundles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def host_file() -> Path:
    return data_dir() / ".pi_host"


def load_pi_host() -> str:
    f = host_file()
    if f.exists():
        host = f.read_text().strip()
        if host:
            return host
    return DEFAULT_PI_HOST


def save_pi_host(host: str) -> None:
    host = host.strip()
    if not host:
        host = DEFAULT_PI_HOST
    host_file().write_text(host + "\n")


def bundle_dir(session_id: str) -> Path:
    return bundles_dir() / session_id


def bundle_exists_locally(session_id: str) -> bool:
    return bundle_dir(session_id).exists()


def list_local_bundles() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in sorted(bundles_dir().iterdir()) if bundles_dir().exists() else []:
        if not d.is_dir():
            continue
        mp = d / "manifest.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text())
        except Exception:
            continue
        out.append({
            "id": m.get("session_id", d.name),
            "started_at": m.get("started_at"),
            "stopped_at": m.get("stopped_at"),
            "interval_seconds": m.get("interval_seconds"),
            "image_count": m.get("image_count", _count_jpgs(d)),
            "total_bytes": m.get("total_bytes", _dir_size(d)),
            "local_path": str(d),
        })
    out.sort(key=lambda b: b["started_at"] or "", reverse=True)
    return out


def import_tar(tar_path: Path) -> str:
    """Extract a downloaded session tar into bundles/. Returns the session id."""
    with tarfile.open(tar_path, mode="r|") as tar:
        members = []
        roots: set[str] = set()
        for m in tar:
            # Tar entries look like "<session_id>/manifest.json"; capture the root.
            parts = m.name.split("/", 1)
            if len(parts) == 2:
                roots.add(parts[0])
            members.append(m)
            tar.extract(m, path=str(bundles_dir()))
    if len(roots) != 1:
        raise ValueError(f"Expected exactly one session root in tar, got {roots}")
    return next(iter(roots))


def open_bundle_in_finder(session_id: str) -> None:
    p = bundle_dir(session_id)
    if p.exists():
        subprocess.run(["open", str(p)], check=False)


def delete_local_bundle(session_id: str) -> None:
    p = bundle_dir(session_id)
    if p.exists():
        shutil.rmtree(p)


def total_local_bytes() -> int:
    return sum(_dir_size(d) for d in bundles_dir().iterdir() if d.is_dir()) if bundles_dir().exists() else 0


def _count_jpgs(d: Path) -> int:
    return sum(1 for _ in d.glob("*.jpg"))


def _dir_size(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
