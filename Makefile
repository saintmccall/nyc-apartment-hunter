SHELL   := /bin/bash
PROJECT := $(shell pwd)
VENV    := $(PROJECT)/.venv
PYTHON  := $(VENV)/bin/python
PLIST   := $(HOME)/Library/LaunchAgents/com.nyc-apartment-hunter.plist
SERVICE := nyc-apartment-hunter
LOG     := $(PROJECT)/data/hunter.log
ENV     := $(PROJECT)/.env
OS      := $(shell uname)

.PHONY: setup deploy run test scrape-test logs stop start status clean

## setup  — macOS: create venv, install deps, configure credentials, install launchd
setup:
	@bash setup.sh

## deploy  — Linux/DigitalOcean: install deps, configure credentials, install systemd service
deploy:
	@bash deploy-linux.sh

## browser  — install Playwright + Chromium (needed for JS-rendered scrapers)
browser:
	@$(VENV)/bin/pip install --quiet -e "$(PROJECT)[browser]" && \
	 $(VENV)/bin/playwright install chromium

## run  — run the full pipeline in the foreground (ctrl-C to stop)
run:
	@set -a && source $(ENV) && set +a && $(PYTHON) main.py

## test  — install dev deps and run the pytest suite
test:
	@$(VENV)/bin/pip install --quiet --upgrade pip && \
	 $(VENV)/bin/pip install --quiet -e "$(PROJECT)[dev]" && \
	 $(VENV)/bin/pytest -v

## scrape-test  — scrape only, no scoring or email, print first 5 results
scrape-test:
	@set -a && source $(ENV) && set +a && $(PYTHON) scripts/scrape_test.py

## logs  — tail the rotating log file (live)
logs:
	@mkdir -p data && tail -f $(LOG)

## stop  — stop the background service (launchd on macOS, systemd on Linux)
stop:
ifeq ($(OS),Darwin)
	@launchctl list | grep -q "com.nyc-apartment-hunter" \
	  && launchctl unload $(PLIST) && echo "Agent stopped." \
	  || echo "Agent is not running."
else
	@systemctl stop $(SERVICE) && echo "Service stopped." || echo "Service is not running."
endif

## start  — start the background service
start:
ifeq ($(OS),Darwin)
	@launchctl load $(PLIST) && echo "Agent started."
else
	@systemctl start $(SERVICE) && echo "Service started."
endif

## status  — check whether the service is running
status:
ifeq ($(OS),Darwin)
	@launchctl list | grep -q "com.nyc-apartment-hunter" \
	  && echo "✓ Agent is running" \
	  || echo "✗ Agent is not running (run: make start)"
else
	@systemctl is-active --quiet $(SERVICE) \
	  && echo "✓ Service is running" \
	  || echo "✗ Service is not running (run: make start)"
endif

## clean  — remove venv and caches (keeps .env and data/)
clean:
	@rm -rf $(VENV) __pycache__ src/__pycache__ tests/__pycache__ .pytest_cache
	@echo "Cleaned. Run 'make setup' to rebuild."
