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

# Choose the signing identity via the shared resolver in signing_lib.sh.
# Tiers (see that file's header for the authoritative list):
#   1) explicit CONTEXT_RECALL_SIGN_IDENTITY=<name>  (env override)
#   2) auto-detected "Developer ID Application" cert  (→ Hardened Runtime +
#      timestamp + entitlements; notarizable — the durable OS-update-safe tier)
#   3) auto-detected per-machine self-signed cert     (setup_signing_cert.sh)
#   4) ad-hoc "-"                                      (CI, fresh clones, others)
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
SIGN_IDENTIFIER="dev.jamiewhite.contextrecall.daemon"
ENTITLEMENTS="$PROJECT_ROOT/scripts/daemon.entitlements"

# Resolve the signing identity (tier: env > Developer ID > self-signed > ad-hoc).
source "$SCRIPT_DIR/signing_lib.sh"
cr_resolve_signing
echo "==> Signing tier: $CR_SIGN_TIER (identity: $CR_SIGN_IDENTITY, hardened: $CR_HARDENED)"

# Sign a bundle/binary per the resolved tier. Developer ID -> Hardened Runtime
# + secure timestamp + entitlements (notarizable). Self-signed -> stable DR,
# no timestamp. Ad-hoc -> cdhash DR. On a Developer ID/self-signed failure,
# degrade to ad-hoc rather than aborting the build (load-bearing for CI).
cr_sign() { # <path> <identifier>
    local path="$1" ident="$2"
    if [ "$CR_SIGN_IDENTITY" = "-" ]; then
        codesign --force --sign - --identifier "$ident" "$path"
        return
    fi
    if [ "$CR_HARDENED" = "1" ]; then
        # --deep: sign every nested Mach-O inside-out (a harmless no-op on a
        # lone file). Required for notarization — PyInstaller leaves the
        # collected native libs (torch/numpy/mlx/portaudio/libsndfile …)
        # ad-hoc-signed, and notarytool rejects any nested Mach-O that is not
        # Developer ID + Hardened Runtime + secure-timestamped. --deep
        # propagates these options to them. Only on this tier: self-signed/
        # ad-hoc keep the shallow sign (a non-notarized local grant binds to
        # the main-executable DR, so nested ad-hoc libs are fine there).
        codesign --force --sign "$CR_SIGN_IDENTITY" --identifier "$ident" --deep \
            --options runtime --timestamp --entitlements "$ENTITLEMENTS" "$path" && return
    else
        codesign --force --sign "$CR_SIGN_IDENTITY" --identifier "$ident" \
            --timestamp=none "$path" && return
    fi
    echo "    WARNING: signing '$path' with '$CR_SIGN_IDENTITY' failed; falling back to ad-hoc."
    codesign --force --sign - --identifier "$ident" "$path"
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
        cr_sign "$HELPER_DEST" "$HELPER_IDENTIFIER"
        echo "==> SCK helper signed and placed at Contents/Resources/sck-audio-capture"
    else
        echo "==> WARNING: SCK helper failed to compile — daemon will degrade to BlackHole (no SCK)"
        rm -f "$HELPER_DEST"
    fi
else
    echo "==> WARNING: swiftc or $HELPER_SRC missing — daemon degrades to BlackHole (no SCK)"
fi

echo "==> Codesigning daemon app bundle (tier: $CR_SIGN_TIER)"
cr_sign "$APP_DIR" "$SIGN_IDENTIFIER"
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
