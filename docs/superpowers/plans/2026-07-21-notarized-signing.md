# Notarized Developer ID Signing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Developer-ID + Hardened-Runtime + notarized+stapled signing tier to the daemon and outer app (local deploy + CI), so macOS TCC grants survive OS updates — while keeping every existing fallback (self-signed cert, ad-hoc) fully intact.

**Architecture:** A shared bash resolver (`scripts/signing_lib.sh`) picks a signing identity from a 4-tier hierarchy (explicit env → Developer ID Application cert → self-signed cert → ad-hoc) and reports whether Hardened Runtime applies. `build_daemon.sh` and `inject_daemon.sh` source it. Notarization is a separate final step (`notarize_and_staple.sh`) run once after all content is signed. CI gains cert-import + notarize steps gated on GitHub Secrets, mirroring the existing "never hard-fail" pattern.

**Tech Stack:** bash, macOS `codesign` / `xcrun notarytool` / `xcrun stapler` / `spctl`, PyInstaller daemon bundle, Tauri 2.x app, GitHub Actions.

## Global Constraints

- **Fallbacks never break:** the resolver is a strict superset of today's `explicit env → self-signed → ad-hoc`. The self-signed cert (`CN="Context Recall Self-Signed"`) and ad-hoc paths must behave exactly as they do today when no Developer ID cert is present.
- **Hardened Runtime + secure timestamp apply ONLY to a Developer ID identity** (identity string contains `"Developer ID"`). Self-signed and ad-hoc must keep `--timestamp=none` and no `--options runtime` (a self-signed cert cannot obtain a secure timestamp; it would fail).
- **Never sign with an "Apple Development" identity** for the daemon — tccd rejects it without a provisioning profile (`OS_REASON_TCC`). Only Developer ID / self-signed / ad-hoc are valid.
- **Daemon signing identifier:** `dev.jamiewhite.contextrecall.daemon`. SCK helper identifier: `dev.jamiewhite.contextrecall.sck`.
- **Notarization must happen once, after ALL content is final** (after `inject_daemon.sh` re-signs the outer app) — re-signing invalidates the ticket.
- **Daemon Hardened-Runtime entitlement floor:** `com.apple.security.cs.disable-library-validation` (torch/numpy dylibs not signed by the Team ID). Add further entitlements ONLY in response to a real notarization/launch failure, never a guessed superset.
- **CI stays "never hard-fail":** when signing/notary secrets are absent, CI builds exactly as today (unsigned, green). Gate every new CI step on the relevant secret, mirroring the existing `TAURI_SIGNING_PRIVATE_KEY` gating.
- **No Claude/AI attribution** in commit messages.
- **Team ID:** `34FA3W7TK5`. Notary keychain profile name: `context-recall-notary`.
- **Run all commands from the worktree root:** `/Users/jamiewhite/Documents/Personal/Projects/context-recall/.claude/worktrees/notarized-signing`.

---

## File Structure

**Create:**

- `scripts/signing_lib.sh` — sourced resolver: `cr_resolve_signing`, `cr_developer_id_identity`, `cr_self_signed_present`; sets `CR_SIGN_IDENTITY` / `CR_HARDENED` / `CR_SIGN_TIER`.
- `scripts/daemon.entitlements` — daemon Hardened-Runtime entitlements (minimal).
- `scripts/setup_notary_profile.sh` — one-time prereq check + notary credential setup.
- `scripts/notarize_and_staple.sh` — `notarytool submit --wait` + `stapler staple`, with auto `notarytool log` on failure.
- `scripts/verify_signing.sh` — codesign/spctl/stapler verification checkpoints.
- `tests/test_signing_lib.sh` — bash unit tests for the resolver (mocked detection).

**Modify:**

- `scripts/build_daemon.sh` — source `signing_lib.sh`; sign daemon + SCK helper via the resolver (Hardened Runtime + timestamp + entitlements for Developer ID; `--timestamp=none` otherwise).
- `scripts/inject_daemon.sh` — source `signing_lib.sh`; re-sign the outer app via the resolver (Developer ID + Hardened Runtime when available; ad-hoc otherwise).
- `ui/src-tauri/Entitlements.plist` — add the Hardened-Runtime WKWebView trio.
- `.github/workflows/release.yml` — cert import + signing env + notarize, gated on secrets.
- `CLAUDE.md` — update the deploy sequence + "GitHub-released daemons stay ad-hoc" note.

