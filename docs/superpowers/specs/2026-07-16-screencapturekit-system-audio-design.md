# ScreenCaptureKit system-audio capture backend

**Date:** 2026-07-16
**Status:** Design — approved for spec
**Author:** Claude + Jamie

## Problem

On macOS 26.6 beta the Microphone TCC service is broken at **both** layers:

- **Prompt layer** — clicking "Allow" doesn't register (permission stays `not_determined`).
- **Enforcement layer** — even a grant present in `TCC.db` (`auth_value=2`) is not honoured; CoreAudio zeros **every** audio _input_ stream.

Because macOS gates **all** input devices behind Microphone TCC — including the **BlackHole** virtual loopback we use to capture system audio — the daemon records digital silence on both sources (`RMS -100 dBFS` on mic _and_ system). Meetings are captured with no audio. Every app-side remedy (re-Allow, reboot, `tccutil reset`, stable-cert DR, manual `TCC.db` seeding) has been exhausted; this is an OS beta defect, not our bug.

**Confirmed viable escape hatch:** ScreenCaptureKit (SCK) captures **system audio output** through the **Screen Recording** TCC service — a _different_ service that works on this beta. A standalone probe (`sck_probe.swift`) captured real system audio at `-19 dBFS` while TTS played (`permission=granted samples=606720`). SCK does **not** capture microphone input, so it recovers the _remote/meeting_ audio; the user's own voice returns automatically once Apple fixes Microphone TCC.

## Goal

Add a ScreenCaptureKit system-audio capture backend so the daemon recovers meeting audio despite the broken mic. Make SCK the **automatic default** when available (macOS 13+, helper present), falling back to BlackHole otherwise. The microphone source is unchanged — it stays silent on this beta and self-heals when mic TCC is fixed.

## Non-goals (YAGNI)

- **Mic capture via SCK** — impossible; SCK is system-output only. The mic returns via the OS fix.
- **Real-time live-transcription / full level-metering of the SCK source** beyond a periodic RMS heartbeat. The _final_ transcript is complete (the whole merged WAV is transcribed post-capture); only the _live_ stream loses the system source during SCK capture. Deferred.
- **A dedicated Settings "Grant Screen Recording" banner.** v1 surfaces status via the existing preflight route + a `last_warning`; a polished banner is a documented follow-up.
- **Notarized distribution.** Out of scope, as today.
- **Non-macOS.** The app is macOS-only.

## Approaches considered

**A. Signed Swift helper binary the daemon spawns — chosen.** A tiny self-contained Swift executable (built from the proven probe) captures system audio and writes the WAV; the Python daemon drives it as a subprocess, exactly as it already drives `pgrep`/`lsof`/`osascript`. Clean process isolation, unit-testable via a faked subprocess, and the daemon stays pure-Python. The probe already proves the exact SCK API works on this beta.

**B. SCK via Rust in the Tauri shell — rejected.** Capture must run in the _daemon_ (which owns recording), not the UI shell; this misplaces the capture and splits the audio path across two processes.

**C. SCK via pyobjc in the daemon — rejected.** The daemon deliberately avoids pyobjc (uses ctypes elsewhere), and embedding an async SCK run-loop inside the pipeline thread is fragile.

## Architecture

The **only** thing that changes is the _source_ of `meeting_<ts>_system.wav`. Today BlackHole (a `sounddevice.InputStream`) writes it; the SCK backend writes the identical file via a helper subprocess. Mic capture, RMS-normalised merge, diarisation, transcription, and `derive_source_paths` are all **unchanged** — they only ever read `_system.wav` / `_mic.wav`.

```
                       ┌─ system source (swappable backend) ─────────────┐
                       │  BlackHole:  sd.InputStream ─► _system.wav       │
AudioCapture.start ───►│  SCK:        helper subprocess ─► _system.wav    │
                       └──────────────────────────────────────────────────┘
                       └─ mic source (unchanged): sd.InputStream ─► _mic.wav
                                          │
                            _merge_sources() ─► meeting_<ts>.wav ─► pipeline
```

**Contract the helper must honour:** write `_system.wav` as **16 kHz mono PCM-16** (`config.sample_rate`, 1 channel) — byte-for-byte the format the BlackHole path writes today — so `_merge_dual_source`'s "same samplerate/channels" guard passes and _no_ merge/diarise code changes. The helper does the SCK-native 48 kHz-stereo → 16 kHz-mono conversion internally (`AVAudioConverter`).

