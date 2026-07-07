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

# Verify the binary exists.
BINARY="dist/context-recall-daemon/context-recall-daemon"
if [ ! -f "$BINARY" ]; then
    echo "ERROR: Build failed - binary not found at $BINARY"
    exit 1
fi

# Fix MLX metallib location: PyInstaller puts libmlx.dylib in _internal/
# but mlx.metallib in _internal/mlx/lib/. MLX resolves the metallib
# relative to the dylib, so copy it next to libmlx.dylib.
METALLIB="dist/context-recall-daemon/_internal/mlx/lib/mlx.metallib"
if [ -f "$METALLIB" ]; then
    cp "$METALLIB" "dist/context-recall-daemon/_internal/mlx.metallib"
    echo "==> Copied mlx.metallib next to libmlx.dylib"
fi

# Sign the main binary with a stable identity when one is available.
# macOS TCC stores a code-signing requirement with every permission
# grant: an ad-hoc signature changes its cdhash on every rebuild, so the
# user would be re-prompted for microphone access after each deploy —
# and the 2026-07 rename already cost the daemon its grant once. A real
# certificate plus a fixed identifier keeps grants valid across builds.
SIGN_IDENTITY="${CONTEXT_RECALL_SIGN_IDENTITY:-Apple Development: jamiecs@live.co.uk (34FA3W7TK5)}"
SIGN_IDENTIFIER="dev.jamiewhite.contextrecall.daemon"
if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_IDENTITY"; then
    echo "==> Codesigning daemon binary with '$SIGN_IDENTITY'"
    codesign --force --sign "$SIGN_IDENTITY" --identifier "$SIGN_IDENTIFIER" --timestamp=none "$BINARY"
    codesign --verify --verbose=1 "$BINARY"
else
    echo "==> WARNING: signing identity not found; leaving ad-hoc signature"
    echo "    (microphone permission will be re-requested after every rebuild)"
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
