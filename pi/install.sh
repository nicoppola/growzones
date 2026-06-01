#!/usr/bin/env bash
# Idempotent install for the GrowZones Pi service.
# Safe to re-run after partial failures; every step checks "already done?" first.
# Path-agnostic: PROJECT_DIR is resolved from the script's own location and
# substituted into the systemd unit at install time, so the repo can live
# anywhere (e.g. `~/growzones/pi`, `~/code/growzones-repo/pi`, etc.).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
STATE_DIR="/var/lib/growzones"
SERVICE_NAME="growzones"
SERVICE_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="${SUDO_USER:-$USER}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

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
    require_root_for install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
    require_root_for install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "${STATE_DIR}/setup_tests"
    require_root_for install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "${STATE_DIR}/sessions"
else
    echo "    ${STATE_DIR} already exists"
fi

say "6/7  Install systemd unit (substituting PROJECT_DIR=${PROJECT_DIR}, user=${SERVICE_USER})"
SRC_UNIT_TEMPLATE="${PROJECT_DIR}/systemd/${SERVICE_NAME}.service"
if [[ ! -f "$SRC_UNIT_TEMPLATE" ]]; then
    echo "ERROR: unit template missing at $SRC_UNIT_TEMPLATE" >&2
    exit 1
fi
# Render the template into a temp file with the resolved values, then install
# only if the resulting content differs from what's already on disk.
RENDERED_UNIT="$(mktemp /tmp/growzones-service.XXXXXX)"
trap 'rm -f "$RENDERED_UNIT"' EXIT
sed \
    -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    -e "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" \
    "$SRC_UNIT_TEMPLATE" > "$RENDERED_UNIT"

if [[ ! -f "$SERVICE_UNIT" ]] || ! cmp -s "$RENDERED_UNIT" "$SERVICE_UNIT"; then
    require_root_for cp "$RENDERED_UNIT" "$SERVICE_UNIT"
    require_root_for systemctl daemon-reload
fi
require_root_for systemctl enable "$SERVICE_NAME"
# Restart on every install run so code changes from rsync are picked up.
require_root_for systemctl restart "$SERVICE_NAME"

say "7/7  Done"
HOSTNAME_FQDN="$(hostname).local"
cat <<EOF

Setup complete. The Pi is now serving the capture API on port 80.

Verify from your Mac:
  curl http://${HOSTNAME_FQDN}/api/health
  # → {"ok": true, "has_profile": false, ...}

Then launch the Mac app from the repo root:
  make app
The app's sidebar will ask for the Pi hostname (default: ${HOSTNAME_FQDN}).

Useful commands:
  systemctl status ${SERVICE_NAME}        # is the service running?
  journalctl -u ${SERVICE_NAME} -f        # follow service logs
  ls ${STATE_DIR}/                        # state on disk (profile, sessions)
EOF
