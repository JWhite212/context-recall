#!/bin/bash
# Report the signing + notarization status of a built app (or daemon) bundle.
# Exit 0 iff `codesign --verify` passes; the Gatekeeper/staple lines are
# informational (they only pass for a notarized+stapled build).
#
# Usage: ./scripts/verify_signing.sh "/path/to/Context Recall.app"
set -uo pipefail

APP="${1:?usage: verify_signing.sh <path-to-.app>}"
[ -e "$APP" ] || { echo "ERROR: not found: $APP" >&2; exit 1; }

echo "== codesign --verify --deep --strict =="
if codesign --verify --deep --strict --verbose=2 "$APP" 2>&1; then
    echo "  -> PASS"
    VERIFY_OK=0
else
    echo "  -> FAIL"
    VERIFY_OK=1
fi

echo "== Designated Requirement =="
codesign -d -r- "$APP" 2>&1 | sed 's/^/  /'

echo "== Hardened Runtime flag (expect 'runtime' for Developer ID builds) =="
codesign -d --verbose=4 "$APP" 2>&1 | grep -i "flags=" | sed 's/^/  /' || echo "  (no flags line)"

echo "== Entitlements =="
codesign -d --entitlements - --xml "$APP" 2>/dev/null | plutil -p - 2>/dev/null | sed 's/^/  /' || echo "  (none)"

echo "== Gatekeeper assessment (expect 'Notarized Developer ID' once notarized) =="
spctl -a -vvv --type execute "$APP" 2>&1 | sed 's/^/  /' || true

echo "== Staple validation =="
xcrun stapler validate "$APP" 2>&1 | sed 's/^/  /' || echo "  (not stapled)"

exit "$VERIFY_OK"
