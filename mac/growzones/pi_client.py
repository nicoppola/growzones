"""HTTP client for the Pi capture API.

One class `PiClient(host)` wrapping every endpoint the Mac app needs. Raises
`PiUnreachable` with a clear message when the Pi can't be reached so the UI
can render specific guidance.
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests


DEFAULT_HOST = "growzones.local"
DEFAULT_TIMEOUT_S = 10
HEALTH_TIMEOUT_S = 3


class PiError(Exception):
    """Base class for all Pi client errors."""

    def __init__(self, message: str, *, kind: str = "ERROR") -> None:
        super().__init__(message)
        self.kind = kind


class PiUnreachable(PiError):
    def __init__(self, host: str, detail: str) -> None:
        super().__init__(f"Could not reach Pi at {host}: {detail}", kind="UNREACHABLE")
        self.host = host


class PiHttpError(PiError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Pi returned HTTP {status}: {detail}", kind="HTTP_ERROR")
        self.status = status


class PiClient:
    def __init__(self, host: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.host = host
        self.timeout = timeout

    def base_url(self) -> str:
        return f"http://{self.host}"

    def stream_url(self) -> str:
        return f"{self.base_url()}/stream.mjpg"

    # ---------- generic plumbing ----------

    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs) -> requests.Response:
        url = f"{self.base_url()}{path}"
        try:
            resp = requests.request(method, url, timeout=timeout or self.timeout, **kwargs)
        except (requests.ConnectionError, socket.gaierror) as e:
            raise PiUnreachable(self.host, str(e)) from e
        except requests.Timeout as e:
            raise PiUnreachable(self.host, f"timeout after {timeout or self.timeout}s") from e
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise PiHttpError(resp.status_code, detail)
        return resp

    # ---------- endpoints ----------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health", timeout=HEALTH_TIMEOUT_S).json()

    def get_profile(self) -> dict[str, Any] | None:
        try:
            return self._request("GET", "/api/camera/profile").json()
        except PiHttpError as e:
            if e.status == 404:
                return None
            raise

    def get_settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/camera/settings").json()

    def patch_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", "/api/camera/settings", json=settings).json()

    def save_settings_to_profile(self) -> dict[str, Any]:
        return self._request("POST", "/api/camera/settings/save-to-profile").json()

    def snapshot(self) -> bytes:
        return self._request("POST", "/api/camera/snapshot", timeout=30).content

    def start_calibrate(self) -> dict[str, Any]:
        return self._request("POST", "/api/setup/calibrate").json()

    def setup_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/setup/status").json()

    def save_calibration(self) -> dict[str, Any]:
        return self._request("POST", "/api/setup/save-profile").json()

    def test_image_url(self, name: str) -> str:
        return f"{self.base_url()}/api/setup/test-image/{quote(name)}"

    def start_session(self, interval_seconds: int) -> dict[str, Any]:
        return self._request("POST", "/api/sessions", json={"interval_seconds": interval_seconds}).json()

    def stop_session(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{quote(session_id)}/stop").json()

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/sessions").json()

    def delete_session(self, session_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/sessions/{quote(session_id)}").json()

    def download_session(self, session_id: str, dest_tar: Path, *, chunk_size: int = 64 * 1024) -> Iterator[int]:
        """Stream the session tar into dest_tar. Yields cumulative byte count as it goes."""
        url = f"{self.base_url()}/api/sessions/{quote(session_id)}/export"
        try:
            with requests.get(url, stream=True, timeout=self.timeout) as resp:
                if resp.status_code >= 400:
                    raise PiHttpError(resp.status_code, resp.text)
                dest_tar.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with dest_tar.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        total += len(chunk)
                        yield total
        except (requests.ConnectionError, socket.gaierror) as e:
            raise PiUnreachable(self.host, str(e)) from e
        except requests.Timeout as e:
            raise PiUnreachable(self.host, f"timeout") from e
