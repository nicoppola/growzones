"""Thin HTTP wrapper around the Pi's `/api/*` endpoints, plus a single place
that owns the persisted Pi hostname.

The Pi already exposes everything as JSON over HTTP (see `pi/app/main.py`),
so the Mac-side Capture and Data pages are just HTTP clients. The Setup and
Live tabs stay on the Pi because SSE and MJPEG don't translate cleanly to
Streamlit's rerun model.

Error model: `PiError` carries a `kind` so pages can render specific guidance:
  - UNREACHABLE: DNS failure, route unreachable, timeout — Pi may be off.
  - SERVICE_DOWN: TCP refused on port 80 — Pi is up but the systemd unit isn't.
  - HTTP_ERROR: service returned 4xx/5xx — usually shows the FastAPI detail.
For SERVICE_DOWN we additionally call `diagnose_pi()` (SSH + journalctl) to
pull the actual crash trace, so the user doesn't have to go hunting.
"""
from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

import requests
import urllib3.exceptions

from .locations import growzones_data_dir


_PI_HOST_FILE = growzones_data_dir() / ".pi_host"
_DEFAULT_PI_HOST = "growzones.local"
_DEFAULT_TIMEOUT = 10
_EXPORT_TIMEOUT = 600  # tars of many days can take a while
_SSH_FLAGS = ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
              "-o", "StrictHostKeyChecking=accept-new"]


def load_pi_host() -> str:
    if _PI_HOST_FILE.exists():
        return _PI_HOST_FILE.read_text().strip() or _DEFAULT_PI_HOST
    return _DEFAULT_PI_HOST


def save_pi_host(host: str) -> None:
    _PI_HOST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PI_HOST_FILE.write_text(host.strip())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PiErrorKind(str, Enum):
    UNREACHABLE = "unreachable"      # hostname doesn't resolve / timeout / no route
    SERVICE_DOWN = "service_down"    # connection refused on the API port
    HTTP_ERROR = "http_error"        # service answered with non-2xx
    BAD_RESPONSE = "bad_response"    # service answered but the body wasn't JSON


class PiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: PiErrorKind,
        status: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.url = url

    @property
    def summary(self) -> str:
        """One-line, user-facing summary appropriate for an `st.error` title."""
        if self.kind is PiErrorKind.UNREACHABLE:
            return "Can't reach the Pi at all."
        if self.kind is PiErrorKind.SERVICE_DOWN:
            return "The Pi is up, but the GrowZones service isn't responding."
        if self.kind is PiErrorKind.HTTP_ERROR:
            return f"Pi returned HTTP {self.status}."
        return "Pi returned an unexpected response."


# ---------------------------------------------------------------------------
# SSH helper (shared with 07_Pi.py — single source of truth for invoking ssh)
# ---------------------------------------------------------------------------

