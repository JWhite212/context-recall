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

# Sign the daemon bundle AD-HOC by default. Counter-intuitive but
# evidence-forced (2026-07-07): signing with an Apple Development
# certificate WITHOUT an embedded provisioning profile makes tccd
# reject the whole bundle — it cannot read the sealed
# NSMicrophoneUsageDescription, silently zeroes every input stream, and
# KILLS the process on an explicit permission request (OS_REASON_TCC).
# The identical bundle signed ad-hoc prompts and records normally.
# The cost: an ad-hoc cdhash changes per rebuild, so macOS re-prompts
# for the microphone once after each deploy. Set
# CONTEXT_RECALL_SIGN_IDENTITY to a PROPERLY PROVISIONED identity
# (Developer ID, or Apple Development plus embedded.provisionprofile)
# to make grants survive rebuilds.
SIGN_IDENTITY="${CONTEXT_RECALL_SIGN_IDENTITY:--}"
SIGN_IDENTIFIER="dev.jamiewhite.contextrecall.daemon"
if [ "$SIGN_IDENTITY" = "-" ]; then
    echo "==> Ad-hoc signing daemon app bundle (identifier $SIGN_IDENTIFIER)"
    codesign --force --sign - --identifier "$SIGN_IDENTIFIER" "$APP_DIR"
else
    echo "==> Codesigning daemon app bundle with '$SIGN_IDENTITY'"
    codesign --force --sign "$SIGN_IDENTITY" --identifier "$SIGN_IDENTIFIER" --timestamp=none "$APP_DIR"
fi
codesign --verify --verbose=1 "$APP_DIR"

# Report size.
SIZE=$(du -sh "$BINARY" | cut -f1)
TOTAL_SIZE=$(du -sh "dist/context-recall-daemon/" | cut -f1)
echo ""
echo "==> Build complete"
echo "    Binary:     $BINARY ($SIZE)"
echo "    Bundle dir: dist/context-recall-daemon/ ($TOTAL_SIZE)"
echo ""
echo "Test with: $BINARY --help"
