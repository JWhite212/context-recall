# Design: Notarized (Developer ID) signing for the daemon + app

**Date:** 2026-07-21
**Branch:** `feat/notarized-signing` (off `main`)
**Status:** Approved design — ready for implementation planning

## Problem

The Context Recall daemon is a headless launchd binary that needs macOS TCC grants
(Calendar, Microphone, Screen Recording). Today it is signed with a **per-machine
self-signed cert** (`scripts/setup_signing_cert.sh`, `CN="Context Recall Self-Signed"`),
which gives a stable cert-leaf Designated Requirement across _rebuilds_. The outer
Tauri app is signed **ad-hoc** (`"signingIdentity": "-"` in `tauri.conf.json`).

**Observed 2026-07-21:** after updating to macOS 26.6 (build 25G70) and rebooting, the
daemon's TCC grants were wiped (Calendar + Microphone → `not_determined`, Screen
Recording → `denied`). Calendar events had displayed correctly at 15:20 on this exact
build; the OS update invalidated the grants. Every in-session re-grant path failed:

- `tccutil reset` + `launchctl bootout/bootstrap` → the daemon's Calendar prompt never
  surfaced (confirmed: user "didn't see the popup").
- Manual add via System Settings → Privacy & Security → Calendars → **there is no "+"**
  for this pane / the nested daemon `.app` cannot be added.
- Launching the daemon `.app` as a foreground GUI app via `open` → prompt still did not
  surface/persist; grant flapped `authorized`→`not_determined` and never finalised.
- A second clean reboot → still `not_determined`.

This is **not a code bug** — the installed build already contains every relevant fix
(the EKEventStore-leak fix `09103c8` was built into the 07-17 daemon; `freeze_support`,
speechbrain-as-source, and the calendar request endpoint are all present). The root
cause is that **self-signed / ad-hoc macOS apps have their TCC grants reset by OS
updates and are increasingly restricted from (re)acquiring them.** Apple's documented
durable path is a **notarized, Developer-ID-signed** app with Hardened Runtime.

Prerequisite already satisfied: the user has an **Apple Developer Program membership**
(Team ID `34FA3W7TK5`, visible on an existing "Apple Development" cert in the login
keychain). What is missing is a **Developer ID Application** certificate (a distinct
cert type from "Apple Development") and an App Store Connect API key — both are one-time
manual steps only the user can perform.

## Goal

Make the deploy pipeline (local and CI) produce a **Developer-ID-signed, Hardened-Runtime,
notarized + stapled** daemon and outer app, so TCC grants survive OS updates — while
keeping every existing fallback (self-signed cert, ad-hoc) fully intact so nothing that
works today breaks before the Developer ID cert exists.

## Key macOS facts that shape the design

- **Notarization inspects every nested executable.** Apple's notary service rejects a
  bundle unless every Mach-O inside (the daemon binary, the SCK helper, PyInstaller's
  bundled dylibs, the outer app) is signed with a **Developer ID** identity and
  **Hardened Runtime** (`codesign --options runtime`) plus a **secure timestamp**
  (`--timestamp`).
- **Re-signing invalidates a notarization ticket.** Notarization must happen **once,
  after all bundle content is final** — i.e. after `inject_daemon.sh` swaps in the
  pristine daemon and re-signs the outer app, not before.
- **Hardened Runtime + third-party dylibs need an entitlement.** PyInstaller bundles
  torch/numpy/etc. dylibs that are not signed by the user's Team ID; under Hardened
  Runtime, loading them requires `com.apple.security.cs.disable-library-validation`.
  WKWebView (Tauri) additionally needs `com.apple.security.cs.allow-jit` and
  `com.apple.security.cs.allow-unsigned-executable-memory`.
- **"Apple Development" identity must NOT be used for the daemon.** Per the existing
  `setup_signing_cert.sh` note, tccd rejects an "Apple Development"-signed bundle without
  an embedded provisioning profile (`OS_REASON_TCC`). Only **Developer ID Application**
  (or self-signed / ad-hoc) are valid here.