---

## Task 1: Shared signing resolver (`scripts/signing_lib.sh`)

**Files:**

- Create: `scripts/signing_lib.sh`
- Test: `tests/test_signing_lib.sh` (create)

**Interfaces:**

- Produces (sourced API):
  - `cr_developer_id_identity()` → echoes the full name of a "Developer ID Application" identity, or empty. Overridable in tests.
  - `cr_self_signed_present()` → exit 0 if the `Context Recall Self-Signed` cert exists. Overridable in tests.
  - `cr_resolve_signing()` → sets globals `CR_SIGN_IDENTITY` (codesign `--sign` arg), `CR_HARDENED` (`1`/`0`), `CR_SIGN_TIER` (`env`|`developer-id`|`self-signed`|`adhoc`).
  - `CR_SELF_SIGNED_CN="Context Recall Self-Signed"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_signing_lib.sh`:

```bash
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash tests/test_signing_lib.sh`
Expected: FAIL — `scripts/signing_lib.sh` does not exist (source error).

- [ ] **Step 3: Implement the resolver**

Create `scripts/signing_lib.sh`:

```bash
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
```

- [ ] **Step 4: Run it to verify it passes**

Run: `bash tests/test_signing_lib.sh`
Expected: PASS — five `ok -` lines, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/signing_lib.sh tests/test_signing_lib.sh
git commit -m "build(signing): shared identity resolver with Developer ID tier"
```

---

## Task 2: Entitlements

**Files:**

- Create: `scripts/daemon.entitlements`
- Modify: `ui/src-tauri/Entitlements.plist`

**Interfaces:**

- Produces: `scripts/daemon.entitlements` (consumed by `build_daemon.sh` Task 3 signing).

- [ ] **Step 1: Create the daemon entitlements**

Create `scripts/daemon.entitlements`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- PyInstaller bundles torch/numpy/etc. dylibs that are NOT signed by
         this Team ID. Under Hardened Runtime, loading them requires disabling
         library validation. This is the minimal entitlement set; add more
         ONLY in response to a concrete notarization/launch failure. -->
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
</dict>
</plist>
```

- [ ] **Step 2: Validate it is a well-formed plist**

Run: `plutil -lint scripts/daemon.entitlements`
Expected: `scripts/daemon.entitlements: OK`

- [ ] **Step 3: Add Hardened-Runtime entitlements to the app**

Edit `ui/src-tauri/Entitlements.plist` — insert these three keys inside the top-level `<dict>` (after the existing `com.apple.security.files.user-selected.read-write` block, before `</dict>`):

```xml
    <!-- Hardened Runtime: WKWebView (Tauri's renderer) JITs JavaScript and
         maps unsigned executable memory; Hardened Runtime blocks both without
         these. disable-library-validation lets the app load the injected
         daemon + its unsigned-by-us dylibs. -->
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
```

- [ ] **Step 4: Validate the app plist**

Run: `plutil -lint ui/src-tauri/Entitlements.plist`
Expected: `ui/src-tauri/Entitlements.plist: OK`

- [ ] **Step 5: Commit**

```bash
git add scripts/daemon.entitlements ui/src-tauri/Entitlements.plist
git commit -m "build(signing): Hardened Runtime entitlements for daemon and app"
```

---

## Task 3: `build_daemon.sh` — resolver-driven daemon + helper signing

**Files:**

- Modify: `scripts/build_daemon.sh`

**Interfaces:**

- Consumes: `scripts/signing_lib.sh` (`cr_resolve_signing`, `CR_SIGN_IDENTITY`, `CR_HARDENED`, `CR_SIGN_TIER`), `scripts/daemon.entitlements`.

- [ ] **Step 1: Source the resolver and replace the inline identity block**

In `scripts/build_daemon.sh`, replace the identity-resolution block (current lines 83–94, from `SELF_SIGNED_CN="Context Recall Self-Signed"` through the closing `fi` of the `if [ -z "$SIGN_IDENTITY" ]` block) with:

