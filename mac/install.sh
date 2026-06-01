#!/usr/bin/env bash
# Idempotent install for the GrowZones Mac app.
# - Verifies Python 3.13 is present
# - Creates mac/.venv (next to this script) with the right Python
# - pip installs the package in editable mode (so edits are picked up live)
# Safe to re-run after partial failures.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_REQ="3.13"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }


say "1/4  Check prerequisites"

# Prefer python3.13 specifically; fall back to python3 if it claims 3.13+.
PYTHON_BIN=""
if command -v "python${PYTHON_REQ}" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "python${PYTHON_REQ}")"
elif command -v python3 >/dev/null 2>&1; then
    py_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [[ "$py_version" == "$PYTHON_REQ" ]] || [[ "$py_version" > "$PYTHON_REQ" ]]; then
        PYTHON_BIN="$(command -v python3)"
    fi
fi
if [[ -z "$PYTHON_BIN" ]]; then
    die "Python ${PYTHON_REQ}+ not found. Install with: brew install python@${PYTHON_REQ}"
fi
ok "Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"


say "2/4  Create venv at ${VENV_DIR}"
if [[ -d "$VENV_DIR" ]]; then
    if "${VENV_DIR}/bin/python" --version >/dev/null 2>&1; then
        ok "venv already exists and is healthy"
    else
        warn "existing venv is broken; recreating"
        rm -rf "$VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        ok "venv recreated"
    fi
else
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "venv created"
fi


say "3/4  Install Python deps"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "$PROJECT_DIR"
ok "deps installed"


say "4/4  Verify the package imports"
if "${VENV_DIR}/bin/python" -c "from growzones import bundles, pi_client, sidebar" 2>/dev/null; then
    ok "growzones package imports cleanly"
else
    die "growzones package failed to import — run \`${VENV_DIR}/bin/python -c 'from growzones import bundles'\` for details"
fi


cat <<EOF

Install complete.

Launch the app from the repo root:
  make app

Or activate the venv and run streamlit directly:
  source ${VENV_DIR}/bin/activate
  streamlit run growzones/growzones_app.py

Mac state (bundles, Pi hostname) lives at:
  ~/Library/Application Support/growzones/
EOF
