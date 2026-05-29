"""Pi management: deploy code from this Mac to the Pi, check service status,
view logs.

This page is operational, not part of the data-analysis flow — feel free to
ignore it once your Pi is running steadily. Deploys are a thin wrapper around
`make pi-deploy PI_HOST=…`, which under the hood is `rsync pi/ …` + SSH
`./install.sh`.

SSH is invoked with `BatchMode=yes` so it can't prompt for a password — you
need either a passphrase-less key or an active ssh-agent. If you haven't
accepted the Pi's host key yet, run `ssh pi@<host> exit` once from a
terminal first.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import streamlit as st

from growzones import state
from growzones.locations import growzones_data_dir


st.set_page_config(page_title="Pi — GrowZones", layout="wide")

_PI_HOST_FILE = growzones_data_dir() / ".pi_host"
_DEFAULT_PI_HOST = "growzones.local"
_TAIL_LINES = 30


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_pi_host() -> str:
    if _PI_HOST_FILE.exists():
        return _PI_HOST_FILE.read_text().strip() or _DEFAULT_PI_HOST
    return _DEFAULT_PI_HOST


def _save_pi_host(host: str) -> None:
    growzones_data_dir()
    _PI_HOST_FILE.write_text(host.strip())


# ---------------------------------------------------------------------------
# Repo location (so we can `cd` to it for `make pi-deploy`)
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Walk up from this file looking for the Makefile."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "Makefile").exists():
            return parent
    raise RuntimeError(
        "Could not find a Makefile above this file; is the repo intact?"
    )


def _git_porcelain(cwd: Path) -> str:
    """Return `git status --porcelain` output; empty string if clean / not git."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

_SSH_FLAGS = ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]


def _ssh(host: str, remote_cmd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", *_SSH_FLAGS, f"pi@{host}", remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _stream_make_deploy(host: str, repo_root: Path, status) -> int:
    """Run `make pi-deploy PI_HOST=…` and stream stdout to the status widget.
    Returns the process exit code."""
    proc = subprocess.Popen(
        ["make", "pi-deploy", f"PI_HOST={host}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, ""):
        lines.append(raw.rstrip("\n"))
        # Show a rolling window so the page doesn't explode on huge rsync output.
        status.update(label=f"Deploying to {host}…", state="running")
        status.code("\n".join(lines[-50:]))
    proc.wait()
    return proc.returncode


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def main() -> None:
    state.location_selector_sidebar()
    st.title("Pi management")
    st.caption(
        "Push code to the Pi, check service status, view recent logs. "
        "Operational page — separate from the data-analysis flow."
    )

    # Tool availability check up front so we don't fail later with cryptic errors.
    missing = [t for t in ("make", "rsync", "ssh") if shutil.which(t) is None]
    if missing:
        st.error(
            f"Missing required tools on PATH: {', '.join(missing)}. "
            "Install Xcode Command Line Tools (`xcode-select --install`)."
        )
        st.stop()

    try:
        repo_root = _repo_root()
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    # --- Connection -----------------------------------------------------
    st.subheader("Connection")
    saved_host = _load_pi_host()
    host = st.text_input(
        "Pi hostname",
        value=saved_host,
        key="pi_host",
        help="Hostname or IP of the Pi. Default is the mDNS name set in Raspberry Pi Imager.",
    )
    if host and host != saved_host:
        _save_pi_host(host)

    if st.button("Test connection"):
        with st.spinner(f"Pinging pi@{host}…"):
            try:
                result = _ssh(host, "echo connected && systemctl is-active growzones", timeout=10)
            except subprocess.TimeoutExpired:
                st.error(f"Timed out connecting to pi@{host}.")
                return
        if result.returncode == 0:
            st.success(f"Connected to {host}. Service status: {result.stdout.strip()!r}")
        else:
            st.error(
                f"Couldn't reach pi@{host}.\n\n```\n{result.stderr or result.stdout}\n```\n"
                "Common causes: hostname not resolving (mDNS may take 30s on first boot), "
                "SSH host key not accepted yet (try `ssh pi@" + host + " exit` from a "
                "terminal first), or no ssh-agent / key set up."
            )

    # --- Deploy ---------------------------------------------------------
    st.divider()
    st.subheader("Deploy")

    dirty = _git_porcelain(repo_root)
    if dirty:
        st.warning(
            "Uncommitted changes in this repo — they'll be deployed but **not** "
            "in git history. Commit first if you want to be safe."
        )
        with st.expander("show uncommitted changes"):
            st.code(dirty, language="text")
    else:
        st.caption("Working tree is clean.")

    if st.button("Deploy to Pi", type="primary", key="btn_deploy"):
        with st.status(f"Deploying to {host}…", expanded=True) as status:
            try:
                code = _stream_make_deploy(host, repo_root, status)
            except FileNotFoundError as e:
                status.update(label="Deploy failed", state="error")
                st.error(f"{e}")
                return
        if code == 0:
            st.success(
                f"Deploy succeeded. The service has been restarted by "
                f"install.sh; open http://{host}/ to test."
            )
            st.link_button(f"Open http://{host}/", f"http://{host}/")
        else:
            st.error(f"Deploy failed (exit code {code}). See output above for details.")

    # --- Service status / logs -----------------------------------------
    st.divider()
    st.subheader("Pi service")
    cols = st.columns(2)
    if cols[0].button("systemctl status"):
        with st.spinner("Fetching status…"):
            try:
                result = _ssh(host, "systemctl status growzones --no-pager", timeout=15)
            except subprocess.TimeoutExpired:
                st.error("Timed out.")
                return
        st.code(result.stdout + result.stderr, language="text")
    if cols[1].button(f"Last {_TAIL_LINES} log lines"):
        with st.spinner("Fetching logs…"):
            try:
                result = _ssh(
                    host,
                    f"journalctl -u growzones --no-pager -n {_TAIL_LINES}",
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                st.error("Timed out.")
                return
        st.code(result.stdout + result.stderr, language="text")


main()
