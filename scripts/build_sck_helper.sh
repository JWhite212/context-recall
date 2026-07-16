#!/bin/bash
# Compile the ScreenCaptureKit system-audio helper for local (non-frozen) runs.
# The daemon resolves it at macos/sck-audio-capture/.build/sck-audio-capture.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$ROOT/macos/sck-audio-capture/main.swift"
OUT_DIR="$ROOT/macos/sck-audio-capture/.build"
OUT="$OUT_DIR/sck-audio-capture"

if ! command -v swiftc >/dev/null 2>&1; then
    echo "ERROR: swiftc not found (install Xcode command-line tools)" >&2
    exit 1
fi
mkdir -p "$OUT_DIR"
echo "==> Compiling $SRC"
swiftc -O "$SRC" -o "$OUT"
echo "==> Built $OUT"