- **Certainty caveat:** the _actual_ fix (grants surviving a future OS update) cannot be
  proven until a real future OS update happens. This design implements Apple's
  documented durable path; it does not guarantee the OS behaves — that is explicitly
  out of our control.

## Architecture

### 1. Signing-identity resolution — a strict superset of today's hierarchy

Both the daemon signing (`build_daemon.sh`) and the outer-app re-sign (`inject_daemon.sh`)
resolve an identity in this priority order:

1. Explicit `CONTEXT_RECALL_SIGN_IDENTITY` env override — **existing** escape hatch.
2. **A "Developer ID Application" cert present in the keychain — NEW.** Detected via
   `security find-identity -v -p codesigning | grep "Developer ID Application"`. When
   used, signing adds `--options runtime --timestamp --entitlements <file>`.
3. The existing **"Context Recall Self-Signed"** cert — **existing** fallback, unchanged
   (stable across rebuilds, not notarizable).
4. Ad-hoc (`--sign -`) — **existing** last resort (CI / fresh clones with no cert).

A shared resolver (small bash function, factored so `build_daemon.sh` and
`inject_daemon.sh` agree) returns both the identity string and whether Hardened-Runtime
flags apply (tiers 1 explicit + 2 Developer ID → yes; self-signed / ad-hoc → no, because
Hardened Runtime is meaningless/counterproductive without notarization).

**Nothing changes until a Developer ID cert exists.** Tier 2 activates automatically on
the next build once the user creates the cert.

### 2. Entitlements

- **New `scripts/daemon.entitlements`** — starts minimal:
  `com.apple.security.cs.disable-library-validation` (torch/numpy dylibs). Additional
  entitlements are added ONLY in response to a real notarization rejection or launch
  failure, each justified by the `notarytool log` / crash reason — never a guessed
  superset.
- **`ui/src-tauri/Entitlements.plist`** — add the Hardened-Runtime WKWebView trio
  (`allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation`)
  alongside its existing sandbox=false / audio-input / network-client / files entries.

### 3. Pipeline ordering (local deploy)

```
scripts/build_daemon.sh          → sign daemon: Developer ID + --options runtime
                                    --timestamp --entitlements scripts/daemon.entitlements
                                    (tier 2). SCK helper signed inside-out with the same.
cd ui && npm run tauri build      → outer app signed Developer ID via APPLE_SIGNING_IDENTITY
                                    env (set from the resolver). Notarization creds are
                                    deliberately NOT exported here, so Tauri's bundler does
                                    not auto-notarize pre-injection content.
scripts/inject_daemon.sh <app>    → swap in pristine daemon (symlink-preserving), THEN
                                    re-sign the OUTER app with Developer ID + Hardened
                                    Runtime (replaces today's always-ad-hoc `codesign
                                    --sign -`). Nested daemon keeps its own DR.
[rebuild DMG via hdiutil]          → unchanged (CI already does this; add a local step).
scripts/notarize_and_staple.sh     → NEW. xcrun notarytool submit "<dmg>" --wait using the
                                    stored keychain profile; on failure auto-runs
                                    `xcrun notarytool log <submission-id>` and prints it;
                                    on success `xcrun stapler staple` the .app AND .dmg.
```

Deploy-sequence doc in `CLAUDE.md` ("Deploy sequence that preserves the stable signature")
is updated to the notarized ordering.

### 4. `scripts/setup_notary_profile.sh` (NEW)

Idempotent, one-time-per-machine, mirrors `setup_signing_cert.sh`'s ergonomics:

- Verifies a **Developer ID Application** cert exists (`security find-identity`); if not,
  prints the exact steps (Xcode → Settings → Accounts → Manage Certificates → **+** →
  Developer ID Application) and exits non-zero.
- Verifies/creates a **notarytool keychain profile** (`xcrun notarytool store-credentials
"context-recall-notary" --key <p8> --key-id <id> --issuer <id>`), prompting for the
  three App Store Connect API-key values if the profile is absent. The `.p8` and IDs are
  stored by macOS in the keychain — nothing secret is written to the repo.

