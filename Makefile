# GrowZones — convenience targets. Run `make help` for the list.
# Most targets are Mac-side; pi-deploy pushes the Pi tree to the configured host.

.PHONY: help install-mac app pi-deploy clean

PI_HOST ?= growzones.local
MAC_VENV := mac/.venv

help:
	@echo "GrowZones make targets"
	@echo
	@echo "  make install-mac    Provision mac/.venv (Python 3.13) and pip install -e"
	@echo "  make app            Launch the Streamlit app at http://localhost:8501"
	@echo "  make pi-deploy      rsync pi/ to PI_HOST and run install.sh (default: growzones.local)"
	@echo "  make clean          Remove mac/.venv and Python caches"

install-mac:
	./mac/install.sh

app: $(MAC_VENV)/bin/python
	cd mac && .venv/bin/streamlit run growzones/growzones_app.py

# macOS ships Apple's openrsync as /usr/bin/rsync; its filter-rule wire format
# isn't understood by GNU rsync 3.2.x on the Pi (receiver-side recv_rules
# buffer overflow). Prefer Homebrew's GNU rsync when available.
RSYNC := $(shell command -v /opt/homebrew/bin/rsync 2>/dev/null || command -v /usr/local/bin/rsync 2>/dev/null || command -v rsync 2>/dev/null)

pi-deploy:
	@test -n "$(RSYNC)" || { echo "rsync required"; exit 1; }
	@case "$$($(RSYNC) --version 2>&1 | head -1)" in \
	  *openrsync*) echo "ERROR: $(RSYNC) is Apple's openrsync, which is incompatible with GNU rsync on the Pi."; \
	               echo "       Install GNU rsync: brew install rsync"; exit 1;; \
	esac
	@echo "Pushing pi/ to pi@$(PI_HOST):/home/pi/growzones/ ..."
	$(RSYNC) -avz --delete-excluded \
	  --exclude-from=pi/.rsyncignore \
	  pi/ pi@$(PI_HOST):/home/pi/growzones/
	@echo "Running install.sh on $(PI_HOST) ..."
	ssh pi@$(PI_HOST) 'cd /home/pi/growzones && ./install.sh'

clean:
	rm -rf $(MAC_VENV)
	find mac -name __pycache__ -type d -exec rm -rf {} +
	find mac -name "*.pyc" -delete
	rm -rf mac/.pytest_cache mac/*.egg-info mac/growzones/*.egg-info

# Guard target so app gives a clear error if the venv doesn't exist yet.
$(MAC_VENV)/bin/python:
	@echo "ERROR: mac venv missing. Run \`make install-mac\` first."
	@exit 1
