#!/bin/bash
# One-time, idempotent, per-machine setup of a self-signed code-signing
# identity so the daemon's Designated Requirement — and thus its macOS
# microphone (TCC) grant — survives rebuilds.
#
# WHY: the mic grant is pinned to the daemon's code DR. Ad-hoc signing
# yields a cdhash DR that changes every rebuild, so the grant dies on each
# deploy (silent zero-capture: RMS -100 dBFS on BOTH mic and BlackHole,
# because a denied mic makes CoreAudio zero all input streams). A self-signed
# cert yields a STABLE cert-leaf DR that survives rebuilds — as long as the
# SAME cert is reused.
#
# The private key lives ONLY in your login keychain; nothing secret is
# written to the repo. TCC is per-machine, so a per-machine cert is correct —
# no shared secret. Safe to re-run (reuses the existing cert). Pass --rotate
# to deliberately replace it (this LOSES the current mic grant — re-Allow once).
#
# Do NOT use an "Apple Development" identity: without an embedded provisioning
# profile it makes tccd reject the bundle and kill the daemon (OS_REASON_TCC).
# Only ad-hoc or self-signed work for TCC here.

set -euo pipefail
umask 077

CERT_CN="Context Recall Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
ROTATE=0
[ "${1:-}" = "--rotate" ] && ROTATE=1

# --- Idempotency gate --------------------------------------------------
# find-certificate (NOT find-identity): a self-signed, untrusted cert never
# appears in `find-identity -p codesigning`, but codesign can still sign with
# it and the signature satisfies its own DR.
if security find-certificate -c "$CERT_CN" "$KEYCHAIN" >/dev/null 2>&1; then
    if [ "$ROTATE" -eq 0 ]; then
        LEAF=$(security find-certificate -c "$CERT_CN" -Z "$KEYCHAIN" 2>/dev/null \
                 | awk '/SHA-1 hash:/ {print $NF}')
        echo "==> '$CERT_CN' already present — reusing (idempotent, grant preserved)."
        echo "    Leaf SHA-1: ${LEAF:-<unavailable>}"
        echo "    build_daemon.sh auto-detects it. Nothing to do."
        exit 0
    fi
    echo "==> --rotate: deleting existing '$CERT_CN' (this WILL cost the current mic grant)."
    security delete-certificate -c "$CERT_CN" "$KEYCHAIN" || true
fi

echo "==> Creating self-signed code-signing identity '$CERT_CN' (one-time)."

# Resolve an OpenSSL 3 binary. macOS's system /usr/bin/openssl is LibreSSL,
# which lacks the `-legacy` flag we need to emit a macOS-importable PKCS12
# (OpenSSL 3 defaults to a SHA-256 MAC that `security import` rejects;
# `-legacy` + SHA1/3DES is what macOS accepts). Prefer an OpenSSL-3 on PATH,
# else the common Homebrew locations; fail loudly (never mid-script) if only
# LibreSSL is available.
resolve_openssl3() {
    local c
    for c in openssl \
             /opt/homebrew/opt/openssl@3/bin/openssl \
             /usr/local/opt/openssl@3/bin/openssl \
             "$( { brew --prefix openssl@3 2>/dev/null; } )/bin/openssl"; do
        c="$(command -v "$c" 2>/dev/null || echo "$c")"
        [ -x "$c" ] || continue
        if "$c" version 2>/dev/null | grep -q '^OpenSSL 3'; then
            printf '%s' "$c"
            return 0
        fi
    done
    return 1
}
if ! OPENSSL="$(resolve_openssl3)"; then
    echo "ERROR: OpenSSL 3 is required (the system /usr/bin/openssl is LibreSSL," >&2
    echo "       which lacks the -legacy flag needed for a macOS-importable p12)." >&2
    echo "       Install it and re-run:  brew install openssl@3" >&2
    exit 1
fi
echo "    using OpenSSL: $OPENSSL ($("$OPENSSL" version 2>/dev/null))"

WORK="$(mktemp -d /tmp/cr-signing.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT INT TERM     # p12 + key material never persist
KEY="$WORK/key.pem"; CRT="$WORK/cert.pem"; P12="$WORK/id.p12"; CNF="$WORK/openssl.cnf"
P12_PASS="$(openssl rand -hex 16)"      # ephemeral, consumed same-run, never stored

# Self-contained openssl config — no dependency on /etc/ssl/openssl.cnf.
cat > "$CNF" <<EOF
[req]
distinguished_name = dn
x509_extensions    = v3_codesign
prompt             = no
[dn]
CN = $CERT_CN
[v3_codesign]
basicConstraints   = critical,CA:false
keyUsage           = critical,digitalSignature
extendedKeyUsage   = critical,codeSigning
EOF

# 10-year self-signed leaf (long-lived so the DR never churns on expiry).
"$OPENSSL" req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CRT" -days 3650 -config "$CNF"

# LEGACY-encoded PKCS12: modern OpenSSL-3 default MAC (SHA-256) makes macOS
# `security import` fail. SHA1/3DES + sha1 MAC is what macOS accepts.
"$OPENSSL" pkcs12 -export -legacy \
    -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 \
    -inkey "$KEY" -in "$CRT" \
    -name "$CERT_CN" -out "$P12" -passout "pass:$P12_PASS"

# Import into login keychain. -T /usr/bin/codesign pre-authorises ONLY codesign
# to use the private key non-interactively. No -A (would authorise all apps).
security import "$P12" -k "$KEYCHAIN" -P "$P12_PASS" -T /usr/bin/codesign

# Best-effort: suppress the one-time "codesign wants to use key" ACL dialog on
# unattended rebuilds. Non-fatal — the -T import ACL already makes codesign
# non-interactive on its own. SCOPED to ONLY this cert's key via `-s -l "$CERT_CN"`
# so it can never rewrite the partition list of unrelated private keys (SSH keys,
# other signing identities) in the login keychain. The empty -k password only
# succeeds on a passwordless keychain; a real password makes this skip harmlessly
# (you may then get one 'Always Allow' click on the first build).
security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -l "$CERT_CN" -k "" "$KEYCHAIN" >/dev/null 2>&1 \
    || echo "    (set-key-partition-list skipped — you may get one 'Always Allow' click on first build.)"

LEAF=$(security find-certificate -c "$CERT_CN" -Z "$KEYCHAIN" 2>/dev/null \
         | awk '/SHA-1 hash:/ {print $NF}')
echo ""
echo "==> Identity ready. Leaf SHA-1: ${LEAF:-<unavailable>}"
echo "    DR the daemon will carry:"
echo "      identifier \"dev.jamiewhite.contextrecall.daemon\" and certificate leaf = H\"${LEAF}\""
echo ""
echo "Next: make build  ->  deploy  ->  launchctl bootout then bootstrap  ->  click Allow ONCE."
echo "From then on the grant survives all future rebuilds signed with this cert."
