#!/bin/bash
# Notarize a signed .dmg (or .app) and staple the ticket. Run ONCE, after all
# bundle content is final and signed — re-signing after this invalidates the
# ticket.
#
# Credentials, in priority order:
#   1) env: APPLE_API_KEY_P8_PATH + APPLE_API_KEY_ID + APPLE_API_KEY_ISSUER_ID  (CI)
#   2) a stored keychain profile named "context-recall-notary" (local;
#      created by scripts/setup_notary_profile.sh)
#
# Usage: ./scripts/notarize_and_staple.sh "/path/to/Context Recall_injected_aarch64.dmg"
set -euo pipefail

TARGET="${1:?usage: notarize_and_staple.sh <path-to-.dmg-or-.app>}"
PROFILE="context-recall-notary"

if [ ! -e "$TARGET" ]; then
    echo "ERROR: target not found: $TARGET" >&2
    exit 1
fi

notarize() {
    if [ -n "${APPLE_API_KEY_P8_PATH:-}" ] && [ -n "${APPLE_API_KEY_ID:-}" ] \
       && [ -n "${APPLE_API_KEY_ISSUER_ID:-}" ]; then
        echo "==> Notarizing with App Store Connect API key (env)" >&2
        xcrun notarytool submit "$TARGET" \
            --key "$APPLE_API_KEY_P8_PATH" \
            --key-id "$APPLE_API_KEY_ID" \
            --issuer "$APPLE_API_KEY_ISSUER_ID" \
            --wait --output-format plist
    elif xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
        echo "==> Notarizing with stored keychain profile '$PROFILE'" >&2
        xcrun notarytool submit "$TARGET" \
            --keychain-profile "$PROFILE" \
            --wait --output-format plist
    else
        echo "ERROR: no notarization credentials." >&2
        echo "  Local: run scripts/setup_notary_profile.sh to create the '$PROFILE' profile." >&2
        echo "  CI:    set APPLE_API_KEY_P8_PATH / APPLE_API_KEY_ID / APPLE_API_KEY_ISSUER_ID." >&2
        exit 2
    fi
}

# notarytool submit --wait exits non-zero if the status is not "Accepted".
# Capture the submission id so we can print the log on any non-accepted result.
SUBMIT_OUT="$(notarize)" || {
    RC=$?
    # notarize() already printed the "no credentials" guidance to stderr;
    # there is no submission id to log, so propagate its exit code as-is.
    if [ "$RC" -eq 2 ]; then
        exit 2
    fi
    echo "==> Notarization did not succeed; fetching log:" >&2
    SID="$(printf '%s' "$SUBMIT_OUT" | /usr/libexec/PlistBuddy -c 'Print :id' /dev/stdin 2>/dev/null || true)"
    if [ -n "$SID" ]; then
        if [ -n "${APPLE_API_KEY_P8_PATH:-}" ]; then
            xcrun notarytool log "$SID" --key "$APPLE_API_KEY_P8_PATH" \
                --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_KEY_ISSUER_ID" >&2 || true
        else
            xcrun notarytool log "$SID" --keychain-profile "$PROFILE" >&2 || true
        fi
    fi
    exit 1
}
printf '%s\n' "$SUBMIT_OUT"

echo "==> Stapling ticket to $TARGET"
xcrun stapler staple "$TARGET"
xcrun stapler validate "$TARGET"
echo "==> Notarized + stapled: $TARGET"
