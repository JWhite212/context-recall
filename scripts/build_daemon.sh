#!/bin/bash
# Build the MeetingMind daemon as a standalone binary via PyInstaller.
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

echo "==> Building meetingmind-daemon"
python3 -m PyInstaller meetingmind.spec --noconfirm

# Verify the binary exists.
BINARY="dist/meetingmind-daemon/meetingmind-daemon"
if [ ! -f "$BINARY" ]; then
    echo "ERROR: Build failed — binary not found at $BINARY"
    exit 1
fi

# Fix MLX metallib location: PyInstaller puts libmlx.dylib in _internal/
# but mlx.metallib in _internal/mlx/lib/. MLX resolves the metallib
# relative to the dylib, so copy it next to libmlx.dylib.
METALLIB="dist/meetingmind-daemon/_internal/mlx/lib/mlx.metallib"
if [ -f "$METALLIB" ]; then
    cp "$METALLIB" "dist/meetingmind-daemon/_internal/mlx.metallib"
    echo "==> Copied mlx.metallib next to libmlx.dylib"
fi

# Report size.
SIZE=$(du -sh "$BINARY" | cut -f1)
TOTAL_SIZE=$(du -sh "dist/meetingmind-daemon/" | cut -f1)
echo ""
echo "==> Build complete"
echo "    Binary:     $BINARY ($SIZE)"
echo "    Bundle dir: dist/meetingmind-daemon/ ($TOTAL_SIZE)"
echo ""
echo "Test with: $BINARY --help"
