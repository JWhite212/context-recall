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

# Choose the signing identity (self-detecting, ad-hoc fallback). Resolution:
#   1) explicit CONTEXT_RECALL_SIGN_IDENTITY=<name>  (Developer ID, etc.)
#   2) auto-detected per-machine self-signed cert     (setup_signing_cert.sh)
#   3) ad-hoc "-"                                      (CI, fresh clones, others)
#
# WHY cert over ad-hoc: an ad-hoc cdhash Designated Requirement changes every
# rebuild, so the macOS microphone (TCC) grant dies on each deploy — recording
# then silently captures zeros (RMS -100 dBFS on BOTH mic and BlackHole, since
# a denied mic makes CoreAudio zero all input streams). A self-signed cert
# yields a STABLE cert-leaf DR, so the grant survives. Run
# scripts/setup_signing_cert.sh ONCE per machine to create it. A self-signed
# cert is untrusted, so it is INVISIBLE to `security find-identity -p
# codesigning` — probe with find-certificate, never find-identity.
#
# Do NOT use an "Apple Development" identity without an embedded provisioning
# profile: tccd then rejects the bundle, zeroes the inputs, and KILLS the
# daemon on an explicit permission request (OS_REASON_TCC, observed
# 2026-07-07). Only ad-hoc / self-signed work for TCC here.
SELF_SIGNED_CN="Context Recall Self-Signed"
SIGN_IDENTIFIER="dev.jamiewhite.contextrecall.daemon"
SIGN_IDENTITY="${CONTEXT_RECALL_SIGN_IDENTITY:-}"

if [ -z "$SIGN_IDENTITY" ]; then
    if security find-certificate -c "$SELF_SIGNED_CN" >/dev/null 2>&1; then
        SIGN_IDENTITY="$SELF_SIGNED_CN"
        echo "==> Detected stable self-signed identity '$SELF_SIGNED_CN'"
    else
        SIGN_IDENTITY="-"
    fi
fi

sign_adhoc() {
    echo "==> Ad-hoc signing daemon app bundle (identifier $SIGN_IDENTIFIER)"
    echo "    NOTE: ad-hoc cdhash changes per rebuild -> macOS re-prompts for the"
    echo "    microphone after each deploy. Run scripts/setup_signing_cert.sh once"
    echo "    to make the grant survive rebuilds."
    codesign --force --sign - --identifier "$SIGN_IDENTIFIER" "$APP_DIR"
}

# --- Compile + inject the ScreenCaptureKit system-audio helper -------------
# The daemon spawns this signed Swift binary to capture system audio via the
# Screen Recording TCC service (works on macOS betas where the Microphone
# service — and thus the BlackHole input — is broken). Sign it FIRST so the
# outer-app seal below covers an already-signed nested binary (inside-out).
HELPER_SRC="macos/sck-audio-capture/main.swift"
HELPER_DEST="$APP_DIR/Contents/Resources/sck-audio-capture"
HELPER_IDENTIFIER="dev.jamiewhite.contextrecall.sck"
if command -v swiftc >/dev/null 2>&1 && [ -f "$HELPER_SRC" ]; then
    echo "==> Compiling SCK audio helper"
    # A present-but-broken main.swift must NOT abort the whole daemon build
    # under `set -euo pipefail`: degrade to a BlackHole-only daemon instead.
    if swiftc -O "$HELPER_SRC" -o "$HELPER_DEST"; then
        if [ "$SIGN_IDENTITY" = "-" ]; then
            codesign --force --sign - --identifier "$HELPER_IDENTIFIER" "$HELPER_DEST"
        else
            codesign --force --sign "$SIGN_IDENTITY" --identifier "$HELPER_IDENTIFIER" \
                --timestamp=none "$HELPER_DEST" 2>/dev/null || \
                codesign --force --sign - --identifier "$HELPER_IDENTIFIER" "$HELPER_DEST"
        fi
        echo "==> SCK helper signed and placed at Contents/Resources/sck-audio-capture"
    else
        echo "==> WARNING: SCK helper failed to compile — daemon will degrade to BlackHole (no SCK)"
        rm -f "$HELPER_DEST"
    fi
else
    echo "==> WARNING: swiftc or $HELPER_SRC missing — daemon degrades to BlackHole (no SCK)"
fi

if [ "$SIGN_IDENTITY" = "-" ]; then
    sign_adhoc
else
    echo "==> Codesigning daemon app bundle with '$SIGN_IDENTITY' (stable DR)"
    # Attempt-and-fallback: a missing/renamed/locked identity degrades to ad-hoc
    # instead of aborting the build under `set -euo pipefail`. Load-bearing for
    # CI and fresh clones, which have no cert.
    if ! codesign --force --sign "$SIGN_IDENTITY" \
            --identifier "$SIGN_IDENTIFIER" --timestamp=none "$APP_DIR" 2>/dev/null; then
        echo "    WARNING: signing with '$SIGN_IDENTITY' failed; falling back to ad-hoc."
        sign_adhoc
    fi
fi
codesign --verify --verbose=1 "$APP_DIR"

# Echo the resulting DR so deploy logs confirm it is cert-leaf (stable) not
# cdhash (volatile).
echo "==> Daemon Designated Requirement:"
codesign -d -r- "$APP_DIR" 2>&1 | sed 's/^/    /' || true

# Report size.
SIZE=$(du -sh "$BINARY" | cut -f1)
TOTAL_SIZE=$(du -sh "dist/context-recall-daemon/" | cut -f1)
echo ""
echo "==> Build complete"
echo "    Binary:     $BINARY ($SIZE)"
echo "    Bundle dir: dist/context-recall-daemon/ ($TOTAL_SIZE)"
echo ""
echo "Test with: $BINARY --help"