```bash
SIGN_IDENTIFIER="dev.jamiewhite.contextrecall.daemon"
ENTITLEMENTS="$PROJECT_ROOT/scripts/daemon.entitlements"

# Resolve the signing identity (tier: env > Developer ID > self-signed > ad-hoc).
source "$SCRIPT_DIR/signing_lib.sh"
cr_resolve_signing
echo "==> Signing tier: $CR_SIGN_TIER (identity: $CR_SIGN_IDENTITY, hardened: $CR_HARDENED)"
```

- [ ] **Step 2: Add a shared bundle-signing helper**

In `scripts/build_daemon.sh`, replace the `sign_adhoc()` function (current lines 96–102) with a single resolver-aware helper:

```bash
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
        codesign --force --sign "$CR_SIGN_IDENTITY" --identifier "$ident" \
            --options runtime --timestamp --entitlements "$ENTITLEMENTS" "$path" && return
    else
        codesign --force --sign "$CR_SIGN_IDENTITY" --identifier "$ident" \
            --timestamp=none "$path" && return
    fi
    echo "    WARNING: signing '$path' with '$CR_SIGN_IDENTITY' failed; falling back to ad-hoc."
    codesign --force --sign - --identifier "$ident" "$path"
}
```

- [ ] **Step 3: Route the SCK helper signing through `cr_sign`**

In `scripts/build_daemon.sh`, replace the helper-signing `if/else` (current lines 117–123 — the block that chooses ad-hoc vs `--timestamp=none`) with:

```bash
        cr_sign "$HELPER_DEST" "$HELPER_IDENTIFIER"
```

- [ ] **Step 4: Route the daemon-bundle signing through `cr_sign`**

In `scripts/build_daemon.sh`, replace the daemon-signing block (current lines 133–145, the `if [ "$SIGN_IDENTITY" = "-" ]; then sign_adhoc; else … fi`) with:

```bash
echo "==> Codesigning daemon app bundle (tier: $CR_SIGN_TIER)"
cr_sign "$APP_DIR" "$SIGN_IDENTIFIER"
```

(Leave the subsequent `codesign --verify` and `codesign -d -r-` DR echo lines unchanged.)

- [ ] **Step 5: Syntax check**

Run: `bash -n scripts/build_daemon.sh`
Expected: no output, exit 0.

- [ ] **Step 6: Functionally verify the self-signed + ad-hoc branches of `cr_sign`**

This machine has the self-signed cert but no Developer ID cert, so the self-signed and ad-hoc branches are exercisable without a full daemon rebuild. Run this throwaway-bundle harness:

```bash
bash -c '
set -e
source scripts/signing_lib.sh
ENTITLEMENTS="$PWD/scripts/daemon.entitlements"
cr_sign() { # copied from build_daemon.sh for isolated verification
    local path="$1" ident="$2"
    if [ "$CR_SIGN_IDENTITY" = "-" ]; then codesign --force --sign - --identifier "$ident" "$path"; return; fi
    if [ "$CR_HARDENED" = "1" ]; then
        codesign --force --sign "$CR_SIGN_IDENTITY" --identifier "$ident" --options runtime --timestamp --entitlements "$ENTITLEMENTS" "$path" && return
    else
        codesign --force --sign "$CR_SIGN_IDENTITY" --identifier "$ident" --timestamp=none "$path" && return
    fi
    codesign --force --sign - --identifier "$ident" "$path"
}
T=$(mktemp -d); APP="$T/x.app"; mkdir -p "$APP/Contents/MacOS"
printf "#!/bin/sh\nexit 0\n" > "$APP/Contents/MacOS/x"; chmod +x "$APP/Contents/MacOS/x"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleExecutable</key><string>x</string><key>CFBundleIdentifier</key><string>test.x</string></dict></plist>
EOF
# Force ad-hoc:
CONTEXT_RECALL_SIGN_IDENTITY="" cr_developer_id_identity(){ :; }; cr_self_signed_present(){ return 1; }; cr_resolve_signing
cr_sign "$APP" "test.x"; codesign --verify "$APP" && echo "ad-hoc branch OK"
# Force self-signed (real cert on this machine):
cr_self_signed_present(){ return 0; }; cr_resolve_signing
cr_sign "$APP" "test.x"; codesign --verify "$APP" && codesign -d -r- "$APP" 2>&1 | grep -q "certificate leaf" && echo "self-signed branch OK (cert-leaf DR)"
rm -rf "$T"
'
```