### 5. CI (`release.yml`) — gated on secrets, "never hard-fail" pattern

Mirror the existing `TAURI_SIGNING_PRIVATE_KEY` gating exactly. New GitHub Secrets:

- `APPLE_CERTIFICATE` (base64 of the Developer ID Application `.p12`) + `APPLE_CERTIFICATE_PASSWORD`
- `APPLE_TEAM_ID`
- `APPLE_API_KEY_P8` (base64 of the `.p8`) + `APPLE_API_KEY_ID` + `APPLE_API_KEY_ISSUER_ID`

When **present**: create a temporary CI keychain, import the cert, export
`CONTEXT_RECALL_SIGN_IDENTITY` for `build_daemon.sh` and `APPLE_SIGNING_IDENTITY` for
`tauri build`, and run `notarize_and_staple.sh` (with `notarytool submit --key/--key-id/
--issuer` from the secrets instead of a keychain profile) on the final DMG before upload.

When **absent**: CI builds exactly as today (self-signed absent → ad-hoc → unsigned DMG),
never fails. The daemon-build job's `CONTEXT_RECALL_SIGN_IDENTITY` is passed from the
build-app job context (the daemon is built in a separate `build-daemon` job today, so the
cert import must occur in **both** jobs, or the daemon build must move — see Risks).

`CLAUDE.md`'s "GitHub-released daemons stay ad-hoc" line is corrected once this lands.

### 6. What the user must do (one-time, only they can)

1. Create a **Developer ID Application** certificate (Xcode → Settings → Accounts →
   Manage Certificates → **+**). REQUIRES a paid Apple Developer Program
   membership — a free "Personal Team" Apple ID can only make "Apple Development"
   certs and cannot notarize; enroll at developer.apple.com/account first.
2. Create an **App Store Connect API key** with the "Developer" role (appstoreconnect.apple.com
   → Users and Access → Keys); download the `.p8` once, note Key ID + Issuer ID.
3. Run `scripts/setup_notary_profile.sh` (prints exact next steps if either prerequisite
   is missing) and, for local deploys, one `xcrun notarytool store-credentials` command it
   hands over.
4. For CI: base64 the `.p12` and `.p8`, add the 6 secrets above to the GitHub repo.

## Testing / verification

These are bash + CI scripts; the sibling scripts (`build_daemon.sh`, `inject_daemon.sh`,
`setup_signing_cert.sh`) have no pytest coverage, so this work adds **verification
checkpoints**, not a fabricated unit-test suite:

- `codesign --verify --deep --strict --verbose=2 <app>` → exit 0.
- `codesign -d --entitlements - <daemon>` → shows the expected entitlements.
- `codesign -d --verbose=4 <daemon>` → `flags=…(runtime)` present (Hardened Runtime on).
- `codesign -d -r- <daemon>` → `Developer ID Application` cert-leaf DR (tier 2 active) or
  the self-signed / cdhash DR (fallback) — the resolver picked the right tier.
- **Definitive signal:** `spctl -a -vvv --type execute <app>` → "accepted,
  source=Notarized Developer ID" after stapling.
- `xcrun stapler validate <app>` and `<dmg>` → "The validate action worked!".

A `scripts/verify_signing.sh` (NEW) runs these checks and prints a pass/fail summary, used
both locally after a deploy and (best-effort) in CI after notarization.

## Risks / notes

- **CI job split:** `release.yml` builds the daemon in a separate `build-daemon` job from
  the app. Developer ID signing of the daemon must happen in whichever job has the cert
  keychain. Simplest: import the cert in the `build-daemon` job too (sign there, as today),
  and the app job re-signs only the outer app. The plan will confirm the exact job wiring.
- **Notarization latency:** `notarytool submit --wait` typically takes 1–5 min; CI must
  allow for it. Not gated behind a short timeout.
- **Cannot prove the OS-update-survival fix now** — see Certainty caveat above. Verified
  as far as Gatekeeper acceptance + stapling; real durability is observable only at the
  next OS update.
- **Self-signed path stays as the offline/no-Apple-account fallback** — this design adds
  a tier above it, never removes it.
