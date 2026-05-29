#!/usr/bin/env bash
# Idempotent install for the GrowZones Mac app.
# - Verifies Python 3.13 and ffmpeg are present
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

if ! command -v ffmpeg >/dev/null 2>&1; then
    die "ffmpeg not found. Install with: brew install ffmpeg"
fi
ok "ffmpeg: $(command -v ffmpeg)"


say "2/4  Create venv at ${VENV_DIR}"
if [[ -d "$VENV_DIR" ]]; then
    # Sanity-check the existing venv is still on the right Python — if the user
    # upgraded Homebrew Python, the venv's symlink may dangle.
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


say "3/4  Install Python deps (this can take a few minutes the first time)"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "$PROJECT_DIR"
ok "deps installed"


say "4/5  Verify the package imports"
if "${VENV_DIR}/bin/python" -c "import growzones; from growzones import cli, bundle, zones, heatmap, timelapse" 2>/dev/null; then
    ok "growzones package imports cleanly"
else
    die "growzones package failed to import — run \`${VENV_DIR}/bin/python -c 'import growzones'\` for details"
fi


say "5/5  Optional: passwordless SSH to a Pi"
echo "    Needed by the Streamlit Deploy button and \`make pi-deploy\`."

# Skip the interactive bits if stdin isn't a terminal (e.g. \`curl ... | bash\`).
if [[ ! -t 0 ]]; then
    warn "stdin is not a TTY — skipping SSH setup. Re-run interactively to set it up."
else
    # Find an existing SSH key (anything in ~/.ssh/id_*).
    KEY_PATH=""
    for candidate in ~/.ssh/id_ed25519 ~/.ssh/id_rsa ~/.ssh/id_ecdsa; do
        if [[ -f "$candidate" ]]; then
            KEY_PATH="$candidate"
            break
        fi
    done

    read -rp "    Set up Pi SSH now? [Y/n] " setup_ssh
    setup_ssh="${setup_ssh:-Y}"

    if [[ ! "$setup_ssh" =~ ^[Yy] ]]; then
        warn "skipped; re-run install.sh later to set this up"
    else
        # --- Generate key if missing -------------------------------------
        if [[ -z "$KEY_PATH" ]]; then
            echo "    No SSH key found in ~/.ssh/."
            read -rp "    Generate ed25519 keypair at ~/.ssh/id_ed25519 (no passphrase)? [Y/n] " gen
            gen="${gen:-Y}"
            if [[ "$gen" =~ ^[Yy] ]]; then
                mkdir -p ~/.ssh && chmod 700 ~/.ssh
                ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "growzones-$(whoami)@$(hostname -s)"
                KEY_PATH=~/.ssh/id_ed25519
                ok "key created at $KEY_PATH"
            else
                warn "skipped key generation; SSH-based deploy won't work without one"
                KEY_PATH=""
            fi
        else
            ok "using existing SSH key at $KEY_PATH"
        fi

        # --- Copy the key to the Pi (if we have one) --------------------
        if [[ -n "$KEY_PATH" ]]; then
            read -rp "    Pi hostname [growzones.local]: " pi_host
            pi_host="${pi_host:-growzones.local}"

            echo "    Checking whether passwordless SSH to pi@$pi_host already works…"
            if ssh -o BatchMode=yes -o ConnectTimeout=5 \
                   -o StrictHostKeyChecking=accept-new \
                   "pi@$pi_host" 'exit' 2>/dev/null; then
                ok "passwordless SSH to $pi_host already works — nothing to do"
            else
                echo "    Copying public key to pi@$pi_host."
                echo "    You'll be asked for the Pi's password ONE time."
                if ssh-copy-id -o StrictHostKeyChecking=accept-new "pi@$pi_host"; then
                    if ssh -o BatchMode=yes -o ConnectTimeout=5 \
                           "pi@$pi_host" 'exit' 2>/dev/null; then
                        ok "passwordless SSH to $pi_host confirmed"
                    else
                        warn "key copied but passwordless test still failed — check Pi-side ~/.ssh/authorized_keys"
                    fi
                else
                    warn "ssh-copy-id failed; you can retry later with: ssh-copy-id pi@$pi_host"
                fi
            fi
        fi
    fi
fi


cat <<EOF

Install complete.

Activate the venv:
  source ${VENV_DIR}/bin/activate

Common things to run:
  streamlit run growzones/growzones_app.py    # launch the app (http://localhost:8501)
  growzones --help                            # CLI
  python smoke_test.py                        # end-to-end pipeline smoke

…or use the Makefile from the repo root:
  make app
  make cli
  make smoke

Mac state on disk lives at:
  ~/Library/Application Support/growzones/data/
EOF