Expected: `ad-hoc branch OK` and `self-signed branch OK (cert-leaf DR)`. (The Developer ID branch is verified end-to-end only after the cert exists — see the final Integration checkpoint.)

- [ ] **Step 7: Commit**

```bash
git add scripts/build_daemon.sh
git commit -m "build(signing): sign daemon + SCK helper via resolver (Developer ID hardened tier)"
```

---

## Task 4: `inject_daemon.sh` — resolver-driven outer-app re-seal

**Files:**

- Modify: `scripts/inject_daemon.sh`

**Interfaces:**

- Consumes: `scripts/signing_lib.sh` (`cr_resolve_signing`, `CR_SIGN_IDENTITY`, `CR_HARDENED`, `CR_SIGN_TIER`); `ui/src-tauri/Entitlements.plist` (app entitlements).

- [ ] **Step 1: Replace the ad-hoc re-seal with a resolver-driven re-seal**

In `scripts/inject_daemon.sh`, replace the re-seal block (current lines 53–56, the comment + `codesign --force --sign - "$APP"`) with:

```bash
# Replacing resources invalidates the outer app's seal; re-sign it. The nested
# daemon bundle keeps its own signature (signed by build_daemon.sh). Use the
# same identity tier: Developer ID -> Hardened Runtime + timestamp + the app
# entitlements (notarizable); self-signed/ad-hoc -> as before. Degrade to
# ad-hoc on failure so CI/fresh clones never abort.
source "$SCRIPT_DIR/signing_lib.sh"
cr_resolve_signing
APP_ENTITLEMENTS="$PROJECT_ROOT/ui/src-tauri/Entitlements.plist"
echo "==> Re-sealing outer app (tier: $CR_SIGN_TIER)"
if [ "$CR_SIGN_IDENTITY" = "-" ]; then
    codesign --force --sign - "$APP"
elif [ "$CR_HARDENED" = "1" ]; then
    codesign --force --sign "$CR_SIGN_IDENTITY" --options runtime --timestamp \
        --entitlements "$APP_ENTITLEMENTS" "$APP" \
        || { echo "    WARNING: Developer ID re-seal failed; ad-hoc."; codesign --force --sign - "$APP"; }
else
    codesign --force --sign "$CR_SIGN_IDENTITY" --timestamp=none "$APP" \
        || { echo "    WARNING: self-signed re-seal failed; ad-hoc."; codesign --force --sign - "$APP"; }
fi
```

- [ ] **Step 2: Syntax check**

Run: `bash -n scripts/inject_daemon.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/inject_daemon.sh
git commit -m "build(signing): re-seal outer app via resolver (Developer ID hardened tier)"
```

---

## Task 5: `notarize_and_staple.sh`

**Files:**

- Create: `scripts/notarize_and_staple.sh`

**Interfaces:**

- Produces: `scripts/notarize_and_staple.sh <path-to-.dmg-or-.app>` — submits to the notary service using either a stored keychain profile (`context-recall-notary`) or explicit API-key env vars, waits, staples on success, prints the notary log on failure.

- [ ] **Step 1: Create the script**

Create `scripts/notarize_and_staple.sh`:

```bash
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
        echo "==> Notarizing with App Store Connect API key (env)"
        xcrun notarytool submit "$TARGET" \
            --key "$APPLE_API_KEY_P8_PATH" \
            --key-id "$APPLE_API_KEY_ID" \
            --issuer "$APPLE_API_KEY_ISSUER_ID" \
            --wait --output-format plist
    elif xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
        echo "==> Notarizing with stored keychain profile '$PROFILE'"
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
```

- [ ] **Step 2: Syntax check**

