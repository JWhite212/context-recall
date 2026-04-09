#!/bin/bash
# Create a styled .dmg installer for MeetingMind.
#
# Usage: ./scripts/create_dmg.sh [path/to/MeetingMind.app]
#
# Requires: create-dmg (brew install create-dmg)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_PATH="${1:-$PROJECT_ROOT/ui/src-tauri/target/release/bundle/macos/MeetingMind.app}"
DMG_DIR="$PROJECT_ROOT/dist"
DMG_NAME="MeetingMind.dmg"
BACKGROUND="$SCRIPT_DIR/dmg/background.png"

if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: App bundle not found at $APP_PATH"
    echo "Build the Tauri app first: cd ui && npm run tauri build"
    exit 1
fi

if ! command -v create-dmg &>/dev/null; then
    echo "ERROR: create-dmg not found. Install with: brew install create-dmg"
    exit 1
fi

# Generate background image if it doesn't exist.
if [ ! -f "$BACKGROUND" ]; then
    echo "==> Generating DMG background image"
    python3 "$SCRIPT_DIR/dmg/create_background.py"
fi

mkdir -p "$DMG_DIR"

# Remove existing DMG if present.
rm -f "$DMG_DIR/$DMG_NAME"

echo "==> Creating DMG"
create-dmg \
    --volname "MeetingMind" \
    --volicon "$PROJECT_ROOT/ui/src-tauri/icons/icon.icns" \
    --background "$BACKGROUND" \
    --window-pos 200 120 \
    --window-size 660 400 \
    --icon-size 80 \
    --icon "MeetingMind.app" 190 190 \
    --app-drop-link 470 190 \
    --hide-extension "MeetingMind.app" \
    --no-internet-enable \
    "$DMG_DIR/$DMG_NAME" \
    "$APP_PATH"

SIZE=$(du -sh "$DMG_DIR/$DMG_NAME" | cut -f1)
echo ""
echo "==> DMG created: $DMG_DIR/$DMG_NAME ($SIZE)"
