#!/usr/bin/env bash
# Idempotent install for the GrowZones Pi service.
# Safe to re-run after partial failures; every step checks "already done?" first.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
STATE_DIR="/var/lib/growzones"
SERVICE_NAME="growzones"
SERVICE_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

require_root_for() {
    # Convenience: call as `require_root_for <cmd> [args...]`. Uses sudo when not root.
    if [[ $EUID -ne 0 ]]; then sudo "$@"; else "$@"; fi
}

say "1/7  Enable camera interface"
if [[ "$(raspi-config nonint get_camera 2>/dev/null || echo 1)" != "0" ]]; then
    require_root_for raspi-config nonint do_camera 0
else
    echo "    already enabled"
fi

say "2/7  Install apt dependencies"
APT_PKGS=(
    python3-picamera2
    python3-libcamera
    python3-kms++
    python3-pip
    python3-venv
    python3-numpy
    python3-pillow
    libcamera-apps
    avahi-daemon
)
missing=()
for pkg in "${APT_PKGS[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then missing+=("$pkg"); fi
done
if (( ${#missing[@]} )); then
    require_root_for apt-get update
    require_root_for apt-get install -y "${missing[@]}"
else
    echo "    all packages already present"
fi

say "3/7  Create venv (with system site-packages so picamera2 is importable)"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
else
    echo "    venv already exists at $VENV_DIR"
fi

say "4/7  Install Pi app pip deps into venv"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "$PROJECT_DIR"

say "5/7  Create state dir at ${STATE_DIR}"
if [[ ! -d "$STATE_DIR" ]]; then
    require_root_for install -d -o "$USER" -g "$USER" "$STATE_DIR"
    require_root_for install -d -o "$USER" -g "$USER" "${STATE_DIR}/setup_tests"
    require_root_for install -d -o "$USER" -g "$USER" "${STATE_DIR}/captures"
else
    echo "    ${STATE_DIR} already exists"
fi

say "6/7  Install systemd unit"
SRC_UNIT="${PROJECT_DIR}/systemd/${SERVICE_NAME}.service"
if [[ ! -f "$SRC_UNIT" ]]; then
    echo "ERROR: unit file missing at $SRC_UNIT" >&2
    exit 1
fi
# Compare to detect changes; copy + reload if different (or missing).
if [[ ! -f "$SERVICE_UNIT" ]] || ! cmp -s "$SRC_UNIT" "$SERVICE_UNIT"; then
    require_root_for cp "$SRC_UNIT" "$SERVICE_UNIT"
    require_root_for systemctl daemon-reload
fi
require_root_for systemctl enable --now "$SERVICE_NAME"

say "7/7  Done"
HOSTNAME_FQDN="$(hostname).local"
cat <<EOF

Setup complete. Open http://${HOSTNAME_FQDN}/ in your browser
(from a Mac on the same network; mDNS resolves the hostname via Bonjour).

Useful commands:
  systemctl status ${SERVICE_NAME}        # is the service running?
  journalctl -u ${SERVICE_NAME} -f        # follow service logs
  ls ${STATE_DIR}/                        # state on disk (profile, captures)

If the camera profile hasn't been calibrated yet, the web UI will
redirect you to the Setup tab on first load.
EOF