Run: `bash -n scripts/notarize_and_staple.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Verify the no-credentials failure path is clean**

Run (no env creds, no profile — this machine has neither yet):
`env -u APPLE_API_KEY_P8_PATH ./scripts/notarize_and_staple.sh scripts/daemon.entitlements`
Expected: exits `2` with the "no notarization credentials" guidance (it reaches credential resolution before any notary call, since the target exists).

- [ ] **Step 4: Commit**

```bash
git add scripts/notarize_and_staple.sh
git commit -m "build(signing): notarize + staple script with API-key and keychain-profile creds"
```

---

## Task 6: `setup_notary_profile.sh`

**Files:**

- Create: `scripts/setup_notary_profile.sh`

**Interfaces:**

- Produces: `scripts/setup_notary_profile.sh` — verifies the Developer ID Application cert exists, then creates the `context-recall-notary` keychain profile from a `.p8` + Key ID + Issuer ID.

- [ ] **Step 1: Create the script**

Create `scripts/setup_notary_profile.sh`:

```bash
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

Create one (REQUIRES a paid Apple Developer Program membership — a free
"Personal Team" Apple ID cannot create Developer ID certs or notarize;
enroll at developer.apple.com/account first):
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
```

- [ ] **Step 2: Syntax check**

Run: `bash -n scripts/setup_notary_profile.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Verify the missing-cert guidance path (this machine has no Developer ID cert yet)**

Run: `./scripts/setup_notary_profile.sh`
Expected: exits `1` printing the "Create one … Developer ID Application" guidance (Prereq 1 fails first, as intended).

- [ ] **Step 4: Commit**

```bash
git add scripts/setup_notary_profile.sh
git commit -m "build(signing): one-time notary credential setup with prereq checks"
```

---

## Task 7: `verify_signing.sh`

**Files:**

- Create: `scripts/verify_signing.sh`

**Interfaces:**

- Produces: `scripts/verify_signing.sh <path-to-.app>` — prints a signing/notarization status report and exits 0 if `codesign --verify` passes (Gatekeeper/notarization lines are informational, since they only pass once notarized).

- [ ] **Step 1: Create the script**

Create `scripts/verify_signing.sh`:

```bash
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
```

- [ ] **Step 2: Syntax check**

Run: `bash -n scripts/verify_signing.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Run it against the currently-installed app (real, self-signed/ad-hoc build)**

Run: `./scripts/verify_signing.sh "/Applications/Context Recall.app/Contents/Resources/resources/context-recall-daemon/Context Recall Daemon.app"`
Expected: `codesign --verify` PASS; the DR line shows the `certificate leaf` (self-signed) DR; Hardened Runtime flag absent; Gatekeeper reports rejected/unnotarized; staple "not stapled". This confirms the report is accurate on a real bundle (it will show all-green only after a notarized build).

- [ ] **Step 4: Commit**

```bash
git add scripts/verify_signing.sh
git commit -m "build(signing): signing/notarization status verifier"
```

---

## Task 8: CI — `release.yml` signing + notarization (gated on secrets)

**Files:**

- Modify: `.github/workflows/release.yml`

**Interfaces:**

- Consumes: `scripts/build_daemon.sh`, `scripts/inject_daemon.sh`, `scripts/notarize_and_staple.sh`, and GitHub Secrets `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_TEAM_ID`, `APPLE_API_KEY_P8`, `APPLE_API_KEY_ID`, `APPLE_API_KEY_ISSUER_ID`.

- [ ] **Step 1: Add a reusable cert-import step to the `build-daemon` job**

In `.github/workflows/release.yml`, in the `build-daemon` job, insert BEFORE the `- name: Build daemon binary` step (so `build_daemon.sh` sees the identity):

```yaml
- name: Import Developer ID certificate
  if: ${{ env.APPLE_CERTIFICATE != '' }}
  env:
    APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}
    APPLE_CERTIFICATE_PASSWORD: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}
  run: |
    KEYCHAIN="$RUNNER_TEMP/cr-signing.keychain-db"
    KPASS="$(uuidgen)"
    security create-keychain -p "$KPASS" "$KEYCHAIN"
    security set-keychain-settings -lut 21600 "$KEYCHAIN"
    security unlock-keychain -p "$KPASS" "$KEYCHAIN"
    echo -n "$APPLE_CERTIFICATE" | base64 --decode -o "$RUNNER_TEMP/cert.p12"
    security import "$RUNNER_TEMP/cert.p12" -k "$KEYCHAIN" \
      -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KPASS" "$KEYCHAIN"
    security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
    IDENT="$(security find-identity -v -p codesigning "$KEYCHAIN" | grep 'Developer ID Application' | head -1 | sed -E 's/.*"(.*)".*/\1/')"
    echo "CONTEXT_RECALL_SIGN_IDENTITY=$IDENT" >> "$GITHUB_ENV"
    echo "==> Imported Developer ID identity: $IDENT"
