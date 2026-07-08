#!/bin/bash
# Inject the freshly built daemon bundle into a Tauri-built app.
#
# Usage: ./scripts/inject_daemon.sh "/path/to/Context Recall.app"
#
# Why this exists: the Tauri resource copier destroys the symlink farm
# inside the PyInstaller-built "Context Recall Daemon.app" (Frameworks/
# Resources are cross-linked with relative symlinks; the copier
# dereferences or drops them — observed as the deployed daemon dying at
# bootstrap with "ModuleNotFoundError: No module named '_struct'").
# So the daemon is copied in AFTER bundling with a symlink-preserving
# cp, and the outer app is re-sealed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP="${1:?usage: inject_daemon.sh /path/to/Context Recall.app}"
DAEMON_SRC="$PROJECT_ROOT/dist/context-recall-daemon/Context Recall Daemon.app"
DEST_DIR="$APP/Contents/Resources/resources/context-recall-daemon"

if [ ! -d "$DAEMON_SRC" ]; then
    echo "ERROR: daemon bundle not found at $DAEMON_SRC (run build_daemon.sh first)"
    exit 1
fi
if [ ! -d "$APP/Contents" ]; then
    echo "ERROR: $APP does not look like an app bundle"
    exit 1
fi

echo "==> Injecting daemon bundle into $APP"
rm -rf "$DEST_DIR"
mkdir -p "$DEST_DIR"
cp -R "$DAEMON_SRC" "$DEST_DIR/"

LINKS=$(find "$DEST_DIR" -type l | wc -l | tr -d ' ')
echo "==> Daemon injected ($LINKS symlinks preserved)"
if [ "$LINKS" -eq 0 ]; then
    echo "ERROR: injection lost the symlinks — daemon would not boot"
    exit 1
fi

# Warn (do not fail) if the injected daemon lost its stable cert-leaf DR — a
# regression tripwire in case a future change re-signs the daemon ad-hoc and
# silently resets the microphone grant. Ad-hoc builds (no cert) legitimately
# have a cdhash DR, so this is informational only.
if ! codesign -d -r- "$DEST_DIR/Context Recall Daemon.app" 2>&1 | grep -q 'certificate leaf'; then
    echo "==> NOTE: injected daemon DR is cdhash-based (ad-hoc). The mic grant will"
    echo "    reset on each rebuild. Run scripts/setup_signing_cert.sh for a stable grant."
fi

# Replacing resources invalidates the outer app's seal; re-sign ad-hoc
# (the nested daemon bundle keeps its own stable-identity signature).
echo "==> Re-sealing outer app"
codesign --force --sign - "$APP"

# The daemon must be able to bootstrap Python from the injected copy.
"$DEST_DIR/Context Recall Daemon.app/Contents/MacOS/context-recall-daemon" --help >/dev/null 2>&1 \
    && echo "==> Injected daemon smoke test passed" \
    || { echo "ERROR: injected daemon failed to run"; exit 1; }
