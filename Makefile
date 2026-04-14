# MeetingMind — Build & Install Automation
#
# Targets:
#   make setup       — Create venv, install Python and UI dependencies
#   make build       — Build daemon binary, copy resources, build Tauri app
#   make install     — Build everything, then install the launch agent
#   make dev         — Start the Tauri dev server with hot-reload
#   make test        — Run the Python test suite
#   make lint        — Run ruff linter on src/ and tests/
#   make clean       — Remove all build artefacts

.PHONY: setup build-daemon copy-daemon build-app build install dev test lint clean

setup:
	@echo "==> Creating virtual environment (if missing)"
	test -d .venv || python3 -m venv .venv
	@echo "==> Installing Python dependencies"
	.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
	@echo "==> Installing UI dependencies"
	cd ui && npm install

build-daemon:
	./scripts/build_daemon.sh

copy-daemon:
	@echo "==> Copying daemon to Tauri resources"
	mkdir -p ui/src-tauri/resources/meetingmind-daemon
	cp -R dist/meetingmind-daemon/ ui/src-tauri/resources/meetingmind-daemon/

build-app:
	cd ui && npm run tauri build

build: build-daemon copy-daemon build-app

install: build
	./scripts/install.sh

dev:
	cd ui && npm run tauri dev

test:
	source .venv/bin/activate && python3 -m pytest tests/ -v

lint:
	source .venv/bin/activate && ruff check src/ tests/

clean:
	@echo "==> Cleaning build artefacts"
	rm -rf dist/
	rm -rf ui/dist/
	rm -rf ui/src-tauri/target/release/bundle/
