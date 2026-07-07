#!/bin/bash
# Build the Context Recall daemon as a standalone binary via PyInstaller.
#
# Usage: ./scripts/build_daemon.sh
#
# Requires: Python venv with all dependencies + pyinstaller installed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Activate venv if available.
if [ -f ".venv/bin/activate" ]; then
    echo "==> Activating virtual environment"
    source .venv/bin/activate
fi

# Ensure PyInstaller is installed.
if ! python3 -m PyInstaller --version &>/dev/null; then
    echo "==> Installing PyInstaller"
    pip3 install pyinstaller
fi

echo "==> Building context-recall-daemon"
python3 -m PyInstaller context-recall.spec --noconfirm

# The spec's BUNDLE step wraps the payload as "Context Recall Daemon.app"
# with a codesign-compliant layout and an Info.plist carrying
# NSMicrophoneUsageDescription — TCC KILLS a process that requests
# microphone access without one (observed 2026-07-07: launchd crash
# loop, last exit reason OS_REASON_TCC). Repackage the .app under
# dist/context-recall-daemon/ so the CI artifact path and the Tauri
# resource directory stay unchanged.
APP_SRC="dist/Context Recall Daemon.app"
if [ ! -d "$APP_SRC" ]; then
    echo "ERROR: Build failed - bundle not found at $APP_SRC"
    exit 1
fi
rm -rf dist/context-recall-daemon
mkdir -p dist/context-recall-daemon
mv "$APP_SRC" dist/context-recall-daemon/
APP_DIR="dist/context-recall-daemon/Context Recall Daemon.app"
BINARY="$APP_DIR/Contents/MacOS/context-recall-daemon"
if [ ! -f "$BINARY" ]; then
    echo "ERROR: Build failed - executable not found at $BINARY"
    exit 1
fi

# Fix MLX metallib location: MLX resolves the metallib relative to
# libmlx.dylib, but PyInstaller collects it under mlx/lib/. Find both
# inside the bundle (the .app layout differs from plain onedir).
LIBMLX=$(find "$APP_DIR" -name "libmlx.dylib" -print -quit)
METALLIB=$(find "$APP_DIR" -name "mlx.metallib" -print -quit)
if [ -n "$LIBMLX" ] && [ -n "$METALLIB" ]; then
    DEST="$(dirname "$LIBMLX")/mlx.metallib"
    if [ ! -f "$DEST" ]; then
        cp "$METALLIB" "$DEST"
    fi
    echo "==> Ensured mlx.metallib sits next to libmlx.dylib"
fi

# Sign the daemon bundle with a stable identity when one is available.
# macOS TCC stores a code-signing requirement with every permission
# grant: an ad-hoc signature changes its cdhash on every rebuild, so the
# user would be re-prompted for microphone access after each deploy —
# and the 2026-07 rename already cost the daemon its grant once. A real
# certificate plus a fixed identifier keeps grants valid across builds.
# SHA-1 fingerprint, not the name: the login keychain holds a revoked
# copy of "Apple Development: jamiecs@live.co.uk (34FA3W7TK5)" with the
# same name, which makes name-based selection ambiguous.
SIGN_IDENTITY="${CONTEXT_RECALL_SIGN_IDENTITY:-92B7AF44BFEBAEB58A7208FF503AFE84311F1CFB}"
SIGN_IDENTIFIER="dev.jamiewhite.contextrecall.daemon"
if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_IDENTITY"; then
    echo "==> Codesigning daemon app bundle with '$SIGN_IDENTITY'"
    codesign --force --sign "$SIGN_IDENTITY" --identifier "$SIGN_IDENTIFIER" --timestamp=none "$APP_DIR"
    codesign --verify --verbose=1 "$APP_DIR"
else
    echo "==> WARNING: signing identity not found; ad-hoc signing the bundle"
    echo "    (microphone permission will be re-requested after every rebuild)"
    codesign --force --sign - --identifier "$SIGN_IDENTIFIER" "$APP_DIR"
fi

# Report size.
SIZE=$(du -sh "$BINARY" | cut -f1)
TOTAL_SIZE=$(du -sh "dist/context-recall-daemon/" | cut -f1)
echo ""
echo "==> Build complete"
echo "    Binary:     $BINARY ($SIZE)"
echo "    Bundle dir: dist/context-recall-daemon/ ($TOTAL_SIZE)"
echo ""
echo "Test with: $BINARY --help"