@dataclass
class SSHResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def ssh_run(host: str, remote_cmd: str, *, timeout: int = 15) -> SSHResult:
    """Run a command on the Pi via SSH. Returns an SSHResult; on transport
    failure (timeout, key rejected, host unreachable) the returncode is
    non-zero and stderr describes why. Never raises — pages can decide what
    to do with the result."""
    try:
        proc = subprocess.run(
            ["ssh", *_SSH_FLAGS, f"pi@{host}", remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return SSHResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return SSHResult(124, "", f"SSH timed out after {timeout}s")
    except FileNotFoundError:
        return SSHResult(127, "", "ssh binary not found on PATH")


# ---------------------------------------------------------------------------
# Diagnosis: turn an opaque connection error into something actionable
# ---------------------------------------------------------------------------

@dataclass
class PiDiagnosis:
    """A multi-part diagnostic block, rendered by the pages."""
    headline: str
    detail: str
    ssh_attempted: bool = False
    ssh_ok: bool = False
    service_active: str | None = None
    recent_logs: str | None = None
    suggestions: tuple[str, ...] = ()


def diagnose_pi(host: str, error: PiError, *, log_lines: int = 25) -> PiDiagnosis:
    """Look at `error` and (when useful) SSH to the Pi to fetch service status
    + recent log output. Returns a structured diagnosis the page can render."""
    if error.kind is PiErrorKind.UNREACHABLE:
        # Distinguish "name doesn't resolve" from "host doesn't answer".
        resolves = _resolves(host)
        if not resolves:
            return PiDiagnosis(
                headline=f"{host!r} does not resolve.",
                detail=(
                    "mDNS / DNS can't translate this hostname to an IP. The Pi "
                    "may be off, on a different network, or named differently."
                ),
                suggestions=(
                    "Confirm the hostname matches what you set in Raspberry Pi Imager.",
                    "Try the Pi's IP address directly (run `arp -a` or check your router).",
                    "On first boot, mDNS can take ~30 seconds to advertise.",
                ),
            )
        return PiDiagnosis(
            headline=f"{host!r} resolves, but isn't answering.",
            detail="The IP came back from DNS but no response from the host.",
            suggestions=(
                "Is the Pi powered on?",
                "Is it on the same network as this Mac?",
                "Try `ping " + host + "` from a terminal.",
            ),
        )

    if error.kind is PiErrorKind.SERVICE_DOWN:
        # Pi is up; SSH in and find out why uvicorn isn't bound to port 80.
        active = ssh_run(host, "systemctl is-active growzones", timeout=10)
        logs = ssh_run(
            host,
            f"journalctl -u growzones --no-pager -n {log_lines}",
            timeout=15,
        )
        suggestions: list[str] = []
        active_value = active.stdout.strip() if active.ok else None
        if not active.ok:
            suggestions.append(
                "SSH didn't work (BatchMode=yes). Set up passwordless SSH: "
                "`ssh-copy-id pi@" + host + "`."
            )
        elif active_value == "failed":
            suggestions.append(
                "Service has exited. Check the log block below — that's the "
                "actual crash. After fixing the root cause, "
                "`sudo systemctl restart growzones` on the Pi."
            )
        elif active_value == "activating":
            suggestions.append(
                "Service is in a crash-restart loop. The log block below "
                "shows the most recent failure."
            )
        elif active_value == "inactive":
            suggestions.append(
                "Service is stopped. Start it: `sudo systemctl start growzones`."
            )
        return PiDiagnosis(
            headline="Connection refused on port 80 — the API isn't listening.",
            detail=(
                "The Pi accepted the TCP connection attempt but rejected it, "
                "which means uvicorn isn't bound to the port. Most likely the "
                "growzones service crashed at startup."
            ),
            ssh_attempted=True,
            ssh_ok=active.ok,
            service_active=active_value,
            recent_logs=(logs.stdout + logs.stderr).strip() if (logs.stdout or logs.stderr) else None,
            suggestions=tuple(suggestions),
        )

    if error.kind is PiErrorKind.HTTP_ERROR:
        return PiDiagnosis(
            headline=f"Pi service returned HTTP {error.status}.",
            detail=str(error),
            suggestions=(
                "If 409: a camera profile is required — run the Setup wizard "
                "at http://" + host + "/setup.",
                "If 5xx: check the Pi page for `journalctl -u growzones` output.",
            ),
        )

    return PiDiagnosis(
        headline="Unexpected response from the Pi.",
        detail=str(error),
    )


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

@dataclass
class PiClient:
    host: str
    timeout: int = _DEFAULT_TIMEOUT

    @property
    def base(self) -> str:
        return f"http://{self.host}"

    def get(self, path: str, **params) -> Any:
        return self._json("GET", path, params=params or None)

    def patch(self, path: str, body: dict) -> Any:
        return self._json("PATCH", path, json=body)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._json("POST", path, json=body)

    def delete(self, path: str, **params) -> Any:
        return self._json("DELETE", path, params=params or None)

    def stream_tar(self, path: str, dest: Path, **params) -> int:
        """Stream `/api/export...` straight to `dest`. Returns bytes written."""
        url = self.base + path
        params = _alias_from(params)
        try:
            with requests.get(url, params=params, stream=True, timeout=_EXPORT_TIMEOUT) as r:
                r.raise_for_status()
                written = 0
                with dest.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):  # 1 MiB
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                return written
        except requests.HTTPError as e:
            raise PiError(
                _format_http_error_response(e.response),
                kind=PiErrorKind.HTTP_ERROR,
                status=e.response.status_code, url=url,
            ) from e
        except requests.RequestException as e:
            raise _classify_request_exception(e, url) from e

    # -- internal ----------------------------------------------------------

    def _json(self, method: str, path: str, **kwargs) -> Any:
        url = self.base + path
        if "params" in kwargs and kwargs["params"]:
            kwargs["params"] = _alias_from(kwargs["params"])
        try:
            r = requests.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise _classify_request_exception(e, url) from e
        if not r.ok:
            raise PiError(
                _format_http_error_response(r),
                kind=PiErrorKind.HTTP_ERROR,
                status=r.status_code, url=url,
            )
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise PiError(
                f"Expected JSON from {url}, got: {r.text[:200]!r}",
                kind=PiErrorKind.BAD_RESPONSE, url=url,
            ) from e


def _alias_from(params: dict) -> dict:
    """`from` is a Python keyword; callers pass `from_`. Pi accepts `from`."""
    if "from_" in params:
        params = dict(params)
        params["from"] = params.pop("from_")
    return params


def _classify_request_exception(e: requests.RequestException, url: str) -> PiError:
    """Map a low-level requests/urllib3 failure to a PiErrorKind."""
    text = str(e)
    # ConnectionRefusedError surfaces in two ways depending on Python version /
    # urllib3 internals: as the literal phrase "Connection refused", or as
    # errno 61 (ECONNREFUSED on macOS) / 111 (Linux). Cover both.
    is_refused = (
        "Connection refused" in text
        or "Errno 61" in text
        or "Errno 111" in text
        or isinstance(getattr(e, "__cause__", None), ConnectionRefusedError)
    )
    if is_refused:
        return PiError(
            f"Connection refused: {url}",
            kind=PiErrorKind.SERVICE_DOWN, url=url,
        )
    return PiError(
        f"Could not reach {url}: {e}",
        kind=PiErrorKind.UNREACHABLE, url=url,
    )


def _format_http_error_response(r: requests.Response) -> str:
    detail = ""
    try:
        body = r.json()
        if isinstance(body, dict) and "detail" in body:
            detail = f": {body['detail']}"
    except ValueError:
        if r.text:
            detail = f": {r.text[:200]}"
    return f"{r.request.method} {r.request.url} → {r.status_code}{detail}"


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

def render_pi_error(st_module, host: str, error: PiError) -> None:
    """Render a structured error block in Streamlit. Pages call this in their
    `except PiError` branch instead of hand-rolling formatting."""
    diag = diagnose_pi(host, error)
    st_module.error(f"**{diag.headline}**\n\n{diag.detail}")

    if diag.suggestions:
        with st_module.expander("Suggestions", expanded=True):
            for s in diag.suggestions:
                st_module.markdown(f"- {s}")

    if diag.ssh_attempted:
        active = diag.service_active or "unknown"
        st_module.caption(
            f"`systemctl is-active growzones` over SSH → **{active}**"
        )
        if diag.recent_logs:
            with st_module.expander(f"Last log lines from `journalctl -u growzones`", expanded=True):
                st_module.code(diag.recent_logs, language="text")


def iter_capture_state(client: PiClient) -> Iterator[dict]:
    """Generator that yields successive `/api/capture/state` snapshots.
    Streamlit pages don't actually consume this as a generator (they use
    `st_autorefresh`), but exposing it here keeps the polling pattern
    documented in one place."""
    while True:
        yield client.get("/api/capture/state")
