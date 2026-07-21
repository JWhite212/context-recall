#!/bin/bash
# One-time, per-machine setup of notarization credentials for local deploys.
# Idempotent: re-running is safe. Mirrors setup_signing_cert.sh ergonomics.
#
# Prerequisites you must create in your Apple Developer account first:
#   1) A "Developer ID Application" certificate (Xcode > Settings > Accounts >
#      Manage Certificates > + > Developer ID Application), imported into your
#      login keychain.
#   2) An App Store Connect API key with the "Developer" role
#      (appstoreconnect.apple.com > Users and Access > Keys): download the
#      AuthKey_XXXX.p8 ONCE and note its Key ID + the team Issuer ID.
#
# Usage:
#   ./scripts/setup_notary_profile.sh /path/to/AuthKey_XXXX.p8 <KEY_ID> <ISSUER_ID>
set -euo pipefail

PROFILE="context-recall-notary"

# --- Prereq 1: Developer ID Application cert ---------------------------------
if ! security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
    cat >&2 <<'EOF'
ERROR: No "Developer ID Application" certificate found in the keychain.

Create one (you are already enrolled in the Apple Developer Program):
  Xcode > Settings > Accounts > (your Apple ID) > Manage Certificates >
    + button > "Developer ID Application"
Then re-run this script.
EOF
    exit 1
fi
echo "==> Developer ID Application certificate present."

# --- Prereq 2: notary keychain profile --------------------------------------
if xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
    echo "==> Notary profile '$PROFILE' already configured — nothing to do (idempotent)."
    exit 0
fi

P8="${1:-}"; KEY_ID="${2:-}"; ISSUER_ID="${3:-}"
if [ -z "$P8" ] || [ -z "$KEY_ID" ] || [ -z "$ISSUER_ID" ]; then
    cat >&2 <<EOF
Notary profile '$PROFILE' is not configured yet. Re-run with your API key:

  ./scripts/setup_notary_profile.sh /path/to/AuthKey_XXXX.p8 <KEY_ID> <ISSUER_ID>

Get these from appstoreconnect.apple.com > Users and Access > Keys.
EOF
    exit 1
fi
if [ ! -f "$P8" ]; then
    echo "ERROR: .p8 key file not found: $P8" >&2
    exit 1
fi

echo "==> Storing notary credentials in keychain profile '$PROFILE'"
xcrun notarytool store-credentials "$PROFILE" \
    --key "$P8" --key-id "$KEY_ID" --issuer "$ISSUER_ID"
echo "==> Done. scripts/notarize_and_staple.sh will now use '$PROFILE'."