## Components

### 1. `macos/sck-audio-capture/` — Swift helper (new)

A single-file Swift executable built from `sck_probe.swift`. CLI:

- `--output <path.wav>` — capture system audio, streaming `CMSampleBuffer`s through `AVAudioConverter` into a 16 kHz mono PCM-16 WAV until it receives **SIGTERM/SIGINT**, then finalise the WAV header and `exit(0)`.
- `--check-permission` — probe Screen Recording auth without capturing; print one token (`granted` | `denied` | `undetermined`) and exit. Used by the preflight.
- While capturing, print a periodic `rms=<float>` line to stdout (~10/s) so the driver can keep the system level-meter alive without streaming samples.
- On Screen Recording denied / SCK failure: print `error=<reason>` to stderr and exit **non-zero** with a distinct code, so the driver detects it and surfaces a warning.

Signed with the daemon's **stable self-signed cert** under its own identifier (`dev.jamiewhite.contextrecall.sck`) so its Screen Recording grant persists across rebuilds (same cert-leaf-DR rationale as the daemon).

### 2. `src/system_audio.py` — backend abstraction (new)

- `SystemAudioCapture` protocol: `start(output_path: Path) -> None`, `stop() -> None`, plus `last_error`, `last_warning`, and the `on_audio_level` / `on_audio_data` / `on_stream_status` callback hooks (mirrors what `AudioCapture` already exposes).
- `BlackHoleSystemCapture` — extracts today's BlackHole system-source logic out of `AudioCapture._record_loop`: opens the `sd.InputStream` on BlackHole, writes `_system.wav`, forwards `on_audio_data` (live transcription) + `on_audio_level`.
- `ScreenCaptureKitSystemCapture` — spawns the helper (`--output _system.wav`); `stop()` sends SIGTERM and waits (bounded, e.g. 30 s) for the WAV to finalise; reads `rms=` lines from stdout on a small reader thread → `on_audio_level(system_rms, 0.0)`; non-zero exit → `last_error` / `last_warning`. `preflight()` runs `--check-permission`.
- `select_system_backend(config) -> SystemAudioCapture` — the auto rule:
  - `blackhole` → BlackHole (explicit override).
  - `screencapturekit` → SCK (explicit override).
  - `auto` (default) → **SCK** when macOS ≥ 13 **and** the helper binary resolves; otherwise **BlackHole**. Screen-Recording _permission_ is handled at runtime (the helper prompts on first capture; denial degrades with a warning), not in the selector — so a first-run undetermined grant still takes the SCK path and triggers the OS prompt.
- `resolve_helper_path() -> Path | None` — frozen bundle `…/Context Recall Daemon.app/Contents/Resources/sck-audio-capture`, else the dev build output (`macos/sck-audio-capture/.build/sck-audio-capture`), else `None`.

### 3. `src/audio_capture.py` — integrate the backend (modify)

`_record_loop` stops opening the BlackHole `sd.InputStream` directly. Instead it asks `select_system_backend(config)` for the system source, starts it with `_system_path`, and keeps the **mic** `sd.InputStream` + `_merge_sources()` exactly as they are. `stop()` stops the system backend (SIGTERM for SCK / stream-close for BlackHole) then joins the mic stream then merges. `last_warning` / `last_error` propagate from the backend (e.g. "Screen Recording not granted — open System Settings → Privacy & Security → Screen Recording, then re-record"). This also shrinks `_record_loop`, which has grown to own two stream lifecycles + metering + merge.

### 4. `src/utils/config.py` — config (modify)

Add to `AudioConfig`:

```python
system_capture_backend: str = "auto"   # auto | blackhole | screencapturekit
```

`__post_init__` rejects any other value. `auto` is the default so existing installs pick up SCK automatically on capable machines with no config edit.

### 5. Build & sign (modify)

