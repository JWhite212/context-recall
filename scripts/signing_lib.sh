#!/bin/bash
# Shared code-signing identity resolution for the Context Recall build
# pipeline. Sourced by build_daemon.sh (daemon + SCK helper) and
# inject_daemon.sh (outer app) so both agree on the identity.
#
# Resolution tiers (highest priority first):
#   1) $CONTEXT_RECALL_SIGN_IDENTITY   explicit override (any identity)
#   2) "Developer ID Application" cert notarizable, Hardened Runtime   [tier 2]
#   3) "Context Recall Self-Signed"    stable cert-leaf DR fallback
#   4) "-"                             ad-hoc (CI / fresh clones)
#
# cr_resolve_signing sets:
#   CR_SIGN_IDENTITY  the codesign --sign argument
#   CR_HARDENED       1 if Hardened Runtime + secure timestamp apply, else 0
#                     (only ever 1 for a "Developer ID" identity)
#   CR_SIGN_TIER      env | developer-id | self-signed | adhoc  (for logs)

CR_SELF_SIGNED_CN="Context Recall Self-Signed"

# Full name of a "Developer ID Application" identity in the keychain, or empty.
# A self-signed/untrusted cert never appears here; a Developer ID does.
cr_developer_id_identity() {
    security find-identity -v -p codesigning 2>/dev/null \
        | grep "Developer ID Application" \
        | head -1 \
        | sed -E 's/^[[:space:]]*[0-9]+\)[[:space:]]+[0-9A-Fa-f]+[[:space:]]+"(.*)"[[:space:]]*$/\1/'
}

# Exit 0 if the self-signed cert is present (find-certificate, since an
# untrusted cert is invisible to find-identity).
cr_self_signed_present() {
    security find-certificate -c "$CR_SELF_SIGNED_CN" >/dev/null 2>&1
}

cr_resolve_signing() {
    if [ -n "${CONTEXT_RECALL_SIGN_IDENTITY:-}" ]; then
        CR_SIGN_IDENTITY="$CONTEXT_RECALL_SIGN_IDENTITY"; CR_SIGN_TIER="env"
    elif [ -n "$(cr_developer_id_identity)" ]; then
        CR_SIGN_IDENTITY="$(cr_developer_id_identity)"; CR_SIGN_TIER="developer-id"
    elif cr_self_signed_present; then
        CR_SIGN_IDENTITY="$CR_SELF_SIGNED_CN"; CR_SIGN_TIER="self-signed"
    else
        CR_SIGN_IDENTITY="-"; CR_SIGN_TIER="adhoc"
    fi
    # Hardened Runtime + secure timestamp are valid ONLY for a Developer ID
    # identity (needed for notarization). Self-signed cannot get a timestamp;
    # ad-hoc has no runtime. Key off the identity string so an env override is
    # classified correctly.
    case "$CR_SIGN_IDENTITY" in
        *"Developer ID"*) CR_HARDENED=1 ;;
        *)                CR_HARDENED=0 ;;
    esac
}
