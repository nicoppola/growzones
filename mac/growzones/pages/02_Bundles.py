"""Bundles page — merged table of Pi sessions + local bundles."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import streamlit as st

from growzones import bundles
from growzones.pi_client import PiClient, PiError
from growzones.sidebar import pi_health, render_sidebar


st.set_page_config(page_title="Bundles — GrowZones", layout="wide")
client = render_sidebar()
health = pi_health()


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _merge_bundles(pi_list: list[dict], local_list: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for s in pi_list:
        by_id[s["id"]] = {
            "id": s["id"],
            "started_at": s.get("started_at"),
            "stopped_at": s.get("stopped_at"),
            "image_count": s.get("image_count", 0),
            "total_bytes": s.get("total_bytes", 0),
            "on_pi": True,
            "on_mac": False,
            "active": s.get("active", False),
            "local_path": None,
        }
    for b in local_list:
        if b["id"] in by_id:
            by_id[b["id"]]["on_mac"] = True
            by_id[b["id"]]["local_path"] = b["local_path"]
        else:
            by_id[b["id"]] = {
                "id": b["id"],
                "started_at": b.get("started_at"),
                "stopped_at": b.get("stopped_at"),
                "image_count": b.get("image_count", 0),
                "total_bytes": b.get("total_bytes", 0),
                "on_pi": False,
                "on_mac": True,
                "active": False,
                "local_path": b["local_path"],
            }
    rows = list(by_id.values())
    rows.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return rows


st.title("Bundles")

pi_list: list[dict] = []
pi_offline = health is None
if not pi_offline:
    try:
        pi_list = client.list_sessions()
    except PiError as e:
        st.error(str(e))
        pi_offline = True
local_list = bundles.list_local_bundles()
rows = _merge_bundles(pi_list, local_list)

if not rows:
    st.info("No sessions yet. Start one from the Camera page.")
    st.stop()


# Header row
hcols = st.columns([2, 2, 1, 1, 1, 1, 3])
for col, label in zip(hcols, ["Started", "Stopped", "Images", "Size", "Pi", "Mac", "Actions"]):
    col.markdown(f"**{label}**")


def _download_bundle(row: dict) -> None:
    sid = row["id"]
    st.session_state[f"dl_progress_{sid}"] = 0
    progress = st.progress(0.0, text=f"Downloading {sid}…")
    total_expected = max(row.get("total_bytes") or 0, 1)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tar_path = Path(tmp.name)
    try:
        for downloaded in client.download_session(sid, tar_path):
            frac = min(downloaded / total_expected, 0.99)
            progress.progress(frac, text=f"Downloading {sid}… {_human_bytes(downloaded)}")
        progress.progress(1.0, text="Extracting…")
        bundles.import_tar(tar_path)
        st.toast(f"Downloaded {sid}")
    except PiError as e:
        st.error(str(e))
    finally:
        if tar_path.exists():
            tar_path.unlink()


for row in rows:
    sid = row["id"]
    cols = st.columns([2, 2, 1, 1, 1, 1, 3])
    cols[0].write(f"`{row.get('started_at') or '—'}`")
    if row["active"]:
        cols[1].write("🔴 recording")
    else:
        cols[1].write(f"`{row.get('stopped_at') or '—'}`")
    cols[2].write(row.get("image_count", 0))
    cols[3].write(_human_bytes(row.get("total_bytes", 0)))
    cols[4].write("●" if row["on_pi"] else "○")
    cols[5].write("●" if row["on_mac"] else "○")

    with cols[6]:
        action_cols = st.columns(3)
        # Download
        if row["on_pi"] and not row["on_mac"] and not row["active"]:
            if action_cols[0].button("⬇ Download", key=f"dl_{sid}"):
                _download_bundle(row)
                time.sleep(0.3)
                st.rerun()
        # Open folder
        if row["on_mac"]:
            if action_cols[1].button("📂 Open", key=f"open_{sid}"):
                bundles.open_bundle_in_finder(sid)
        # Delete on Pi (two-step)
        if row["on_pi"] and not row["active"]:
            armed_key = f"arm_del_{sid}"
            armed = st.session_state.get(armed_key, False)
            label = "✓ Confirm" if armed else "🗑 Delete Pi"
            if action_cols[2].button(label, key=f"del_{sid}"):
                if armed:
                    try:
                        client.delete_session(sid)
                        st.session_state[armed_key] = False
                        st.toast(f"Deleted {sid} from Pi")
                        time.sleep(0.3)
                        st.rerun()
                    except PiError as e:
                        st.error(str(e))
                else:
                    st.session_state[armed_key] = True
                    st.rerun()


# Footer totals
st.divider()
fcols = st.columns(3)
pi_total = sum(r.get("total_bytes", 0) for r in rows if r["on_pi"])
mac_total = bundles.total_local_bytes()
pi_free = (health or {}).get("pi_free_bytes", 0)
fcols[0].metric("Used on Pi", _human_bytes(pi_total))
fcols[1].metric("Used on Mac", _human_bytes(mac_total))
fcols[2].metric("Free on Pi", _human_bytes(pi_free))