- `scripts/build_sck_helper.sh` (new) — `swiftc macos/sck-audio-capture/main.swift -O -o <out>`; callable standalone for dev runs.
- `scripts/build_daemon.sh` — after PyInstaller (alongside the existing MLX-metallib fixup, **before** the outer-app codesign): compile the helper, copy it into `…app/Contents/Resources/sck-audio-capture`, and **sign the helper first** with the stable identity (inside-out), so the subsequent outer-app `codesign --force` seals an already-signed nested binary. Absent the cert (CI/fresh clone) both fall back to ad-hoc, as today. If `swiftc` is unavailable, log a warning and skip — the daemon degrades to BlackHole at runtime rather than failing the build.
- `context-recall.spec` — unchanged for the helper (injected post-build by `build_daemon.sh`, mirroring the metallib fixup), keeping the spec free of a Swift build step.

### 6. Preflight surface (modify, minimal)

`src/api/routes/preflight.py` gains a `screen_recording` status field sourced from `ScreenCaptureKitSystemCapture.preflight()` (helper `--check-permission`), so the daemon logs it at boot and the UI _can_ surface a "grant Screen Recording" hint later. The full banner UI is a documented follow-up, not v1.

## Behaviour on the broken beta

SCK captures the remote/meeting audio into `_system.wav`; the mic source stays silent (mic TCC broken) → `_merge_dual_source` mixes real system audio with a silent mic. The energy diariser (system-vs-mic RMS) sees a silent mic, so **every segment is labelled "Other"** — expected, acceptable degradation: the meeting content is captured and transcribed; the user's own-voice attribution ("Me") returns automatically once mic TCC is fixed and BlackHole/mic un-zero.

## Error handling & degradation

| Condition                                                 | Behaviour                                                                                                                                                        |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auto`, macOS < 13 or helper missing                      | `select_system_backend` → BlackHole (silent on this beta, but no regression on healthy OSes).                                                                    |
| SCK helper exits non-zero (permission denied / SCK error) | `last_error`/`last_warning` set; pipeline surfaces `pipeline.warning`; `_system.wav` may be empty → merge takes the single/silent path, meeting still completes. |
| SIGTERM finalise times out                                | Bounded wait, then the partial WAV (valid header) is used; warning logged.                                                                                       |
| Explicit `screencapturekit` on an incapable machine       | Hard error surfaced (user opted out of fallback).                                                                                                                |

## Testing

**Python (pytest, SCK faked — the suite never touches real SCK):**

- `select_system_backend` — auto/blackhole/screencapturekit across mocked `platform.mac_ver()` + helper-present flag.
- `ScreenCaptureKitSystemCapture` — `start()` spawns a **stub** helper script (writes a WAV, prints `rms=` lines); `stop()` SIGTERMs and waits; stdout parsing drives `on_audio_level`; non-zero exit sets `last_error`. `--check-permission` parsing.
- `BlackHoleSystemCapture` — parity with today's behaviour via mocked `sounddevice` (reuse existing `audio_capture` test patterns).
- `AudioCapture` integration — with the backend faked, `_record_loop` writes `_system.wav` via the selected backend and the existing dual-source merge is unchanged.
- `AudioConfig.system_capture_backend` — default `auto`, parsing, and rejection of unknown values.

**Native helper smoke test (manual/local — Apple Silicon + Screen Recording grant, like the existing MLX/Tauri Apple-Silicon-only CI jobs):** run `sck-audio-capture --output tmp.wav` with `say`/TTS playing, SIGTERM after ~3 s, assert non-empty WAV with RMS above the noise floor; assert `--check-permission` prints a valid token.

## Key risk to de-risk first

**Screen Recording TCC attribution for a daemon-spawned child.** The probe was granted as its own shell-responsible process; when the launchd daemon spawns the helper, macOS attributes the grant to the helper's **own** code identity (its cert-leaf DR) — so the helper needs its **own** one-time Screen Recording Allow (via the OS prompt on first capture, or System Settings → Screen Recording). **Implementation step 1 is a spike:** build+sign the minimal helper, spawn it from the running daemon, and confirm `--check-permission` + a real capture succeed (after a single grant) _as a daemon child_ — before building the full integration. If attribution misbehaves, we adapt (e.g. helper identifier/entitlements) before investing in the abstraction.

## Deploy note

The stable-signature deploy sequence is unchanged: `build_daemon.sh` (now also compiles + inside-out-signs the helper) → `npm run tauri build` → `scripts/inject_daemon.sh` (restores the pristine stable-signed daemon, helper included). After install, the user grants **Screen Recording** once (in addition to the existing mic grant, which is inert on this beta).