```

Add `APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}` to a job-level `env:` on `build-daemon` so the `if:` can read it (mirroring how `TAURI_SIGNING_PRIVATE_KEY` is referenced). Concretely, add to the `build-daemon` job, directly under `runs-on: macos-14`:

```yaml
env:
  APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}
```

(`build_daemon.sh` already reads `CONTEXT_RECALL_SIGN_IDENTITY` as tier 1, so no build-command change is needed — the resolver signs Developer ID + Hardened Runtime automatically.)

- [ ] **Step 2: Import the cert + set the app signing identity in the `build-app` job**

In the `build-app` job, add the same job-level `env: APPLE_CERTIFICATE:` (under `runs-on: macos-14`), and insert the SAME `Import Developer ID certificate` step BEFORE `- name: Build Tauri app`. Then add `APPLE_SIGNING_IDENTITY` for Tauri's own signing by extending the existing `Build Tauri app` step's `env:` block with:

```yaml
APPLE_SIGNING_IDENTITY: ${{ env.CONTEXT_RECALL_SIGN_IDENTITY }}
```

(When the secret is absent, `CONTEXT_RECALL_SIGN_IDENTITY` is empty and Tauri signs ad-hoc via the existing `"-"` config — unchanged behavior.)

- [ ] **Step 3: Notarize + staple the injected DMG**

In the `build-app` job, immediately AFTER the `- name: Re-inject daemon bundle and rebuild DMG` step, insert:

```yaml
- name: Notarize and staple DMG
  if: ${{ env.APPLE_CERTIFICATE != '' }}
  env:
    APPLE_API_KEY_P8: ${{ secrets.APPLE_API_KEY_P8 }}
    APPLE_API_KEY_ID: ${{ secrets.APPLE_API_KEY_ID }}
    APPLE_API_KEY_ISSUER_ID: ${{ secrets.APPLE_API_KEY_ISSUER_ID }}
  run: |
    echo -n "$APPLE_API_KEY_P8" | base64 --decode -o "$RUNNER_TEMP/AuthKey.p8"
    export APPLE_API_KEY_P8_PATH="$RUNNER_TEMP/AuthKey.p8"
    ./scripts/notarize_and_staple.sh \
      "ui/src-tauri/target/release/bundle/dmg/Context Recall_injected_aarch64.dmg"
```

- [ ] **Step 4: Validate the workflow YAML**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release.yml')); print('YAML OK')"`
Expected: `YAML OK`.

- [ ] **Step 5: Review gating logic (no CI run possible here)**

