#!/bin/bash
# Unit tests for scripts/signing_lib.sh's identity resolver. No real certs
# needed: the two detection helpers are overridden per scenario.
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$DIR/scripts/signing_lib.sh"

fail=0
check() { # desc expected_identity expected_hardened expected_tier
  local desc="$1" ei="$2" eh="$3" et="$4"
  cr_resolve_signing
  if [ "$CR_SIGN_IDENTITY" = "$ei" ] && [ "$CR_HARDENED" = "$eh" ] && [ "$CR_SIGN_TIER" = "$et" ]; then
    echo "ok - $desc"
  else
    echo "FAIL - $desc: got identity='$CR_SIGN_IDENTITY' hardened='$CR_HARDENED' tier='$CR_SIGN_TIER'"
    fail=1
  fi
}

# Tier 4: nothing present -> ad-hoc
cr_developer_id_identity() { :; }
cr_self_signed_present() { return 1; }
unset CONTEXT_RECALL_SIGN_IDENTITY
check "adhoc when no cert" "-" "0" "adhoc"

# Tier 3: self-signed present -> self-signed, not hardened
cr_self_signed_present() { return 0; }
check "self-signed when only self-signed cert" "Context Recall Self-Signed" "0" "self-signed"

# Tier 2: Developer ID present (wins over self-signed) -> hardened
cr_developer_id_identity() { echo "Developer ID Application: Jamie White (34FA3W7TK5)"; }
check "developer-id wins and is hardened" "Developer ID Application: Jamie White (34FA3W7TK5)" "1" "developer-id"

# Tier 1: explicit env override (Developer ID string -> hardened)
export CONTEXT_RECALL_SIGN_IDENTITY="Developer ID Application: Someone Else (XXXX)"
check "env override Developer ID is hardened" "Developer ID Application: Someone Else (XXXX)" "1" "env"

# Tier 1: explicit env override that is NOT a Developer ID -> not hardened
export CONTEXT_RECALL_SIGN_IDENTITY="Context Recall Self-Signed"
check "env override self-signed is not hardened" "Context Recall Self-Signed" "0" "env"

exit $fail