Confirm by reading the diff that every new step is gated on `env.APPLE_CERTIFICATE != ''`, and that with the secret absent the job graph is byte-for-byte the old behavior (unsigned build, unsigned DMG, green). Note in the commit body that end-to-end CI verification requires the secrets to be configured and a `v*` tag push.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci(signing): Developer ID sign + notarize releases, gated on secrets"
```

---

## Task 9: Docs — `CLAUDE.md`

**Files:**

- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the deploy sequence + ad-hoc note**

In `CLAUDE.md`, in the "Stable microphone grant across rebuilds" section, add a paragraph documenting the notarized tier and the new deploy sequence, and correct the "GitHub-released daemons stay ad-hoc" line. Insert after the existing "Deploy sequence that preserves the stable signature" paragraph:

```markdown
**Notarized (Developer ID) signing — durable across OS updates.** Self-signed grants
survive rebuilds but NOT macOS OS updates (observed 2026-07-21: macOS 26.6 wiped the
daemon's Calendar/Mic grants). The durable fix is a Developer-ID-signed, Hardened-Runtime,
**notarized** build. `scripts/signing_lib.sh` resolves the identity in tiers: explicit
`CONTEXT_RECALL_SIGN_IDENTITY` → a "Developer ID Application" cert (→ Hardened Runtime +
timestamp + entitlements, notarizable) → the self-signed cert → ad-hoc. Local one-time
setup: create a Developer ID Application cert + an App Store Connect API key, then run
`scripts/setup_notary_profile.sh`. Notarized deploy sequence: `build_daemon.sh` →
`npm run tauri build` (with `APPLE_SIGNING_IDENTITY`) → `scripts/inject_daemon.sh "<app>"`
(re-signs the outer app Developer ID + Hardened Runtime) → rebuild DMG →
`scripts/notarize_and_staple.sh "<dmg>"`. Verify with `scripts/verify_signing.sh "<app>"`
(expect `spctl` "Notarized Developer ID"). CI notarizes when the `APPLE_*` secrets are set;
absent them it still builds ad-hoc/unsigned (never hard-fails).
```

Also change the sentence "GitHub-released daemons stay ad-hoc — the stable grant is a
local-deploy benefit." to: "GitHub-released daemons are Developer-ID-signed + notarized
when the `APPLE_*` CI secrets are configured; otherwise they fall back to ad-hoc."

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document notarized Developer ID signing pipeline"
```

---

## Integration checkpoint (manual, after the user creates the Developer ID cert)

Not a code task — the end-to-end Developer ID + notarization path cannot run until the
cert + API key exist. Once the user completes the one-time setup:

1. `scripts/setup_notary_profile.sh /path/AuthKey_XXXX.p8 <KEY_ID> <ISSUER_ID>` → profile stored.
2. `./scripts/build_daemon.sh` → log shows `Signing tier: developer-id`, `hardened: 1`.
3. `cd ui && npm run tauri build` (with `APPLE_SIGNING_IDENTITY` exported).
4. `./scripts/inject_daemon.sh "…/Context Recall.app"` → re-seal tier `developer-id`.
5. Rebuild the DMG (`hdiutil create …`, as CI does).
6. `./scripts/notarize_and_staple.sh "…Context Recall_injected_aarch64.dmg"` → Accepted + stapled.
7. `./scripts/verify_signing.sh "…/Context Recall.app"` → `codesign` PASS, DR = Developer ID, `flags=…(runtime)`, `spctl` "accepted, source=Notarized Developer ID".
8. Install, `launchctl bootout`/`bootstrap`, grant Calendar/Mic/Screen-Recording once. The
   durable OS-update-survival is only observable at the next OS update (documented caveat).

---

## Self-review (author checklist — completed)

**Spec coverage:** §1 resolver → Task 1; §2 entitlements → Task 2; §3 pipeline (daemon
sign → Task 3, app re-seal → Task 4, notarize+staple → Task 5) ; §4 `setup_notary_profile`
→ Task 6; §5 CI → Task 8; verification (`verify_signing`) → Task 7; docs → Task 9. The
spec's "Certainty caveat" is honored in the Integration checkpoint and Task 8/7 expected
outputs (verified only to Gatekeeper acceptance).

**Placeholder scan:** no TBD/TODO. The daemon-entitlements "add more only on real failure"
is a deliberate minimal-first strategy, and every code step shows full content.

**Type/name consistency:** `cr_resolve_signing` / `CR_SIGN_IDENTITY` / `CR_HARDENED` /
`CR_SIGN_TIER` / `CR_SELF_SIGNED_CN` / `cr_developer_id_identity` / `cr_self_signed_present`
/ `cr_sign` / profile `context-recall-notary` / identifiers
`dev.jamiewhite.contextrecall.daemon` + `.sck` are used identically across every task and
match the current scripts.

**Testability honesty:** the resolver is fully unit-tested (Task 1); self-signed + ad-hoc
signing branches are functionally verified against a throwaway bundle (Task 3 Step 6) and
the real installed bundle (Task 7 Step 3); the Developer ID + notarization path is a
documented manual Integration checkpoint, not a fabricated test.
